/**
 * Waveform data prep: TrackAnalysis.waveform (3 bands, 0-1, ~50 points/s) or the binary waveform file → Float32Arrays
 * normalised per band so every track reads alike (the engine's bands have different natural ranges: lows top out
 * ~0.6, mids ~1.0). Pure functions: `wave.worker.ts` runs them off the main thread (`useWave` in `waveData.ts`);
 * `prepareWave` is the synchronous version for tests and browsers without workers.
 */
import type { TrackAnalysis, Waveform } from "@/api/types";

export interface WaveData {
  pps: number; // points per second
  n: number;
  duration: number;
  low: Float32Array;
  mid: Float32Array;
  high: Float32Array;
}

const cache = new WeakMap<Waveform, WaveData>();

/** Robust per-band ceiling (99th percentile), so a few spikes don't flatten the rest. */
export function ceiling(v: ArrayLike<number>): number {
  if (!v.length) return 1;
  const step = Math.max(1, Math.floor(v.length / 4000));
  const sample: number[] = [];
  for (let i = 0; i < v.length; i += step) sample.push(v[i]);
  sample.sort((a, b) => a - b);
  return Math.max(0.05, sample[Math.min(sample.length - 1, Math.floor(sample.length * 0.99))]);
}

function norm(v: ArrayLike<number>, n: number): Float32Array {
  const out = new Float32Array(n);
  const c = ceiling(v);
  for (let i = 0; i < n; i++) {
    const x = v[i] / c;
    out[i] = x > 1 ? 1 : x < 0 || !Number.isFinite(x) ? 0 : x;
  }
  return out;
}

/** The three bands of a waveform, normalised. */
export function prepareBands(w: Waveform, duration: number): WaveData | null {
  if (!w || !w.points_per_second || !w.low?.length) return null;
  const n = Math.min(w.low.length, w.mid.length, w.high.length);
  return {
    pps: w.points_per_second,
    n,
    duration: duration || n / w.points_per_second,
    low: norm(w.low, n),
    mid: norm(w.mid, n),
    high: norm(w.high, n),
  };
}

/** Synchronous prep on the main thread, cached per Waveform object. Prefer `useWave`, which uses the worker. */
export function prepareWave(an: TrackAnalysis): WaveData | null {
  const w = an.waveform;
  const hit = w && cache.get(w);
  if (hit) return hit;
  const data = prepareBands(w, an.duration_s);
  if (data) cache.set(w, data);
  return data;
}

// ------------------------------------------------------------------------------------------------ binary file

/**
 * The binary waveform file (`GET /api/tracks/:id/waveform`, #49), version 1, little-endian:
 *
 *   0  "SBWF"   magic
 *   4  u8       version (1)
 *   5  u8       bands (3: low, mid, high)
 *   6  u16      flags: bit 0 = an RMS plane per band follows the peak planes
 *   8  f32      points per second (150)
 *   12 u32      n, points per band
 *   16 u8[n]    peaks of each band in turn, log-scaled: 0 = silence, 1..255 = -DB_RANGE..0 dBFS
 *
 * The decoder reads the peaks and ignores what follows them, so later versions can append planes (RMS, overview,
 * stems) without breaking it. #49 owns the encoder; change both together.
 */
export const WAVE_MAGIC = "SBWF";
export const WAVE_HEADER = 16;
export const DB_RANGE = 60;

const fromLog = new Float32Array(256).map((_, b) => (b === 0 ? 0 : 10 ** (((b - 255) / 254) * (DB_RANGE / 20))));

export function decodeWaveBinary(buf: ArrayBuffer, duration?: number): WaveData {
  if (buf.byteLength < WAVE_HEADER) throw new Error("waveform file too short");
  const v = new DataView(buf);
  const magic = String.fromCharCode(v.getUint8(0), v.getUint8(1), v.getUint8(2), v.getUint8(3));
  if (magic !== WAVE_MAGIC) throw new Error("not a waveform file");
  const version = v.getUint8(4);
  if (version !== 1) throw new Error(`unsupported waveform file version ${version}`);
  const bands = v.getUint8(5);
  const pps = v.getFloat32(8, true);
  const n = v.getUint32(12, true);
  if (bands < 3 || !(pps > 0) || buf.byteLength < WAVE_HEADER + bands * n) throw new Error("corrupt waveform file");
  const plane = (k: number) => {
    const bytes = new Uint8Array(buf, WAVE_HEADER + k * n, n);
    const amp = new Float32Array(n);
    for (let i = 0; i < n; i++) amp[i] = fromLog[bytes[i]];
    return norm(amp, n);
  };
  return { pps, n, duration: duration || n / pps, low: plane(0), mid: plane(1), high: plane(2) };
}

/** The encoder's half of the format, for tests and the demo data: amplitudes 0-1 → log-scaled bytes. */
export function encodeWaveBinary(pps: number, bands: ArrayLike<number>[]): ArrayBuffer {
  const n = Math.min(...bands.map((b) => b.length));
  const buf = new ArrayBuffer(WAVE_HEADER + bands.length * n);
  const v = new DataView(buf);
  for (let i = 0; i < 4; i++) v.setUint8(i, WAVE_MAGIC.charCodeAt(i));
  v.setUint8(4, 1);
  v.setUint8(5, bands.length);
  v.setUint16(6, 0, true);
  v.setFloat32(8, pps, true);
  v.setUint32(12, n, true);
  const out = new Uint8Array(buf, WAVE_HEADER);
  bands.forEach((b, k) => {
    for (let i = 0; i < n; i++) {
      const db = 20 * Math.log10(Math.max(1e-9, b[i]));
      out[k * n + i] = db <= -DB_RANGE ? 0 : Math.max(1, Math.min(255, Math.round(255 + (db / DB_RANGE) * 254)));
    }
  });
  return buf;
}
