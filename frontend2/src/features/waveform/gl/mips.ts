/**
 * The waveform as texture data: the three normalised bands as bytes (R = low, G = mid, B = high, A = the loudest of
 * the three), with a mip chain where each level holds the peak of two points of the level below (peaks, not
 * averages, so a kick stays as tall zoomed out as in). Level 0 is the file's own resolution (150 points/s); levels
 * stop once a level fits in `MIN_LEVEL` points, so a whole track drawn across ~1,200 columns reads one or two texels
 * per column.
 *
 * The levels are packed into one RGBA8 atlas `width` texels wide, each level starting on a new row: a 2-hour mix at
 * 150 points/s is ~1.1 M points, wider than any texture, so point `i` of level `k` lives at
 * (i % width, rows[k] + floor(i / width)). Pure functions: tests run them, the renderer uploads the result once.
 */
import type { WaveData } from "../prepare";
import type { StemWave } from "../stems";

export const MIN_LEVEL = 256;
export const MAX_LEVELS = 16;

export interface WaveAtlas {
  width: number;
  height: number;
  /** first row of each level */
  rows: number[];
  /** points in each level */
  lengths: number[];
  data: Uint8Array;
}

const byte = (v: number) => (v >= 1 ? 255 : v > 0 ? Math.round(v * 255) : 0);

/** The mip chain of `wave` as interleaved RGBA bytes, one array per level (level 0 first). */
export function waveMips(wave: Pick<WaveData, "n" | "low" | "mid" | "high">): Uint8Array[] {
  const n = wave.n;
  const base = new Uint8Array(n * 4);
  for (let i = 0; i < n; i++) {
    const l = byte(wave.low[i]);
    const m = byte(wave.mid[i]);
    const h = byte(wave.high[i]);
    base[i * 4] = l;
    base[i * 4 + 1] = m;
    base[i * 4 + 2] = h;
    base[i * 4 + 3] = Math.max(l, m, h);
  }
  return mipChain(base);
}

/** The chain above an interleaved RGBA level 0: each level holds the peak, per channel, of two points below it. */
function mipChain(base: Uint8Array): Uint8Array[] {
  const levels = [base];
  let prev = base;
  while (prev.length / 4 > MIN_LEVEL && levels.length < MAX_LEVELS) {
    const pn = prev.length / 4;
    const len = Math.ceil(pn / 2);
    const next = new Uint8Array(len * 4);
    for (let i = 0; i < len; i++) {
      const a = i * 8;
      const b = Math.min(a + 4, (pn - 1) * 4); // an odd last point pairs with itself
      for (let c = 0; c < 4; c++) next[i * 4 + c] = Math.max(prev[a + c], prev[b + c]);
    }
    levels.push(next);
    prev = next;
  }
  return levels;
}

/** Packs the mip chain into one atlas at most `maxWidth` texels wide (the GPU's MAX_TEXTURE_SIZE, or less). */
export function waveAtlas(wave: Pick<WaveData, "n" | "low" | "mid" | "high">, maxWidth = 4096): WaveAtlas {
  return packLevels(waveMips(wave), wave.n, maxWidth);
}

/** A stem waveform as an atlas of the same layout: R drums, G bass, B vocals, A other, each with its own peaks. */
export function stemAtlas(stems: Pick<StemWave, "n" | "drums" | "bass" | "vocals" | "other">, maxWidth = 4096): WaveAtlas {
  const base = new Uint8Array(stems.n * 4);
  for (let i = 0; i < stems.n; i++) {
    base[i * 4] = byte(stems.drums[i]);
    base[i * 4 + 1] = byte(stems.bass[i]);
    base[i * 4 + 2] = byte(stems.vocals[i]);
    base[i * 4 + 3] = byte(stems.other[i]);
  }
  return packLevels(mipChain(base), stems.n, maxWidth);
}

function packLevels(levels: Uint8Array[], n: number, maxWidth: number): WaveAtlas {
  const width = Math.max(1, Math.min(maxWidth, n));
  const rows: number[] = [];
  const lengths: number[] = [];
  let height = 0;
  for (const lv of levels) {
    rows.push(height);
    lengths.push(lv.length / 4);
    height += Math.max(1, Math.ceil(lv.length / 4 / width));
  }
  const data = new Uint8Array(width * height * 4);
  levels.forEach((lv, k) => data.set(lv, rows[k] * width * 4));
  return { width, height, rows, lengths, data };
}

/** The level whose points come closest to one per column without being sparser (fractional: the shader blends the
 * two levels around it so zooming doesn't step). */
export function mipLevel(pointsPerColumn: number, levels: number): number {
  if (!(pointsPerColumn > 1)) return 0;
  return Math.min(levels - 1, Math.log2(pointsPerColumn));
}
