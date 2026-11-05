/** A synthetic set for the benchmark's timeline scenarios (#/bench): real sizes, no server. */
import type { SetOrderTrack, SetTimelineData } from "@/api/types";
import type { WaveData } from "@/features/waveform/prepare";

const PPS = 150;
const BEAT_S = 60 / 124;

function rand(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

function demoWave(seed: number, duration: number): WaveData {
  const n = Math.floor(duration * PPS);
  const low = new Float32Array(n);
  const mid = new Float32Array(n);
  const high = new Float32Array(n);
  const r = rand(seed);
  const beat = BEAT_S * PPS;
  for (let i = 0; i < n; i++) {
    const phase = (i % beat) / beat;
    const envelope = Math.min(1, i / (24 * PPS), (n - i) / (24 * PPS));
    low[i] = envelope * Math.max(0, 1 - phase * 3);
    mid[i] = envelope * (0.3 + 0.3 * r());
    high[i] = envelope * (phase > 0.5 ? 0.4 * r() : 0.1 * r());
  }
  return { pps: PPS, n, duration, low, mid, high };
}

export interface DemoSet {
  order: SetOrderTrack[];
  data: SetTimelineData;
  waves: Map<string, WaveData>;
}

/** `tracks` tracks of 3-5 minutes with 16-bar blends, every one with its own waveform. */
export function demoSet(tracks: number): DemoSet {
  const order: SetOrderTrack[] = [];
  const waves = new Map<string, WaveData>();
  const data: SetTimelineData = { tracks: [], transitions: [] };
  const blend = 64 * BEAT_S;
  for (let i = 0; i < tracks; i++) {
    const hash = `demo${i}`;
    const duration = 180 + ((i * 53) % 120);
    waves.set(hash, demoWave(i + 1, duration));
    const energy = 0.3 + 0.6 * Math.sin((i / tracks) * Math.PI);
    order.push({ position: i, hash, title: `Track ${i + 1}`, artist: "Demo", bpm: 120 + (i * 8) / tracks, key: `${1 + ((i * 7) % 12)}A`, energy, rating: Math.round(energy * 9) + 1, shift: 0, pinned: false, target_energy: energy, target_bpm: null });
    const curve = Array.from({ length: 64 }, (_, k): [number, number] => [(k / 64) * duration, Math.max(0, Math.min(1, energy + 0.2 * Math.sin(k / 5)))]);
    data.tracks.push({ hash, duration_s: duration, curve, bar_s: 4 * BEAT_S, sections: [{ kind: "intro", start_s: 0, end_s: 30 }, { kind: "drop", start_s: 30, end_s: duration - 30 }, { kind: "outro", start_s: duration - 30, end_s: duration }] });
    if (i) data.transitions.push({ from: `demo${i - 1}`, to: hash, state: "ready", technique: "long blend", bars: 16, seconds: blend, a_in_s: data.tracks[i - 1].duration_s - blend, a_out_s: data.tracks[i - 1].duration_s, b_in_s: 0, b_out_s: blend });
  }
  return { order, data, waves };
}
