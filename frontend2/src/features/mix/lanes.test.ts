import { describe, expect, it } from "vitest";
import type { Deck, Keyframe, MixPlan } from "@/api/mixplan.generated";
import { addKeyframe, laneNames, laneOf, moveKeyframe, transitionRange, withLane } from "./lanes";

const kf = (beat: number, value: number): Keyframe => ({ beat, value, curve: "linear" });
const lanes = { volume: [kf(0, 0.5), kf(8, 1), kf(16, 0)] };
const deck = (over: Partial<Deck> = {}) => ({ lanes, stem_lanes: null, play_from_beat: 0, play_to_beat: 32, ...over }) as unknown as Deck;
const plan = (a: Deck, b: Deck) => ({ a, b, window: { start_beat: 0, end_beat: 64 }, edited: false }) as MixPlan;

describe("lane edits", () => {
  it("keeps a keyframe in its lane's range and between its neighbours, on the grid", () => {
    const moved = moveKeyframe(lanes.volume, "volume", 1, 100.03, 5);
    expect(moved[1]).toEqual({ beat: 16, value: 1, curve: "linear" });
    expect(moveKeyframe(lanes.volume, "volume", 1, 3.01, -2)[1]).toEqual({ beat: 3, value: 0, curve: "linear" });
    expect(lanes.volume[1].beat).toBe(8); // the plan it came from is untouched
  });

  it("uses each lane's own range", () => {
    expect(moveKeyframe([kf(0, 0)], "eq_low", 0, 0, -99)[0].value).toBe(-30);
    expect(moveKeyframe([kf(0, 0)], "filter", 0, 0, 2)[0].value).toBe(1);
  });

  it("adds a keyframe in order", () => {
    const { keyframes, index } = addKeyframe(lanes.volume, "volume", 4, 0.25);
    expect(index).toBe(1);
    expect(keyframes.map((k) => k.beat)).toEqual([0, 4, 8, 16]);
    expect(addKeyframe(lanes.volume, "volume", 40, 0.25).index).toBe(3);
  });

  it("marks the plan edited and leaves the other deck alone", () => {
    const b = deck();
    const edited = withLane(plan(deck(), b), "a", "volume", [kf(0, 0.1)]);
    expect(edited.edited).toBe(true);
    expect(laneOf(edited.a, "volume")).toEqual([kf(0, 0.1)]);
    expect(edited.b).toBe(b);
  });

  it("offers stem lanes only on a deck with stems, and edits them there", () => {
    const stems = { gain_drums: [kf(0, 1)], gain_bass: [kf(0, 1)], gain_vocals: [kf(0, 1)], gain_other: [kf(0, 1)] };
    expect(laneNames(deck())).not.toContain("gain_bass");
    expect(laneNames(deck({ stem_lanes: stems }))).toContain("gain_bass");
    const edited = withLane(plan(deck({ stem_lanes: stems }), deck()), "a", "gain_bass", [kf(0, 0)]);
    expect(edited.a.stem_lanes?.gain_bass).toEqual([kf(0, 0)]);
  });

  it("the transition is where both decks sound; without that, the window", () => {
    expect(transitionRange(plan(deck({ play_from_beat: 0, play_to_beat: 32 }), deck({ play_from_beat: 16, play_to_beat: 64 })))).toEqual([16, 32]);
    expect(transitionRange(plan(deck({ play_to_beat: 8 }), deck({ play_from_beat: 16 })))).toEqual([0, 64]);
  });
});
