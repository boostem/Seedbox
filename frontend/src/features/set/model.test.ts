import { describe, expect, it } from "vitest";
import type { CrateSet, SetTrack, Track, Transition } from "@/api/types";
import {
  headline,
  moveBy,
  orderOf,
  pairKey,
  pitchPct,
  placeNextTo,
  placeholderTransition,
  sameOrder,
  setStats,
  setTechnique,
  techniqueLong,
  techniqueShort,
  tempoMismatch,
  withOrder,
} from "./model";

function track(id: string, over: Partial<Track> = {}): Track {
  return { id, display_name: id, camelot: "8A", bpm: 120, duration_s: 240, ...over } as Track;
}

function setTrack(t: Track, order: number): SetTrack {
  return { track: t, order, analysis_state: "ready" } as SetTrack;
}

function transition(from: string, to: string, over: Partial<Transition> = {}): Transition {
  return {
    from_id: from,
    to_id: to,
    score: 80,
    key_move: "same",
    pitch_pct: 0,
    out_start_s: 200,
    in_start_s: 0,
    overlap_bars: 8,
    swap_bar: 5,
    technique: "bass swap",
    notes: [],
    warnings: [],
    ready: true,
    ...over,
  };
}

function crateSet(tracks: Track[], transitions?: Transition[]): CrateSet {
  return {
    folder: "f",
    path: "/f",
    seed_id: null,
    tracks: tracks.map((t, i) => setTrack(t, i + 1)),
    transitions: transitions ?? tracks.slice(1).map((t, i) => transition(tracks[i].id, t.id)),
  };
}

describe("pitchPct", () => {
  it("matches B's tempo to A", () => {
    expect(pitchPct(126, 120)).toBe(5);
    expect(pitchPct(120, 126)).toBe(-4.76);
  });
  it("uses half/double time when closer", () => {
    expect(pitchPct(128, 64)).toBe(0);
    expect(pitchPct(87, 174)).toBe(0);
  });
  it("is 0 for unknown tempos", () => {
    expect(pitchPct(0, 120)).toBe(0);
    expect(pitchPct(120, Number.NaN)).toBe(0);
  });
});

describe("placeholderTransition", () => {
  it("fills key move and pitch, marks it pending", () => {
    const t = placeholderTransition(track("a", { camelot: "8A", bpm: 126 }), track("b", { camelot: "9A", bpm: 120 }));
    expect(t).toMatchObject({ from_id: "a", to_id: "b", key_move: "+1", pitch_pct: 5, ready: false, pending: true });
  });
});

describe("withOrder", () => {
  const [a, b, c] = ["a", "b", "c"].map((id) => track(id));
  const set = crateSet([a, b, c]);

  it("re-numbers tracks and reuses known transitions", () => {
    const next = withOrder(set, ["b", "c", "a"], new Map());
    expect(orderOf(next)).toEqual(["b", "c", "a"]);
    expect(next.tracks.map((t) => t.order)).toEqual([1, 2, 3]);
    expect(next.transitions[0]).toBe(set.transitions[1]); // b>c is known
    expect(next.transitions[1]).toMatchObject({ from_id: "c", to_id: "a", pending: true });
  });

  it("prefers the session cache over a placeholder", () => {
    const cached = transition("c", "a", { score: 42 });
    const next = withOrder(set, ["b", "c", "a"], new Map([[pairKey("c", "a"), cached]]));
    expect(next.transitions[1]).toBe(cached);
  });

  it("ignores unknown/duplicate ids and appends missing tracks in order", () => {
    expect(orderOf(withOrder(set, ["c", "zz", "c"], new Map()))).toEqual(["c", "a", "b"]);
  });

  it("keeps unchanged track objects", () => {
    expect(withOrder(set, ["a", "b", "c"], new Map()).tracks[0]).toBe(set.tracks[0]);
  });
});

describe("ordering helpers", () => {
  const ids = ["a", "b", "c", "d"];
  it("orderOf / sameOrder", () => {
    expect(orderOf(null)).toEqual([]);
    expect(sameOrder(ids, [...ids])).toBe(true);
    expect(sameOrder(ids, ["a", "b", "d", "c"])).toBe(false);
    expect(sameOrder(ids, ["a"])).toBe(false);
  });
  it("moveBy clamps to the ends", () => {
    expect(moveBy(ids, "b", 1)).toEqual(["a", "c", "b", "d"]);
    expect(moveBy(ids, "b", -5)).toEqual(["b", "a", "c", "d"]);
    expect(moveBy(ids, "d", 1)).toBe(ids);
    expect(moveBy(ids, "zz", 1)).toBe(ids);
  });
  it("placeNextTo", () => {
    expect(placeNextTo(ids, "a", "c", "after")).toEqual(["b", "c", "a", "d"]);
    expect(placeNextTo(ids, "d", "a", "before")).toEqual(["d", "a", "b", "c"]);
    expect(placeNextTo(ids, "a", "a", "after")).toBe(ids);
    expect(placeNextTo(ids, "a", "zz", "after")).toBe(ids);
  });
});

