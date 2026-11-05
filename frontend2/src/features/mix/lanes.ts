/** Editing a plan's lanes by hand: every edit returns a new plan, for `LiveEngine.schedule` and for A/B. */
import { LANE_DEFAULTS, LANE_RANGES, LANES, STEM_LANES } from "@/api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan, StemLanes } from "@/api/mixplan.generated";

export type DeckId = "a" | "b";
export type LaneName = keyof Lanes | keyof StemLanes;

const SNAP_BEATS = 1 / 16;

const isStem = (name: LaneName): name is keyof StemLanes => (STEM_LANES as readonly string[]).includes(name);

/** The lanes this deck has: the stem lanes only when it has stems. */
export function laneNames(deck: Deck): LaneName[] {
  return deck.stem_lanes ? [...STEM_LANES, ...LANES] : [...LANES];
}

export function laneOf(deck: Deck, name: LaneName): Keyframe[] {
  if (isStem(name)) return deck.stem_lanes?.[name] ?? [];
  return deck.lanes[name];
}

/** Stem lanes that leave every stem as it is: where a deck starts when its track has stems and its plan has no lanes yet. */
export function flatStemLanes(beat: number): StemLanes {
  const flat = (): Keyframe[] => [{ beat, value: 1, curve: "linear" }];
  return { gain_drums: flat(), gain_bass: flat(), gain_vocals: flat(), gain_other: flat() };
}

/** The lane replaced, and pinned: a person set it, so regenerating the plan leaves it alone. */
export function withLane(plan: MixPlan, id: DeckId, name: LaneName, keyframes: Keyframe[]): MixPlan {
  const deck = plan[id];
  const pinned = deck.pinned?.includes(name) ? deck.pinned : [...(deck.pinned ?? []), name];
  const next: Deck = isStem(name)
    ? { ...deck, pinned, stem_lanes: { ...(deck.stem_lanes ?? flatStemLanes(plan.window.start_beat)), [name]: keyframes } }
    : { ...deck, pinned, lanes: { ...deck.lanes, [name]: keyframes } };
  return { ...plan, [id]: next, edited: true };
}

/** What a lane holds where it has no keyframes yet (a stem lane on a deck without any). */
export const laneDefault = (name: LaneName): number => LANE_DEFAULTS[name];

const clampValue = (name: LaneName, value: number): number => {
  const [lo, hi] = LANE_RANGES[name];
  return Math.min(hi, Math.max(lo, value));
};

/** Keyframe `i` moved: its value kept in the lane's range, its beat on the grid and between its neighbours. */
export function moveKeyframe(keyframes: Keyframe[], name: LaneName, i: number, beat: number, value: number): Keyframe[] {
  const lo = i > 0 ? keyframes[i - 1].beat : -Infinity;
  const hi = i < keyframes.length - 1 ? keyframes[i + 1].beat : Infinity;
  const snapped = Math.round(beat / SNAP_BEATS) * SNAP_BEATS;
  return keyframes.map((k, j) => (j === i ? { ...k, beat: Math.min(hi, Math.max(lo, snapped)), value: clampValue(name, value) } : k));
}

/** A new keyframe in its place, and which index it got. */
export function addKeyframe(keyframes: Keyframe[], name: LaneName, beat: number, value: number): { keyframes: Keyframe[]; index: number } {
  const snapped = Math.round(beat / SNAP_BEATS) * SNAP_BEATS;
  let index = keyframes.findIndex((k) => k.beat > snapped);
  if (index < 0) index = keyframes.length;
  const added: Keyframe = { beat: snapped, value: clampValue(name, value), curve: "linear" };
  return { keyframes: [...keyframes.slice(0, index), added, ...keyframes.slice(index)], index };
}

/** Keyframe `i` gone. A lane keeps at least one keyframe: the plan needs it. */
export function removeKeyframe(keyframes: Keyframe[], i: number): Keyframe[] {
  return keyframes.length > 1 ? keyframes.filter((_, j) => j !== i) : keyframes;
}

export const CURVES: Keyframe["curve"][] = ["linear", "ease", "hold"];

export function setCurve(keyframes: Keyframe[], i: number, curve: Keyframe["curve"]): Keyframe[] {
  return keyframes.map((k, j) => (j === i ? { ...k, curve } : k));
}

/** Where both decks sound: what "loop the transition" loops. The whole window when they never do. */
export function transitionRange(plan: MixPlan): [number, number] {
  const from = Math.max(plan.a.play_from_beat, plan.b.play_from_beat, plan.window.start_beat);
  const to = Math.min(plan.a.play_to_beat, plan.b.play_to_beat, plan.window.end_beat);
  return to > from ? [from, to] : [plan.window.start_beat, plan.window.end_beat];
}
