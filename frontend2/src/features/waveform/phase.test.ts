import { describe, expect, it } from "vitest";
import { beatPhase, fmtBarsBeats } from "./phase";

const grid = { first_downbeat_s: 1, bar_s: 2, bar_count: 100, phrase_offset: 0 };

describe("beatPhase", () => {
  it("places a time in the bar and the beat", () => {
    expect(beatPhase(grid, 1)).toEqual({ bar: 0, beat: 0, frac: 0, beatsToPhrase: 32, phraseLevel: null });
    const p = beatPhase(grid, 1 + 2 * 3 + 0.75)!; // bar 3, half way through beat 2 (0-based 1)
    expect([p.bar, p.beat, p.frac]).toEqual([3, 1, 0.5]);
    expect(p.beatsToPhrase).toBe(19); // bar 8 is 4.625 bars away: 18.5 beats, rounded up
  });

  it("before the first downbeat, the bar is negative and the phase still runs forward", () => {
    const p = beatPhase(grid, 0.5)!;
    expect(p.bar).toBe(-1);
    expect(p.beat).toBe(3);
    expect(p.frac).toBe(0);
    expect(p.beatsToPhrase).toBe(1);
  });

  it("counts to the grid's phrases with an offset, and stops after the last", () => {
    const g = { ...grid, phrase_offset: 3 };
    expect(beatPhase(g, 1)!.beatsToPhrase).toBe(12); // to bar 3
    expect(beatPhase(g, 1 + 2 * 3)!.beatsToPhrase).toBe(32); // on a phrase: the next one
    expect(beatPhase({ ...g, bar_count: 10 }, 1 + 2 * 3)!.beatsToPhrase).toBeNull(); // bar 11 is past the end
  });

  it("counts to analysed phrases and says their length", () => {
    const g = { ...grid, phrases: [{ bar: 0, level: 16 as const }, { bar: 16, level: 8 as const }, { bar: 24, level: 32 as const }] };
    expect(beatPhase(g, 1)).toMatchObject({ beatsToPhrase: 64, phraseLevel: 8 });
    expect(beatPhase(g, 1 + 2 * 16)).toMatchObject({ beatsToPhrase: 32, phraseLevel: 32 });
    expect(beatPhase(g, 1 + 2 * 24)).toMatchObject({ beatsToPhrase: null, phraseLevel: null });
  });

  it("has nothing to say without a grid", () => {
    expect(beatPhase({ ...grid, bar_s: 0 }, 10)).toBeNull();
  });
});

describe("fmtBarsBeats", () => {
  it("writes bars.beats", () => {
    expect(fmtBarsBeats(30)).toBe("7.2");
    expect(fmtBarsBeats(32)).toBe("8.0");
    expect(fmtBarsBeats(1)).toBe("0.1");
  });
});