describe("setStats", () => {
  // 120 BPM: one bar = 2 s
  const [a, b, c] = [track("a", { bpm: 120 }), track("b", { bpm: 124 }), track("c", { bpm: 0 })];

  it("sums durations and estimates the mixed length", () => {
    const s = setStats(crateSet([a, b], [transition("a", "b", { out_start_s: 200, in_start_s: 10, overlap_bars: 8 })]));
    expect(s).toMatchObject({ count: 2, totalS: 480, avgScore: 80, bpmMin: 120, bpmMax: 124, scored: 1, ready: 1 });
    // A plays to 200 + 16 = 216, B from 10 to 240 = 230, minus the 16 s overlap
    expect(s.mixedS).toBe(216 + 230 - 16);
  });

  it("finds the worst transition and skips pending ones", () => {
    const set = crateSet([a, b, c], [transition("a", "b", { score: 70 }), transition("b", "c", { score: 30 })]);
    expect(setStats(set).worst?.index).toBe(1);
    const pending = withOrder(set, ["a", "c", "b"], new Map());
    const s = setStats(pending);
    expect(s.scored).toBe(0);
    expect(s.worst).toBeNull();
    expect(s.avgScore).toBeNull();
    expect(s.mixedS).toBeNull();
    expect(s.bpmMin).toBe(120); // c's 0 BPM ignored
  });

  it("handles an empty set", () => {
    expect(setStats(crateSet([]))).toMatchObject({ count: 0, totalS: 0, mixedS: null, worst: null, bpmMin: null });
  });
});

describe("technique labels", () => {
  it.each([
    [{ technique: null, overlap_bars: 8, swap_bar: 5 }, null, null],
    [{ technique: "bass swap", overlap_bars: 8, swap_bar: 5 }, "8-bar bass swap", "8-bar blend, swap bass at bar 5"],
    [{ technique: "bass swap", overlap_bars: 8, swap_bar: null }, "8-bar bass swap", "8-bar bass swap"],
    [{ technique: "echo out", overlap_bars: 8, swap_bar: 5 }, "8-bar blend → echo out", "Blend 8 bars, swap bass at bar 5, then echo A out"],
    [{ technique: "echo out", overlap_bars: 0, swap_bar: null }, "echo out", "Echo A out, B in on the 1"],
    [{ technique: "quick cut", overlap_bars: null, swap_bar: null }, "quick cut", "Quick cut"],
  ] as const)("%o", (t, short, long) => {
    const fields = t as Parameters<typeof techniqueShort>[0];
    expect(techniqueShort(fields)).toBe(short);
    expect(techniqueLong(fields)).toBe(long);
  });
});

describe("setTechnique", () => {
  const base = { technique: "long blend", overlap_bars: 16, swap_bar: 9 } as const;
  it("falls back to the pair's own suggestion without a set plan", () => {
    expect(setTechnique(transition("a", "b", base))).toMatchObject(base);
  });
  it("shows the set plan's technique; a cut has no blend and no bass swap", () => {
    const t = transition("a", "b", { ...base, plan_technique: "reverb wash", plan_bars: 0 });
    expect(setTechnique(t)).toEqual({ technique: "reverb wash", overlap_bars: 0, swap_bar: null });
    expect(techniqueShort(setTechnique(t))).toBe("reverb wash");
  });
  it("keeps the swap bar for a blend of the suggestion's length", () => {
    const t = transition("a", "b", { ...base, plan_technique: "echo out", plan_bars: 16 });
    expect(techniqueShort(setTechnique(t))).toBe("16-bar blend → echo out");
    expect(techniqueLong(setTechnique(t))).toBe("Blend 16 bars, swap bass at bar 9, then echo A out");
  });
});

describe("headline", () => {
  it("returns the first note without its period", () => {
    expect(headline(transition("a", "b", { notes: ["Same key.", "Other."] }))).toBe("Same key");
    expect(headline(transition("a", "b"))).toBeNull();
  });
});

describe("tempoMismatch", () => {
  it("flags detected tempos that aren't ×½/×1/×2 within 3 %", () => {
    expect(tempoMismatch(128, { bpm: 100, bpm_source: "detected" })).toBe(true);
    expect(tempoMismatch(128, { bpm: 130, bpm_source: "detected" })).toBe(false);
    expect(tempoMismatch(128, { bpm: 64.5, bpm_source: "detected" })).toBe(false);
    expect(tempoMismatch(87, { bpm: 174, bpm_source: "detected" })).toBe(false);
  });
  it("only applies to detected tempos", () => {
    expect(tempoMismatch(128, { bpm: 100, bpm_source: "tunebat" })).toBe(false);
    expect(tempoMismatch(128, null)).toBe(false);
    expect(tempoMismatch(0, { bpm: 100, bpm_source: "detected" })).toBe(false);
  });
});
