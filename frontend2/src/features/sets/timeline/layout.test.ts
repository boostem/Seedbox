import { describe, expect, it } from "vitest";
import type { SetOrderTrack, SetTimelineData } from "@/api/types";
import { wavePieces } from "./paint";
import { arcTimeAt, dropPosition, estimatePlacement, fileAt, layoutTimeline, moveTrack, setTimeOf, timeOfArc } from "./layout";

const order = (hashes: string[]): SetOrderTrack[] =>
  hashes.map((hash, position) => ({ position, hash, title: hash, artist: "", bpm: 120, key: "8A", energy: 0.5, rating: 5, shift: 0, pinned: false, target_energy: 0.5, target_bpm: null }));

const track = (hash: string, duration_s = 300) => ({ hash, duration_s, sections: [], curve: [] as [number, number][], bar_s: 2 });

const data: SetTimelineData = {
  tracks: [track("a"), track("b"), track("c")],
  transitions: [
    { from: "a", to: "b", state: "ready", technique: "long blend", bars: 16, seconds: 32, a_in_s: 260, a_out_s: 292, b_in_s: 8, b_out_s: 41 },
    { from: "b", to: "c", state: "pending" },
  ],
};

describe("layoutTimeline", () => {
  const layout = layoutTimeline(order(["a", "b", "c"]), [], data);

  it("overlaps neighbours for as long as the plan says, on alternate lanes", () => {
    const [a, b] = layout.tracks;
    const t = layout.transitions[0];
    expect([t.t0, t.t1]).toEqual([260, 292]);
    expect(a.end).toBe(292);
    expect(b.start).toBe(260);
    expect([a.lane, b.lane]).toEqual([0, 1]);
    expect(t.state).toBe("ready");
  });

  it("lands each file's beats on the master clock in the transition, and at its own speed outside it", () => {
    const b = layout.tracks[1];
    expect(fileAt(b, 260)).toBe(8);
    expect(fileAt(b, 292)).toBe(41); // pitched: 33 s of file in 32 s
    expect(fileAt(b, 302)).toBe(51);
    expect(setTimeOf(b, 51)).toBe(302);
    expect(setTimeOf(b, 24.5)).toBeCloseTo(276);
  });

  it("estimates a transition without a plan, and marks it", () => {
    const t = layout.transitions[1];
    expect(t.state).toBe("pending");
    expect(t.t1 - t.t0).toBe(32);
    expect([t.aIn, t.aOut, t.bIn, t.bOut]).toEqual([268, 300, 0, 32]);
  });

  it("treats a pair the server hasn't seen as pending, so a dropped track lays out at once", () => {
    const moved = layoutTimeline(order(["c", "a", "b"]), [], data);
    expect(moved.transitions.map((t) => t.state)).toEqual(["pending", "ready"]);
    expect(moved.total).toBeGreaterThan(0);
  });

  it("never runs backwards when two transitions want more of a track than it has", () => {
    const short: SetTimelineData = {
      tracks: [track("a"), track("b", 40), track("c")],
      transitions: [
        { from: "a", to: "b", state: "ready", seconds: 32, a_in_s: 260, a_out_s: 292, b_in_s: 0, b_out_s: 32 },
        { from: "b", to: "c", state: "ready", seconds: 32, a_in_s: 8, a_out_s: 40, b_in_s: 0, b_out_s: 32 },
      ],
    };
    const l = layoutTimeline(order(["a", "b", "c"]), [], short);
    for (const t of l.tracks) for (const p of t.pieces) expect(p.t1).toBeGreaterThan(p.t0);
    expect(l.transitions[1].t0).toBe(l.transitions[0].t1);
  });

  it("is empty without tracks, and one track is the whole set", () => {
    expect(layoutTimeline([], [], null).total).toBe(0);
    const one = layoutTimeline(order(["a"]), [], { tracks: [track("a")], transitions: [] });
    expect(one.total).toBe(300);
    expect(one.transitions).toEqual([]);
  });

  it("keeps a short track's estimate inside it", () => {
    expect(estimatePlacement(track("a", 30), order(["a"])[0], track("b")).seconds).toBe(10);
  });
});

describe("wavePieces", () => {
  const b = layoutTimeline(order(["a", "b", "c"]), [], data).tracks[1];

  it("draws a track once when its pitch is under a pixel, and piece by piece when it isn't", () => {
    expect(wavePieces(b, 0.1)).toHaveLength(1);
    expect(wavePieces(b, 0.1)[0]).toMatchObject({ t0: b.start, t1: b.end, f0: 8 });
    expect(wavePieces(b, 50)).toEqual(b.pieces);
  });
});

describe("arc time", () => {
  const layout = layoutTimeline(order(["a", "b", "c"]), [], data);

  it("counts tracks: each track's middle is its position's place on the arc", () => {
    const mid = (i: number) => (layout.tracks[i].start + layout.tracks[i].end) / 2;
    expect(arcTimeAt(layout, mid(0))).toBe(0);
    expect(arcTimeAt(layout, mid(1))).toBeCloseTo(0.5);
    expect(arcTimeAt(layout, mid(2))).toBe(1);
    expect(arcTimeAt(layout, 0)).toBe(0);
    expect(arcTimeAt(layout, layout.total)).toBe(1);
  });

  it("goes back and forth", () => {
    for (const t of [0, 0.2, 0.5, 0.9, 1]) expect(arcTimeAt(layout, timeOfArc(layout, t))).toBeCloseTo(t);
  });
});

describe("dragging", () => {
  const layout = layoutTimeline(order(["a", "b", "c"]), [], data);

  it("lands by the other tracks' middles", () => {
    expect(dropPosition(layout, 0, layout.total)).toBe(2);
    expect(dropPosition(layout, 2, 0)).toBe(0);
    expect(dropPosition(layout, 0, 1)).toBe(0);
  });

  it("moves a track", () => {
    expect(moveTrack(["a", "b", "c", "d"], 0, 2)).toEqual(["b", "c", "a", "d"]);
    expect(moveTrack(["a", "b", "c", "d"], 3, 0)).toEqual(["d", "a", "b", "c"]);
    expect(moveTrack(["a", "b", "c"], 1, 1)).toBeNull();
  });

  it("flows around pins, and a pinned track stays", () => {
    expect(moveTrack(["a", "b", "c", "d"], 0, 3, new Set([1]))).toEqual(["c", "b", "d", "a"]);
    expect(moveTrack(["a", "b", "c", "d"], 1, 3, new Set([1]))).toBeNull();
    expect(moveTrack(["a", "b", "c", "d"], 3, 0, new Set([0]))).toEqual(["a", "d", "b", "c"]);
  });
});
