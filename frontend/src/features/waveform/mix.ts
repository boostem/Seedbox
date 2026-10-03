/**
 * Geometry of an A → B transition on a shared "mix time" axis (= A's file time). How to read a Transition
 * (musicreco/transitions.py): out_start_s is A's file time where B comes in, in_start_s is B's file time it starts
 * from, overlap_bars is counted in A's bars (B is pitched to A's tempo by pitch_pct), overlap 0 = cut on the 1.
 * B's file time t maps to mix time out_start_s + (t − in_start_s) / (1 + pitch_pct/100).
 */
import type { TrackAnalysis, Transition } from "@/api/types";
import { type Lane, laneOf, toFile, toMix } from "./draw";
import { prepareWave } from "./prepare";

export interface MixGeometry {
  laneA: Lane;
  laneB: Lane;
  rate: number; // B playback rate
  out: number; // mix time B comes in (= A file time)
  outEnd: number; // mix time A is out (end of the overlap; = out for a cut)
  overlapS: number;
  bars: number; // overlap bars (0 = cut)
  barA: number; // A's bar length, s
  a0: number | null; // A bar index where B comes in (null without A's grid)
  inStart: number; // B file time it starts from
  /** Overlap bars (0-based offsets) where both decks carry vocals (vocal_per_bar ≥ 0.5). */
  clash: number[];
  /** 0-based overlap bar where the bass swaps (Transition.swap_bar, else parsed from the notes). */
  swapBar: number | null;
  /** Full extent of both decks on the mix axis. */
  min: number;
  max: number;
}

const VOCAL_ON = 0.5;

export function mixGeometry(t: Transition, a: TrackAnalysis | null, b: TrackAnalysis | null, aBpm: number): MixGeometry | null {
  if (t.out_start_s == null || t.in_start_s == null) return null;
  const rate = 1 + (t.pitch_pct || 0) / 100;
  const out = t.out_start_s;
  const inStart = t.in_start_s;
  const laneA = laneOf(a, a ? prepareWave(a) : null, 0, 1);
  const laneB = laneOf(b, b ? prepareWave(b) : null, out - inStart / rate, rate);
  const barA = a?.bar_s && a.bar_s > 0 ? a.bar_s : 240 / (aBpm || 120);
  const bars = Math.max(0, t.overlap_bars ?? 0);
  const overlapS = bars * barA;
  const a0 = a?.bar_s ? Math.round((out - a.first_downbeat_s) / a.bar_s) : null;

  const clash: number[] = [];
  if (a && b && a.bar_s > 0 && b.bar_s > 0 && bars > 0 && a0 != null) {
    for (let j = 0; j < bars; j++) {
      const va = a.vocal_per_bar?.[a0 + j] ?? 0;
      if (va < VOCAL_ON) continue;
      const t0 = toFile(laneB, out + j * barA);
      const t1 = toFile(laneB, out + (j + 1) * barA);
      const k0 = Math.floor((t0 - b.first_downbeat_s) / b.bar_s + 0.02);
      const k1 = Math.ceil((t1 - b.first_downbeat_s) / b.bar_s - 0.02);
      let vb = 0;
      for (let k = Math.max(0, k0); k < Math.min(b.vocal_per_bar?.length ?? 0, Math.max(k0 + 1, k1)); k++) vb = Math.max(vb, b.vocal_per_bar[k]);
      if (vb >= VOCAL_ON) clash.push(j);
    }
  }

  // swap_bar is 1-based within the overlap; older data only has it in the notes ("bar N of the blend")
  let swapBar: number | null = t.swap_bar != null && t.swap_bar >= 1 && t.swap_bar <= bars ? t.swap_bar - 1 : null;
  if (swapBar == null && t.swap_bar === undefined) for (const n of t.notes) {
    const m = /\bbar (\d+) of the (?:blend|overlap)/i.exec(n);
    if (m) {
      const k = Number(m[1]) - 1;
      if (k >= 0 && k < bars) swapBar = k;
      break;
    }
  }

  const aEnd = a?.duration_s ?? out + overlapS;
  const bEnd = b ? toMix(laneB, b.duration_s) : out + overlapS;
  return {
    laneA,
    laneB,
    rate,
    out,
    outEnd: out + overlapS,
    overlapS,
    bars,
    barA,
    a0,
    inStart,
    clash,
    swapBar,
    min: Math.min(0, laneB.origin),
    max: Math.max(aEnd, bEnd),
  };
}
