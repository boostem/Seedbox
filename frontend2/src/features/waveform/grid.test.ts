import { describe, expect, it } from "vitest";
import { barAt, barTime, beatAt, beatTime, phraseLevel, phraseStarts } from "./grid";
import { slotLetter, styleOf } from "./palette";

const constant = { first_downbeat_s: 0.5, bar_s: 2, bar_count: 64, phrase_offset: 0 };

describe("grid", () => {
  it("places bars on a constant grid", () => {
    expect(barTime(constant, 0)).toBe(0.5);
    expect(barTime(constant, 3)).toBe(6.5);
    expect(beatTime(constant, 2)).toBe(1.5);
    expect(barAt(constant, 6.5)).toBe(3);
  });

  it("follows a piecewise grid across a tempo change, counting bars from the first downbeat", () => {
    // 120 BPM (0.5 s a beat) from beat 0 at 0.25 s, then 60 BPM from beat 18 at 9.25 s; the downbeat is beat 2
    const an = { ...constant, first_downbeat_s: 1.25, grid: [{ start_beat: 0, start_s: 0.25, bpm: 120 }, { start_beat: 18, start_s: 9.25, bpm: 60 }] };
    expect(barTime(an, 0)).toBeCloseTo(1.25);
    expect(barTime(an, 4)).toBeCloseTo(9.25); // beat 18: where the tempo changes
    expect(barTime(an, 5)).toBeCloseTo(13.25); // a bar of 1 s beats after it
    expect(barAt(an, 11.25)).toBeCloseTo(4.5);
    for (const t of [0, 3.3, 9.25, 20]) expect(beatTime(an, beatAt(an, t))).toBeCloseTo(t);
  });

  it("weighs phrase starts by the phrase they open", () => {
    expect([0, 8, 16, 24, 32, 3].map((k) => phraseLevel(constant, k))).toEqual([32, 8, 16, 8, 32, 0]);
    const analysed = { ...constant, phrases: [{ bar: 2, level: 16 as const }, { bar: 18, level: 32 as const }, { bar: 50, level: 8 as const }] };
    expect([2, 18, 50, 10].map((k) => phraseLevel(analysed, k))).toEqual([16, 32, 8, 0]);
    expect(phraseStarts(analysed)).toEqual([2, 18, 50]);
    expect(phraseStarts({ ...constant, bar_count: 20, phrase_offset: 4 })).toEqual([4, 12]);
  });
});

describe("palette helpers", () => {
  it("colours a transition by its style", () => {
    expect(["long blend", "phrase_blend", "bass swap", "drop swap", "filter sweep", "echo out", "blend → echo out", "quick cut", "something new"].map(styleOf))
      .toEqual(["blend", "blend", "swap", "swap", "filter", "echo", "echo", "cut", "blend"]);
    expect([0, 7, 8].map(slotLetter)).toEqual(["A", "H", "A"]);
  });
});
