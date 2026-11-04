import { describe, expect, it } from "vitest";
import { LANE_DEFAULTS, LANE_RANGES, LANES, STEM_LANES } from "./mixplan.generated";
import type { Keyframe, Lanes, MixPlan, StemLanes } from "./mixplan.generated";

const hold = (value: number): Keyframe[] => [{ beat: 0, value, curve: "hold" }];

// Fails `tsc` when the generated types stop describing a whole plan, with every default filled in as the API sends it.
function plan(): MixPlan {
  const lanes = Object.fromEntries(LANES.map((name) => [name, hold(LANE_DEFAULTS[name])])) as unknown as Lanes;
  const stems = Object.fromEntries(STEM_LANES.map((name) => [name, hold(LANE_DEFAULTS[name])])) as unknown as StemLanes;
  const deck = (track_id: string, stem_lanes: StemLanes | null): MixPlan["a"] => ({
    track_id,
    grid: [{ start_beat: 0, start_s: 1.5, bpm: 124 }],
    key_shift: -1,
    play_from_beat: -32,
    play_to_beat: 64,
    lanes,
    stem_lanes,
    sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "post_fader" },
    loops: [{ start_beat: 0, end_beat: 8, source_beat: 0, length_beats: 4 }],
    fx: [
      { type: "brake", start_beat: 60, end_beat: 62 },
      { type: "stutter", start_beat: 62, end_beat: 64, div: 0.25 },
    ],
  });
  return {
    version: 3,
    technique: "long blend",
    bars: 16,
    key_lock: true,
    window: { start_beat: -32, end_beat: 64 },
    tempo: hold(124),
    a: deck("a", stems),
    b: deck("b", null),
    markers: [{ beat: 0, label: "B in" }],
    notes: [],
    edited: false,
  };
}

describe("the generated mix plan types", () => {
  it("describe a whole plan", () => {
    expect(plan().a.stem_lanes?.gain_bass[0].value).toBe(1);
    expect(plan().b.stem_lanes).toBeNull();
  });

  it("have a range and a default for every lane", () => {
    for (const name of [...LANES, ...STEM_LANES]) {
      const [lo, hi] = LANE_RANGES[name];
      expect(LANE_DEFAULTS[name]).toBeGreaterThanOrEqual(lo);
      expect(LANE_DEFAULTS[name]).toBeLessThanOrEqual(hi);
    }
  });
});
