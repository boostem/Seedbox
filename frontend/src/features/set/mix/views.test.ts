import { describe, expect, it } from "vitest";
import type { Keyframe, LaneId, MixPlan } from "@/api/types";
import { amountOf, followFilter, setAmount } from "./views";

const kf = (beat: number, value: number): Keyframe => ({ beat, value, curve: "linear" });
const flat = (v: number) => [kf(0, v)];
const deck = (lanes: Partial<Record<LaneId, Keyframe[]>>) => ({
  lanes: { volume: flat(1), eq_low: flat(0), eq_mid: flat(0), eq_high: flat(0), filter: flat(0), filter_res: flat(0), echo_send: flat(0), echo_feedback: flat(0.55), reverb_send: flat(0), ...lanes },
});
// A: a high-pass sweep to 0.4 with resonance riding to 0.6, a reverb wash to 0.6; B: a low-pass opening, no resonance
const plan = {
  a: deck({ filter: [kf(0, 0), kf(16, 0.4)], filter_res: [kf(0, 0), kf(16, 0.6)], reverb_send: [kf(8, 0), kf(16, 0.6), kf(20, 0)] }),
  b: deck({ filter: [kf(0, -0.5), kf(8, 0)] }),
} as unknown as MixPlan;

describe("amountOf", () => {
  it("is the deepest point of the lane over both decks, 0 when the style doesn't use it", () => {
    expect(amountOf(plan, "reverb")).toBeCloseTo(0.6);
    expect(amountOf(plan, "filter")).toBeCloseTo(0.5); // B's low-pass
    expect(amountOf(plan, "echo")).toBe(0);
  });
});

describe("setAmount", () => {
  it("scales the planner's shape so its peak becomes the level", () => {
    const next = setAmount(plan, "reverb", 0.3);
    expect(next.a.lanes.reverb_send.map((k) => [k.beat, k.value])).toEqual([[8, 0], [16, 0.3], [20, 0]]);
    expect(next.a.lanes.filter).toBe(plan.a.lanes.filter);
  });
  it("scales the filter and its resonance together on both decks", () => {
    const next = setAmount(plan, "filter", 1);
    expect(next.a.lanes.filter[1].value).toBeCloseTo(0.8);
    expect(next.a.lanes.filter_res[1].value).toBeCloseTo(1); // clamped to the lane's range
    expect(next.b.lanes.filter[0].value).toBeCloseTo(-1);
  });
  it("leaves an unused amount alone and never goes to 0", () => {
    expect(setAmount(plan, "echo", 0.8)).toBe(plan);
    expect(amountOf(setAmount(plan, "reverb", 0), "reverb")).toBeGreaterThan(0);
  });
});

describe("followFilter", () => {
  it("re-draws the resonance from the edited filter at the planner's ratio", () => {
    const edited = { ...plan, a: { ...plan.a, lanes: { ...plan.a.lanes, filter: [kf(0, 0), kf(24, 0.2)] } } } as MixPlan;
    const res = followFilter(plan, edited).a.lanes.filter_res;
    expect(res.map((k) => k.beat)).toEqual([0, 24]);
    expect(res[1].value).toBeCloseTo(0.3); // 0.2 × 0.6 / 0.4
  });
  it("keeps a deck without resonance without it, and doesn't touch decks whose filter didn't change", () => {
    const edited = { ...plan, b: { ...plan.b, lanes: { ...plan.b.lanes, filter: [kf(0, -0.5), kf(4, 0.3)] } } } as MixPlan;
    const next = followFilter(plan, edited);
    expect(next.b.lanes.filter_res.every((k) => k.value === 0)).toBe(true);
    expect(next.a).toBe(edited.a);
  });
});
