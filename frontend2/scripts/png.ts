// PNG decode/encode and a pixel diff for the screenshot tests (scripts/screenshot.ts), on web APIs only
// ((De)CompressionStream), so they run in Deno and in vitest without a dependency. Decodes what Chrome writes:
// 8-bit greyscale, RGB or RGBA (with or without alpha), not interlaced.

export interface Image {
  width: number;
  height: number;
  data: Uint8Array; // RGBA, row-major
}

const SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

async function pipe(bytes: Uint8Array, stream: CompressionStream | DecompressionStream): Promise<Uint8Array> {
  const out = new Response(new Blob([bytes as BlobPart]).stream().pipeThrough(stream));
  return new Uint8Array(await out.arrayBuffer());
}

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(bytes: Uint8Array): number {
  let c = 0xffffffff;
  for (const b of bytes) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function paeth(a: number, b: number, c: number): number {
  const p = a + b - c;
  const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
}

export async function decodePng(bytes: Uint8Array): Promise<Image> {
  if (!SIGNATURE.every((b, i) => bytes[i] === b)) throw new Error("not a PNG");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let width = 0, height = 0, channels = 0;
  const idat: Uint8Array[] = [];
  for (let at = 8; at < bytes.length;) {
    const len = view.getUint32(at);
    const type = String.fromCharCode(...bytes.subarray(at + 4, at + 8));
    const body = bytes.subarray(at + 8, at + 8 + len);
    if (type === "IHDR") {
      width = view.getUint32(at + 8);
      height = view.getUint32(at + 12);
      const [depth, colour, , , interlace] = body.subarray(8, 13);
      channels = ({ 0: 1, 2: 3, 4: 2, 6: 4 } as Record<number, number>)[colour] ?? 0;
      if (depth !== 8 || !channels || interlace) throw new Error(`unsupported PNG (bit depth ${depth}, colour type ${colour}, interlace ${interlace})`);
    } else if (type === "IDAT") idat.push(body);
    else if (type === "IEND") break;
    at += 12 + len;
  }
  const raw = await pipe(concat(idat), new DecompressionStream("deflate"));
  const stride = width * channels;
  const px = new Uint8Array(stride * height);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const src = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    const row = y * stride;
    for (let x = 0; x < stride; x++) {
      const a = x >= channels ? px[row + x - channels] : 0;
      const b = y > 0 ? px[row - stride + x] : 0;
      const c = x >= channels && y > 0 ? px[row - stride + x - channels] : 0;
      const pred = filter === 0 ? 0 : filter === 1 ? a : filter === 2 ? b : filter === 3 ? (a + b) >> 1 : paeth(a, b, c);
      px[row + x] = (src[x] + pred) & 0xff;
    }
  }
  const data = new Uint8Array(width * height * 4);
  for (let i = 0, j = 0; i < width * height; i++, j += channels) {
    const grey = channels <= 2;
    data[i * 4] = px[j];
    data[i * 4 + 1] = px[grey ? j : j + 1];
    data[i * 4 + 2] = px[grey ? j : j + 2];
    data[i * 4 + 3] = channels === 4 ? px[j + 3] : channels === 2 ? px[j + 1] : 255;
  }
  return { width, height, data };
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

function chunk(type: string, body: Uint8Array): Uint8Array {
  const out = new Uint8Array(12 + body.length);
  const view = new DataView(out.buffer);
  view.setUint32(0, body.length);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  out.set(body, 8);
  view.setUint32(8 + body.length, crc32(out.subarray(4, 8 + body.length)));
  return out;
}

/** RGBA PNG, no filtering (for diff images; size doesn't matter much). */
export async function encodePng({ width, height, data }: Image): Promise<Uint8Array> {
  const ihdr = new Uint8Array(13);
  const view = new DataView(ihdr.buffer);
  view.setUint32(0, width);
  view.setUint32(4, height);
  ihdr.set([8, 6, 0, 0, 0], 8);
  const raw = new Uint8Array((width * 4 + 1) * height);
  for (let y = 0; y < height; y++) raw.set(data.subarray(y * width * 4, (y + 1) * width * 4), y * (width * 4 + 1) + 1);
  const idat = await pipe(raw, new CompressionStream("deflate"));
  return concat([new Uint8Array(SIGNATURE), chunk("IHDR", ihdr), chunk("IDAT", idat), chunk("IEND", new Uint8Array())]);
}

export interface DiffResult {
  /** Pixels whose colour moved more than `threshold`. */
  changed: number;
  total: number;
  /** The new screenshot faded to grey, changed pixels in red. */
  image: Image;
}

/**
 * Compares two images of the same size. A pixel counts as changed when any channel differs by more than
 * `threshold` (0-255): small anti-aliasing and colour-rounding noise stays under it.
 */
export function diffImages(a: Image, b: Image, threshold = 24): DiffResult {
  if (a.width !== b.width || a.height !== b.height) throw new Error(`size changed: ${a.width}×${a.height} → ${b.width}×${b.height}`);
  const out = new Uint8Array(a.data.length);
  let changed = 0;
  for (let i = 0; i < a.data.length; i += 4) {
    let d = 0;
    for (let c = 0; c < 4; c++) d = Math.max(d, Math.abs(a.data[i + c] - b.data[i + c]));
    if (d > threshold) {
      changed++;
      out.set([255, 0, 64, 255], i);
    } else {
      const grey = (b.data[i] * 0.3 + b.data[i + 1] * 0.59 + b.data[i + 2] * 0.11) * 0.35;
      out.set([grey, grey, grey, 255], i);
    }
  }
  return { changed, total: a.width * a.height, image: { width: a.width, height: a.height, data: out } };
}
