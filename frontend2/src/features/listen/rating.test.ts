import { describe, expect, it } from "vitest";
import { interval, verdict, winRate, withScore } from "./rating";

const rate = (win_rate: number | null, ci_low: number, ci_high: number) => ({ trials: 10, wins: 5, win_rate, ci_low, ci_high });

describe("withScore", () => {
  it("sets a score per slot without touching the old object", () => {
    const before = { a: { energy: 2 } };
    expect(withScore(before, "b", "dj", 4)).toEqual({ a: { energy: 2 }, b: { dj: 4 } });
    expect(withScore(before, "a", "energy", 5)).toEqual({ a: { energy: 5 } });
    expect(before).toEqual({ a: { energy: 2 } });
  });

  it("clears a score picked twice", () => {
    expect(withScore({ a: { energy: 2 } }, "a", "energy", 2)).toEqual({ a: {} });
  });
});

describe("the numbers", () => {
  it("formats rates and intervals", () => {
    expect(winRate(rate(0.756, 0.5, 0.9))).toBe("76%");
    expect(interval(rate(0.756, 0.504, 0.9))).toBe("50%–90%");
    expect(winRate(rate(null, 0, 1))).toBe("n/a");
    expect(interval(rate(null, 0, 1))).toBe("n/a");
  });

  it("only calls a result when the interval leaves out a coin flip", () => {
    expect(verdict(rate(0.8, 0.55, 0.95))).toBe("better");
    expect(verdict(rate(0.2, 0.05, 0.45))).toBe("worse");
    expect(verdict(rate(0.7, 0.4, 0.9))).toBe("unclear");
    expect(verdict(rate(null, 0, 1))).toBe("unclear");
  });
});
