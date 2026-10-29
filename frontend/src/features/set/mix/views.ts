/**
 * The transition editor's view modes (settings.mix_view) and the controls that edit a plan "under the hood":
 *  - simple: style (technique), length and a few amounts, each scaling the planner's own automation on both decks;
 *  - advanced: only the main lanes, the others follow (resonance rides with the filter) or keep the planner's values;
 *  - manual: every lane.
 */
import type { Keyframe, LaneId, MixPlan, MixTechnique, MixView } from "@/api/types";
import { LANE_RANGES } from "@/api/types";
import { type DeckId, LANE_META, type LaneMeta } from "./lanes";

export const MIX_VIEWS: { id: MixView; label: string; hint: string }[] = [
  { id: "simple", label: "Simple", hint: "Pick a style and a length; a few amounts tune it. The planner draws every fade, EQ and effect." },
  { id: "advanced", label: "Advanced", hint: "Edit the main moves (fader, bass, filter, echo, reverb, loops, effects); the rest follows them." },
  { id: "manual", label: "Manual", hint: "Every automation lane of both decks and the master tempo, keyframe by keyframe." },
];

/** Advanced mode's lanes and their names there; EQ mid/high, resonance, echo feedback and tempo stay under the hood. */
export const ADVANCED_LANES: LaneMeta[] = (
  [
    ["volume", "Fader"],
    ["eq_low", "Bass"],
    ["filter", "Filter"],
    ["echo_send", "Echo"],
    ["reverb_send", "Reverb"],
  ] as [LaneId, string][]
).map(([id, label]) => ({ ...LANE_META[id], label }));

/** One-line descriptions of the styles (techniques) for the simple view. */
export const STYLE_HINTS: Record<MixTechnique, string> = {
  "long blend": "Both tracks play together for the whole blend; the bass swaps on a phrase.",
  "bass swap": "B comes in without its lows; the bass trades places on a phrase, then A fades.",
  "phrase blend": "A shorter overlap; the bass swaps on a phrase, then A fades.",
  "filter sweep": "B opens up from a low-pass while A rides a high-pass sweep out.",
  "echo out": "A's last beats are thrown into an echo that rings over B.",
  "loop roll": "A loops shorter and shorter under a rising high-pass, cut as B's bass drops.",
  "drop swap": "A's build runs, then cuts straight into B's drop.",
  slam: "A rides a riser, then is cut into an echo/reverb throw as B slams in.",
  "reverb wash": "A's reverb swells under a rising high-pass, then cuts into B and rings over it.",
  "quick cut": "A straight cut on the downbeat.",
  spinback: "A spins backwards to a stop into a reverb tail; B comes in on the 1.",
  brake: "A slows to a turntable stop into a reverb tail; B comes in on the 1.",
  stutter: "A's last bar stutters into an echo; B comes in on the 1.",
};

/** The simple view's amounts: the lane they read and every lane they scale. */
export type Amount = "echo" | "reverb" | "filter";
export const AMOUNTS: { id: Amount; label: string; lane: LaneId; scales: LaneId[]; hint: string }[] = [
  { id: "echo", label: "Echo", lane: "echo_send", scales: ["echo_send"], hint: "How loud the echo throws are" },
  { id: "reverb", label: "Reverb", lane: "reverb_send", scales: ["reverb_send"], hint: "How much reverb the washes send" },
  { id: "filter", label: "Filter", lane: "filter", scales: ["filter", "filter_res"], hint: "How far the filter sweeps go (resonance follows)" },
];
/** Lowest amount the simple view sets: never all the way to 0, which would lose the shape it scales. */
export const MIN_AMOUNT = 0.05;

const DECKS: DeckId[] = ["a", "b"];
const peak = (kfs: Keyframe[]) => kfs.reduce((m, k) => Math.max(m, Math.abs(k.value)), 0);
const peakUp = (kfs: Keyframe[]) => kfs.reduce((m, k) => Math.max(m, k.value), 0);
const clampTo = (id: LaneId, v: number) => Math.min(LANE_RANGES[id][1], Math.max(LANE_RANGES[id][0], v));

/** An amount's current level: the deepest point of its lane over both decks (0 = the style doesn't use it). */
export function amountOf(plan: MixPlan, which: Amount): number {
  const a = AMOUNTS.find((m) => m.id === which)!;
  return Math.max(...DECKS.map((d) => peak(plan[d].lanes[a.lane])));
}

/** Scales every lane of an amount on both decks so its level becomes `level` (the shape stays the planner's). */
export function setAmount(plan: MixPlan, which: Amount, level: number): MixPlan {
  const a = AMOUNTS.find((m) => m.id === which)!;
  const cur = amountOf(plan, which);
  if (cur < 1e-3) return plan;
  const f = Math.max(MIN_AMOUNT, Math.min(1, level)) / cur;
  const next = { ...plan };
  for (const d of DECKS) {
    const lanes = { ...plan[d].lanes };
    for (const id of a.scales) lanes[id] = lanes[id].map((k) => ({ ...k, value: clampTo(id, k.value * f) }));
    next[d] = { ...plan[d], lanes };
  }
  return next;
}

/**
 * Advanced view: when an edit changed a deck's filter lane, its resonance follows. The new resonance has the
 * filter's keyframes at the planner's resonance-per-filter ratio (taken from the plan before the edit), so a
 * sweep keeps its resonant peak wherever it is moved; a deck whose filter had no resonance keeps none.
 */
export function followFilter(prev: MixPlan, next: MixPlan): MixPlan {
  let out = next;
  for (const d of DECKS) {
    const f = next[d].lanes.filter;
    if (f === prev[d].lanes.filter) continue;
    const pf = peakUp(prev[d].lanes.filter); // resonance rides the high-pass side only
    const ratio = pf > 1e-3 ? peak(prev[d].lanes.filter_res) / pf : 0;
    const res: Keyframe[] = ratio > 0 ? f.map((k) => ({ ...k, value: clampTo("filter_res", Math.max(0, k.value) * ratio) })) : [{ beat: f[0]?.beat ?? 0, value: 0, curve: "hold" }];
    out = { ...out, [d]: { ...out[d], lanes: { ...out[d].lanes, filter_res: res } } };
  }
  return out;
}
