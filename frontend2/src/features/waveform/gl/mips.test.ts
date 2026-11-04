import { describe, expect, it } from "vitest";
import { MIN_LEVEL, mipLevel, waveAtlas, waveMips } from "./mips";

const wave = (n: number, f: (i: number) => [number, number, number]) => {
  const low = new Float32Array(n);
  const mid = new Float32Array(n);
  const high = new Float32Array(n);
  for (let i = 0; i < n; i++) [low[i], mid[i], high[i]] = f(i);
  return { n, low, mid, high };
};

describe("waveform mips", () => {
  it("stores the bands as bytes with the loudest in alpha", () => {
    const [l0] = waveMips(wave(4, (i) => [i / 3, 1, 0]));
    expect(Array.from(l0.slice(0, 8))).toEqual([0, 255, 0, 255, 85, 255, 0, 255]);
  });

  it("keeps peaks, not averages, at every level", () => {
    // one loud point (a kick) in 36,000 points: every level still has it at full height
    const levels = waveMips(wave(36000, (i) => (i === 12345 ? [1, 0.5, 0.25] : [0.1, 0.1, 0.1])));
    for (const lv of levels) {
      let top = 0;
      for (let i = 0; i < lv.length; i += 4) top = Math.max(top, lv[i]);
      expect(top).toBe(255);
    }
  });

  it("halves each level until it fits MIN_LEVEL points, odd lengths included", () => {
    const levels = waveMips(wave(36001, () => [0.5, 0.5, 0.5]));
    const lens = levels.map((l) => l.length / 4);
    expect(lens[0]).toBe(36001);
    expect(lens[1]).toBe(18001);
    for (let k = 1; k < lens.length; k++) expect(lens[k]).toBe(Math.ceil(lens[k - 1] / 2));
    expect(lens.at(-1)!).toBeLessThanOrEqual(MIN_LEVEL);
    expect(lens.at(-2)!).toBeGreaterThan(MIN_LEVEL);
    // a 4-minute track at 150 points/s has a level near 1,200 columns for the overview
    expect(lens.some((n) => n >= 1000 && n <= 2400)).toBe(true);
  });

  it("packs levels into rows of an atlas narrower than the GPU's limit", () => {
    const n = 1_080_000; // 2 hours at 150 points/s
    const atlas = waveAtlas(wave(n, (i) => [(i % 7) / 7, 0, 0]), 4096);
    expect(atlas.width).toBe(4096);
    expect(atlas.rows[0]).toBe(0);
    expect(atlas.rows[1]).toBe(Math.ceil(n / 4096));
    expect(atlas.data.length).toBe(atlas.width * atlas.height * 4);
    // point i of level 0 sits at (i % width, floor(i / width))
    const i = 500_003;
    const at = ((Math.floor(i / 4096) * 4096 + (i % 4096)) * 4) | 0;
    expect(atlas.data[at]).toBe(Math.round(((i % 7) / 7) * 255));
  });

  it("picks the level with one to two points per column", () => {
    expect(mipLevel(0.3, 8)).toBe(0);
    expect(mipLevel(1, 8)).toBe(0);
    expect(mipLevel(4, 8)).toBe(2);
    expect(mipLevel(6, 8)).toBeCloseTo(Math.log2(6));
    expect(mipLevel(1e6, 8)).toBe(7);
  });
});
