/**
 * Waveform data prep: TrackAnalysis.waveform (3 bands, 0-1, ~50 points/s) → Float32Arrays normalised per band so
 * every track reads alike (the engine's bands have different natural ranges: lows top out ~0.6, mids ~1.0).
 * Cached per Waveform object, so re-renders/zooms never redo it.
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
function ceiling(v: number[]): number {
  if (!v.length) return 1;
  const step = Math.max(1, Math.floor(v.length / 4000));
  const sample: number[] = [];
  for (let i = 0; i < v.length; i += step) sample.push(v[i]);
  sample.sort((a, b) => a - b);
  return Math.max(0.05, sample[Math.min(sample.length - 1, Math.floor(sample.length * 0.99))]);
}

function norm(v: number[], n: number): Float32Array {
  const out = new Float32Array(n);
  const c = ceiling(v);
  for (let i = 0; i < n; i++) {
    const x = v[i] / c;
    out[i] = x > 1 ? 1 : x < 0 || !Number.isFinite(x) ? 0 : x;
  }
  return out;
}

export function prepareWave(an: TrackAnalysis): WaveData | null {
  const w = an.waveform;
  if (!w || !w.points_per_second || !w.low?.length) return null;
  const hit = cache.get(w);
  if (hit) return hit;
  const n = Math.min(w.low.length, w.mid.length, w.high.length);
  const data: WaveData = {
    pps: w.points_per_second,
    n,
    duration: an.duration_s || n / w.points_per_second,
    low: norm(w.low, n),
    mid: norm(w.mid, n),
    high: norm(w.high, n),
  };
  cache.set(w, data);
  return data;
}
