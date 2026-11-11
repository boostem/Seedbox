/** A synthetic TrackAnalysis (128 BPM, 64 bars) for the design-system page: shows the waveform code without a backend. */
import type { CuePoint, Section, SectionKind, TrackAnalysis, Waveform } from "@/api/types";
import { prepareBands, type WaveData } from "@/features/waveform/prepare";

const BPM = 128;
const BAR_S = (60 / BPM) * 4;
const BARS = 64;
const PPS = 50;
const LAYOUT: [SectionKind, number, number][] = [
  ["intro", 0, 16],
  ["build", 16, 24],
  ["drop", 24, 40],
  ["breakdown", 40, 48],
  ["drop", 48, 56],
  ["outro", 56, 64],
];

function rand(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

export function demoAnalysis(): TrackAnalysis {
  const duration = BARS * BAR_S;
  const n = Math.round(duration * PPS);
  const r = rand(7);
  const energyAt = (bar: number) => {
    const kind = LAYOUT.find(([, a, b]) => bar >= a && bar < b)?.[0] ?? "body";
    return { intro: 0.45, build: 0.65, drop: 1, breakdown: 0.3, body: 0.7, outro: 0.45 }[kind];
  };
  const low: number[] = [];
  const mid: number[] = [];
  const high: number[] = [];
  for (let i = 0; i < n; i++) {
    const t = i / PPS;
    const bar = Math.floor(t / BAR_S);
    const e = energyAt(bar);
    const beat = (t / (BAR_S / 4)) % 1; // 0 at each kick
    const kick = Math.exp(-beat * 9);
    const breakdown = e < 0.4;
    low.push((breakdown ? 0.08 : 0.25 + 0.75 * kick) * e + r() * 0.05);
    mid.push((0.35 + 0.4 * Math.sin(t * 0.9) ** 2) * e + r() * 0.12);
    high.push((((beat * 2) % 1 < 0.15 ? 0.7 : 0.25) * e) + r() * 0.1);
  }
  const sections: Section[] = LAYOUT.map(([kind, a, b]) => ({ kind, start_bar: a, end_bar: b, start_s: a * BAR_S, end_s: b * BAR_S }));
  const cues: CuePoint[] = [
    { bar: 0, time_s: 0, label: "first downbeat" },
    { bar: 16, time_s: 16 * BAR_S, label: "mix in (16-bar intro)" },
    { bar: 24, time_s: 24 * BAR_S, label: "drop" },
    { bar: 56, time_s: 56 * BAR_S, label: "mix out (8-bar outro)" },
    // hot cues in their slots' colours: B lands on the drop's cue, so the two stack
    { bar: 0, time_s: 0, label: "start", slot: 0 },
    { bar: 24, time_s: 24 * BAR_S, label: "drop", slot: 1 },
    { bar: 40, time_s: 40 * BAR_S, label: "break", slot: 2 },
  ];
  // the phrases the sections analysis finds: a 32-bar phrase opens each drop's run, 8s in the build and breakdown
  const phrases: TrackAnalysis["phrases"] = [
    { bar: 0, level: 16 },
    { bar: 16, level: 8 },
    { bar: 24, level: 16 },
    { bar: 40, level: 8 },
    { bar: 48, level: 8 },
    { bar: 56, level: 8 },
  ];
  const perBar = Array.from({ length: BARS }, (_, b) => energyAt(b));
  return {
    version: 1,
    track_id: "demo",
    duration_s: duration,
    bpm: BPM,
    bpm_source: "detected",
    first_downbeat_s: 0,
    bar_s: BAR_S,
    bar_count: BARS,
    phrase_offset: 0,
    phrases,
    energy_per_bar: perBar,
    low_per_bar: perBar.map((e) => (e < 0.4 ? 0.1 : e)),
    vocal_per_bar: perBar.map(() => 0),
    sections,
    intro: { bars: 16, start_s: 0, end_s: 16 * BAR_S, drums_only: true, has_vocals: false },
    outro: { bars: 8, start_s: 56 * BAR_S, end_s: duration, drums_only: true, has_vocals: false },
    cues,
    waveform: { points_per_second: PPS, low, mid, high },
  };
}

/** Where the demo's mix out would play, as a deck marks it: an 8-bar bass swap over the outro. */
export const demoRegions = () => [{ start_s: 56 * BAR_S, end_s: 64 * BAR_S, label: "Bass swap", technique: "bass swap" }];

/** A 20 s preview clip of the demo track, from its first drop. */
export function demoClip(): WaveData | null {
  const { waveform: w } = demoAnalysis();
  const from = Math.round(24 * BAR_S * PPS);
  const cut = (v: ArrayLike<number>) => Array.prototype.slice.call(v, from, from + 20 * PPS) as number[];
  return prepareBands({ ...w, low: cut(w.low), mid: cut(w.mid), high: cut(w.high) } as Waveform, 20);
}
