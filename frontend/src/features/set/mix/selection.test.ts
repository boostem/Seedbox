import { describe, expect, it } from "vitest";
import type { Keyframe, MixPlan } from "@/api/types";
import type { Sel } from "./daw";
import { deleteKeyframes, moveSelection, pruneSelection } from "./selection";

const kf = (beat: number, value: number): Keyframe => ({ beat, value, curve: "linear" });
const deck = (volume: Keyframe[]) => ({ native_bpm: 124, lanes: { volume } });
const plan = { window: { start_beat: 0, end_beat: 64 }, tempo: [kf(0, 124)], a: deck([kf(0, 1), kf(16, 0.5), kf(32, 0)]), b: deck([kf(0, 0), kf(32, 1)]) } as unknown as MixPlan;
const sel = (entries: [string, number[]][]): Sel => new Map(entries.map(([k, i]) => [k as never, new Set(i)]));

describe("moveSelection", () => {
  it("moves the selected keyframe on the beat grid, re-sorts the lane and remaps the selection", () => {
    const res = moveSelection(plan, sel([["a.volume", [0]]]), 20.4, 0, 1, false);
    expect(res.plan.a.lanes.volume.map((k) => k.beat)).toEqual([16, 20, 32]);
    expect([...res.sel.get("a.volume")!]).toEqual([1]);
  });
  it("clamps values to the lane's range", () => {
    const res = moveSelection(plan, sel([["a.volume", [1]]]), 0, 5, 1, false);
    expect(res.plan.a.lanes.volume[1].value).toBe(1);
  });
});

describe("pruneSelection", () => {
  it("keeps the same selection when every index still exists", () => {
    const s = sel([["b.volume", [0, 1]]]);
    expect(pruneSelection(s, plan)).toBe(s);
  });
  it("drops indexes past the end of a lane", () => {
    expect([...pruneSelection(sel([["b.volume", [1, 4]]]), plan).get("b.volume")!]).toEqual([1]);
  });
});

describe("deleteKeyframes", () => {
  it("removes the selected keyframes but keeps a lane's first one", () => {
    const next = deleteKeyframes(plan, sel([["a.volume", [1]], ["b.volume", [0, 1]]]));
    expect(next.a.lanes.volume.map((k) => k.beat)).toEqual([0, 32]);
    expect(next.b.lanes.volume.map((k) => k.beat)).toEqual([0]);
  });
});
