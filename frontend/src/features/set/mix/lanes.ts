/**
 * MixPlan model helpers (mirrors musicreco/mixplan.py): lane metadata (range, colour, snapping, formatting),
 * keyframe interpolation, master beat → deck file time (loops honoured), and render-audio time ↔ master beat.
 */
import type { BlendStyle, Curve, DeckPlan, Keyframe, LaneId, MixPlan, MixRender, MixStyle, MixTechnique } from "@/api/types";
import { LANE_RANGES } from "@/api/types";

export type DeckId = "a" | "b";
/** "tempo" (master) or "<deck>.<lane>". */
export type LaneKey = "tempo" | `${DeckId}.${LaneId}`;

export interface LaneMeta {
  id: LaneId | "tempo";
  label: string;
  short: string;
  color: string; // CSS var
  step: number; // value snap
  fine: number;
  neutral: number;
  bipolar?: boolean;
  fmt: (v: number) => string;
}

const pct = (v: number) => `${Math.round(v * 100)}%`;
const db = (v: number) => (v <= -29.5 ? "kill" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(Math.abs(v) < 10 && v % 1 ? 1 : 0)} dB`);

export const LANES: LaneMeta[] = [
  { id: "volume", label: "Volume", short: "VOL", color: "var(--color-fg)", step: 0.05, fine: 0.01, neutral: 1, fmt: pct },
  { id: "eq_low", label: "EQ Low", short: "LOW", color: "var(--color-wave-low)", step: 1, fine: 0.5, neutral: 0, fmt: db },
  { id: "eq_mid", label: "EQ Mid", short: "MID", color: "var(--color-wave-mid)", step: 1, fine: 0.5, neutral: 0, fmt: db },
  { id: "eq_high", label: "EQ High", short: "HIGH", color: "var(--color-wave-high)", step: 1, fine: 0.5, neutral: 0, fmt: db },
  {
    id: "filter",
    label: "Filter",
    short: "FLT",
    color: "var(--color-info)",
    step: 0.05,
    fine: 0.01,
    neutral: 0,
    bipolar: true,
    fmt: (v) => (Math.abs(v) < 0.005 ? "off" : v < 0 ? `LPF ${Math.round(-v * 100)}%` : `HPF ${Math.round(v * 100)}%`),
  },
  { id: "echo_send", label: "Echo send", short: "ECHO", color: "var(--color-move-boost)", step: 0.05, fine: 0.01, neutral: 0, fmt: pct },
  { id: "echo_feedback", label: "Echo feedback", short: "FB", color: "var(--color-move-boost)", step: 0.05, fine: 0.01, neutral: 0.5, fmt: pct },
  { id: "reverb_send", label: "Reverb send", short: "VERB", color: "var(--color-ok)", step: 0.05, fine: 0.01, neutral: 0, fmt: pct },
];

export const TEMPO_META: LaneMeta = { id: "tempo", label: "Tempo", short: "BPM", color: "var(--color-warn)", step: 0.1, fine: 0.01, neutral: 120, fmt: (v) => `${v.toFixed(1)} BPM` };

export const LANE_META: Record<LaneId | "tempo", LaneMeta> = Object.fromEntries([...LANES, TEMPO_META].map((m) => [m.id, m])) as Record<LaneId | "tempo", LaneMeta>;

/** [min, max] of a lane; the tempo range is derived from the plan (±6 BPM around its keyframes). */
export function laneRange(key: LaneKey, plan: MixPlan): [number, number] {
  if (key === "tempo") {
    const v = plan.tempo.map((k) => k.value);
    const lo = Math.floor(Math.min(...v, plan.a.native_bpm, plan.b.native_bpm) - 4);
    const hi = Math.ceil(Math.max(...v, plan.a.native_bpm, plan.b.native_bpm) + 4);
    return [lo, Math.max(hi, lo + 4)];
  }
  return LANE_RANGES[laneId(key)];
}

export const laneId = (key: LaneKey): LaneId => key.slice(2) as LaneId;
export const deckOf = (key: LaneKey): DeckId | null => (key === "tempo" ? null : (key[0] as DeckId));
export const metaOf = (key: LaneKey): LaneMeta => (key === "tempo" ? TEMPO_META : LANE_META[laneId(key)]);

export function getLane(plan: MixPlan, key: LaneKey): Keyframe[] {
  return key === "tempo" ? plan.tempo : plan[key[0] as DeckId].lanes[laneId(key)];
}

/** Immutable lane replacement (only the touched arrays change identity — rows re-render selectively). */
export function setLane(plan: MixPlan, key: LaneKey, kfs: Keyframe[]): MixPlan {
  if (key === "tempo") return { ...plan, tempo: kfs };
  const d = key[0] as DeckId;
  return { ...plan, [d]: { ...plan[d], lanes: { ...plan[d].lanes, [laneId(key)]: kfs } } };
}

const smooth = (t: number) => t * t * (3 - 2 * t);

/** Value of a lane at master beat (same rules as mixplan.lane_value: flat before/after, curve of the left key). */
export function laneValue(kfs: Keyframe[], beat: number): number {
  if (!kfs.length) return 0;
  if (beat <= kfs[0].beat) return kfs[0].value;
  const last = kfs[kfs.length - 1];
  if (beat >= last.beat) return last.value;
  let lo = 0;
  let hi = kfs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (kfs[mid].beat <= beat) lo = mid;
    else hi = mid;
  }
  const k0 = kfs[lo];
  const k1 = kfs[hi];
  const span = k1.beat - k0.beat;
  if (span <= 0 || k0.curve === "hold") return k0.value;
  const t = (beat - k0.beat) / span;
  return k0.value + (k1.value - k0.value) * (k0.curve === "ease" ? smooth(t) : t);
}

/** Un-looped deck beat for a master beat: inside a loop, the looped source window repeats (slip-style afterwards). */
export function deckBeat(deck: DeckPlan, beat: number): number {
  for (const l of deck.loops) {
    if (beat >= l.start_beat && beat < l.end_beat && l.length_beats > 0) {
      return l.source_beat + ((((beat - l.start_beat) % l.length_beats) + l.length_beats) % l.length_beats);
    }
  }
  return beat;
}

/** Deck file time (s) at a master beat. */
export function deckTime(deck: DeckPlan, beat: number): number {
  return deck.beat0_s + (deckBeat(deck, beat) * 60) / (deck.native_bpm || 120);
}

/** Render audio time (s) → master beat via beat_times_s (linear between beats, extrapolated at the ends). */
export function beatAt(r: MixRender, t: number): number {
  const bt = r.beat_times_s;
  if (bt.length < 2) return r.start_beat;
  if (t <= bt[0]) return r.start_beat - (bt[0] - t) / (bt[1] - bt[0]);
  const n = bt.length - 1;
  if (t >= bt[n]) return r.start_beat + n + (t - bt[n]) / (bt[n] - bt[n - 1]);
  let lo = 0;
  let hi = n;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (bt[mid] <= t) lo = mid;
    else hi = mid;
  }
  return r.start_beat + lo + (t - bt[lo]) / (bt[hi] - bt[lo]);
}

/** Master beat → render audio time (s). */
export function timeAt(r: MixRender, beat: number): number {
  const bt = r.beat_times_s;
  if (bt.length < 2) return 0;
  const x = beat - r.start_beat;
  const n = bt.length - 1;
  if (x <= 0) return Math.max(0, bt[0] + x * (bt[1] - bt[0]));
  if (x >= n) return bt[n] + (x - n) * (bt[n] - bt[n - 1]);
  const i = Math.floor(x);
  return bt[i] + (x - i) * (bt[i + 1] - bt[i]);
}

export const CURVES: Curve[] = ["linear", "ease", "hold"];

export const TECHNIQUES = ["long blend", "bass swap", "filter sweep", "echo out", "loop roll", "phrase blend", "double drop", "drop swap", "slam", "spinback", "brake", "quick cut"] as const;
export const BAR_OPTIONS = [4, 8, 16, 32] as const;

/** The phrase-mixing techniques: their sound is set by a blend style. */
export const BLEND_TECHNIQUES: readonly MixTechnique[] = ["long blend", "phrase blend", "bass swap"];
export const isBlend = (t: MixTechnique) => BLEND_TECHNIQUES.includes(t);

/** Blend styles, the default first, with what each one does (STYLE_HINTS covers every style). */
export const BLEND_STYLES: { id: BlendStyle; hint: string }[] = [
  { id: "phrase mix", hint: "B eases in and A eases out over whole phrases, bass swapped on the 1. No FX." },
  { id: "vocal throw", hint: "A's last sung phrase is thrown into the echo and trails off over B." },
  { id: "roll", hint: "A rolls its last bar (2 → 1 → ½ → ¼ beat) into B's drop or the bass swap." },
  { id: "echo exit", hint: "A's last bar trails off in a dotted-8th echo." },
  { id: "dub echo", hint: "A's last 4 bars build into a long 8th-note echo." },
  { id: "swell in", hint: "B rises out of a reverb wash before its dry signal comes up." },
  { id: "wash exit", hint: "A dissolves into a reverb wash over its last 2 bars." },
  { id: "filter wash", hint: "A climbs a high-pass into a light reverb wash on its way out." },
];

export const STYLE_HINTS: Record<MixStyle, string> = {
  ...(Object.fromEntries(BLEND_STYLES.map((s) => [s.id, s.hint])) as Record<BlendStyle, string>),
  "filter sweep": "A rides a high-pass sweep out while B's low-pass opens: hides a key change.",
  "loop roll": "A loops and rolls 8 → 4 → 2 → 1 → ½ beat into the 1 where B's bass drops in.",
  "double drop": "Both drops land on the same 1, then the bass goes to B.",
  "echo out": "A's last beats fly off into an echo and B starts on the 1.",
  slam: "A 4-bar loop roll and a high-pass riser slam into B on the 1.",
  "quick cut": "A 1-bar stutter, then a clean cut into B on the 1.",
  "drop swap": "A's build cuts straight into B's drop.",
  brake: "A powers down like a turntable switched off; B starts on the 1.",
  spinback: "A is spun back and B lands on the 1: hides a big tempo jump.",
};

/** "bar.beat" relative to B in (beat 0 = bar 1.1; negative beats count back from it). */
export function barLabel(beat: number): string {
  const b = Math.floor(beat + 1e-6);
  const bar = Math.floor(b / 4);
  const inBar = ((b % 4) + 4) % 4;
  return `${bar >= 0 ? bar + 1 : bar}.${inBar + 1}`;
}

const near = (a: number, b: number) => Math.abs(a - b) < 1e-3;

/** Does the plan actually use this lane? Unused = every keyframe sits at the lane default (echo feedback / tempo: constant). */
export function laneUsed(key: LaneKey, kfs: Keyframe[]): boolean {
  if (!kfs.length) return false;
  const first = kfs[0].value;
  if (key === "tempo" || laneId(key) === "echo_feedback") return kfs.some((k) => !near(k.value, first));
  const neutral = LANE_META[laneId(key)].neutral;
  return kfs.some((k) => !near(k.value, neutral));
}
