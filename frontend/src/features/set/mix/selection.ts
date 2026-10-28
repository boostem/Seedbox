/** Keyframe selection maths of the mix DAW: hit-testing, dragging, pruning and deleting a selection. */
import type { Keyframe, MixPlan } from "@/api/types";
import type { View } from "@/features/waveform/draw";
import { clamp, type Sel, snapTo } from "./daw";
import { getLane, laneRange, metaOf, setLane } from "./lanes";
import { valueY } from "./paint";

/** Index of the keyframe within 8px of (x, y) on a lane row of height h, or null. */
export function hitKeyframe(kfs: Keyframe[], view: View, range: [number, number], h: number, x: number, y: number): number | null {
  let best: number | null = null;
  let bestD = 8;
  kfs.forEach((k, i) => {
    const d = Math.hypot((k.beat - view.start) * view.pxPerSec - x, valueY(k.value, range, h) - y);
    if (d < bestD) [best, bestD] = [i, d];
  });
  return best;
}

/** Move the selected keyframes (from a drag-start snapshot) — re-sorts lanes and remaps the selection. */
export function moveSelection(snap: MixPlan, sel: Sel, dBeat: number, dNorm: number, grid: number, free: boolean): { plan: MixPlan; sel: Sel } {
  let plan = snap;
  const next: Sel = new Map();
  const lo = snap.window.start_beat - 32;
  const hi = snap.window.end_beat + 32;
  for (const [key, idx] of sel) {
    const kfs = getLane(snap, key);
    const range = laneRange(key, snap);
    const meta = metaOf(key);
    const entries = kfs.map((k, i) => {
      if (!idx.has(i)) return { k, s: false };
      const beat = clamp(free ? k.beat + dBeat : snapTo(k.beat + dBeat, grid), lo, hi);
      const raw = k.value + dNorm * (range[1] - range[0]);
      const value = clamp(free ? raw : snapTo(raw, meta.step), range[0], range[1]);
      return { k: { ...k, beat: Math.round(beat * 1000) / 1000, value: Math.round(value * 1000) / 1000 }, s: true };
    });
    entries.sort((a, b) => a.k.beat - b.k.beat);
    plan = setLane(plan, key, entries.map((e) => e.k));
    next.set(key, new Set(entries.flatMap((e, i) => (e.s ? [i] : []))));
  }
  return { plan, sel: next };
}

/** Drop selected indexes that no longer exist in the plan (it was regenerated); the same Sel when nothing changed. */
export function pruneSelection(sel: Sel, plan: MixPlan): Sel {
  let changed = false;
  const next: Sel = new Map();
  for (const [k, idx] of sel) {
    const n = getLane(plan, k).length;
    const keep = new Set([...idx].filter((i) => i < n));
    if (keep.size !== idx.size) changed = true;
    if (keep.size) next.set(k, keep);
  }
  return changed ? next : sel;
}

/** Remove the selected keyframes; a lane always keeps at least its first keyframe. */
export function deleteKeyframes(plan: MixPlan, sel: Sel): MixPlan {
  let next = plan;
  for (const [k, idx] of sel) {
    const lane = getLane(plan, k);
    let kept = lane.filter((_, i) => !idx.has(i));
    if (!kept.length) kept = [lane[0]];
    next = setLane(next, k, kept);
  }
  return next;
}
