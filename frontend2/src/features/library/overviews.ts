/**
 * Overview waveforms for the library table (#73): the 1,200-column peak overview of each band from the waveform
 * file (#49), fetched for the rows on screen in one binary request (POST /api/library/overviews), kept per track,
 * and drawn into small cached images that the table's one shared canvas blits per row.
 *
 * Response (seedbox2/library/view.py): header "SBOV", columns u16, bands u8, pad u8, tracks u32 (little-endian);
 * then per track asked for: present u8, and if present bands × columns peak bytes (log scale, 0 = -48 dB).
 */
import { api } from "@/api/client";
import { wavePalette } from "@/features/waveform/palette";

export interface Overview {
  columns: number;
  bands: number;
  /** [bands × columns] peak bytes: low, mid, high rows */
  peaks: Uint8Array;
}

export function parseOverviews(buf: ArrayBuffer): (Overview | null)[] {
  const dv = new DataView(buf);
  if (buf.byteLength < 12 || String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3)) !== "SBOV") {
    throw new Error("not an overview response");
  }
  const columns = dv.getUint16(4, true);
  const bands = dv.getUint8(6);
  const count = dv.getUint32(8, true);
  const size = columns * bands;
  const out: (Overview | null)[] = [];
  let at = 12;
  for (let i = 0; i < count; i++) {
    if (at >= buf.byteLength) throw new Error("overview response cut short");
    const present = dv.getUint8(at);
    at += 1;
    if (!present) {
      out.push(null);
      continue;
    }
    if (at + size > buf.byteLength) throw new Error("overview response cut short");
    out.push({ columns, bands, peaks: new Uint8Array(buf, at, size) });
    at += size;
  }
  return out;
}

/** Column heights 0-1 of each band for a `width`-pixel image: the loudest overview column under each pixel. */
export function overviewColumns(o: Overview, width: number): Float32Array[] {
  const bands: Float32Array[] = [];
  for (let b = 0; b < Math.min(3, o.bands); b++) {
    const row = o.peaks.subarray(b * o.columns, (b + 1) * o.columns);
    const out = new Float32Array(width);
    for (let x = 0; x < width; x++) {
      const c0 = Math.floor((x * o.columns) / width);
      const c1 = Math.max(c0 + 1, Math.floor(((x + 1) * o.columns) / width));
      let m = 0;
      for (let c = c0; c < c1; c++) if (row[c] > m) m = row[c];
      out[x] = m / 255; // already a 48 dB log scale: 0 = silence, 1 = full scale
    }
    bands.push(out);
  }
  return bands;
}

// How each band fills its half of the row: lows are the body, highs a thin bright top, like Rekordbox's overview.
const SHAPE = [
  { gain: 1, curve: 2.2, alpha: 1 },
  { gain: 0.8, curve: 2.6, alpha: 0.9 },
  { gain: 0.55, curve: 3, alpha: 0.85 },
];

/** The overview drawn at this size (CSS px × dpr), mirrored around the middle, the three bands on top of each other. */
export function renderOverview(o: Overview, width: number, height: number, dpr: number): HTMLCanvasElement | null {
  const W = Math.max(1, Math.round(width * dpr));
  const H = Math.max(1, Math.round(height * dpr));
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  const p = wavePalette();
  const colors = [p.low, p.mid, p.high];
  const cols = overviewColumns(o, W);
  const mid = H / 2;
  cols.forEach((vals, b) => {
    const s = SHAPE[b];
    ctx.globalAlpha = s.alpha;
    ctx.fillStyle = colors[b];
    ctx.beginPath();
    ctx.moveTo(0, mid);
    for (let x = 0; x < W; x++) ctx.lineTo(x, mid - Math.max(0.5, Math.pow(vals[x], s.curve) * s.gain * mid));
    ctx.lineTo(W, mid);
    for (let x = W - 1; x >= 0; x--) ctx.lineTo(x, mid + Math.max(0.5, Math.pow(vals[x], s.curve) * s.gain * mid));
    ctx.closePath();
    ctx.fill();
  });
  return canvas;
}

// ------------------------------------------------------------------------------------------------ the store

const BATCH = 120;
/** Row images kept (a few screens' worth): ~300 × 270 × 22 px × 4 bytes × dpr² stays a few MB. */
const IMAGES = 300;

/**
 * Overviews by track hash: `undefined` = not asked yet, `null` = none yet (no waveform), else the data (3.6 KB a
 * track). Row images are kept for the IMAGES most recently drawn rows at one size; a new size (the column was
 * resized) drops them.
 */
export class OverviewStore {
  private data = new Map<string, Overview | null>();
  private images = new Map<string, HTMLCanvasElement | null>();
  private imageSize = "";
  private wanted = new Set<string>();
  private pending = new Set<string>();
  private timer: ReturnType<typeof setTimeout> | undefined;
  private listeners = new Set<() => void>();
  constructor(private fetcher: (hashes: string[]) => Promise<ArrayBuffer> = (h) => api.library.overviews(h, { quiet: true })) {}

  /** Called when new overviews arrived (redraw). */
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  get(hash: string): Overview | null | undefined {
    return this.data.get(hash);
  }

  /** The image for a row, rendering it on first use; undefined while its data is on the way (it is requested). */
  image(hash: string, width: number, height: number, dpr: number): HTMLCanvasElement | null | undefined {
    const size = `${width}x${height}@${dpr}`;
    if (size !== this.imageSize) {
      this.images.clear();
      this.imageSize = size;
    }
    const cached = this.images.get(hash);
    if (cached !== undefined) {
      // most recently used last, so the oldest go first
      this.images.delete(hash);
      this.images.set(hash, cached);
      return cached;
    }
    const o = this.data.get(hash);
    if (o === undefined) {
      this.want(hash);
      return undefined;
    }
    const img = o ? renderOverview(o, width, height, dpr) : null;
    this.images.set(hash, img);
    if (this.images.size > IMAGES) this.images.delete(this.images.keys().next().value as string);
    return img;
  }

  /** Ask for this track's overview (batched with the others asked for in the next few ms). */
  want(hash: string): void {
    if (this.data.has(hash) || this.pending.has(hash)) return;
    this.wanted.add(hash);
    this.timer ??= setTimeout(() => void this.flush(), 40);
  }

  /** Forget a track's overview (its waveform was just computed or redone), so it is fetched again. */
  invalidate(hash: string): void {
    if (!this.data.has(hash) && !this.images.has(hash)) return;
    this.data.delete(hash);
    this.images.delete(hash);
    for (const fn of this.listeners) fn();
  }

  async flush(): Promise<void> {
    this.timer = undefined;
    const hashes = [...this.wanted];
    this.wanted.clear();
    for (let i = 0; i < hashes.length; i += BATCH) {
      const batch = hashes.slice(i, i + BATCH);
      batch.forEach((h) => this.pending.add(h));
      try {
        const got = parseOverviews(await this.fetcher(batch));
        batch.forEach((h, k) => this.data.set(h, got[k] ?? null));
      } catch {
        // the engine is away: ask again when the rows are drawn next
      } finally {
        batch.forEach((h) => this.pending.delete(h));
      }
      for (const fn of this.listeners) fn();
    }
  }
}

export const overviews = new OverviewStore();
