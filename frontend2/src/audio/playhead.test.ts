import { describe, expect, it } from "vitest";
import { centredStart, ClockAnchor, FrameSmoother, traceJitter } from "./playhead";

const FRAME = 1000 / 60;

/** 10 s of frames with the clock moving in steps of `stepS`, as an audio output buffer does. */
function stepped(stepS: number, smoother = new FrameSmoother()) {
  const trace = { frames: [] as number[], clock: [] as number[], head: [] as number[] };
  for (let i = 0; i < 600; i++) {
    const ms = i * FRAME;
    const clock = 5 + Math.floor(ms / 1000 / stepS) * stepS;
    trace.frames.push(ms);
    trace.clock.push(clock);
    trace.head.push(smoother.at(clock, ms));
  }
  return trace;
}

describe("FrameSmoother", () => {
  it("evens out a clock that moves in output-buffer steps", () => {
    for (const step of [0.003, 0.0107, 0.0213, 0.0427]) {
      const t = stepped(step);
      const j = traceJitter(t);
      expect(j.head_jitter_ms, `step ${step}`).toBeLessThan(j.clock_jitter_ms / 3);
      expect(j.head_jitter_ms, `step ${step}`).toBeLessThan(1);
    }
  });

  it("stays with the clock", () => {
    const t = stepped(0.0213);
    for (let i = 60; i < t.head.length; i++) expect(Math.abs(t.head[i] - t.clock[i])).toBeLessThan(0.03);
  });

  it("jumps on a seek and after a reset", () => {
    const s = new FrameSmoother();
    s.at(1, 0);
    expect(s.at(1.016, 16)).toBeCloseTo(1.016, 3);
    expect(s.at(60, 32)).toBe(60);
    s.reset();
    expect(s.at(60.01, 1000)).toBe(60.01);
  });
});

describe("ClockAnchor", () => {
  it("runs on the context's clock, not the media position's noise", () => {
    const a = new ClockAnchor();
    expect(a.read(10, 100)).toBe(10);
    const noisy = a.read(10.03, 100.01); // the element says 30 ms, the context 10 ms
    expect(noisy).toBeCloseTo(10.01, 3);
  });

  it("follows slow drift and restarts on a jump", () => {
    const a = new ClockAnchor();
    let t = a.read(10, 100);
    for (let i = 1; i <= 2000; i++) t = a.read(10 + i * 0.016 * 1.001, 100 + i * 0.016);
    expect(Math.abs(t - (10 + 2000 * 0.016 * 1.001))).toBeLessThan(0.005);
    expect(a.read(50, 200)).toBe(50);
    a.reset();
    expect(a.read(51, 300)).toBe(51);
  });
});

describe("centredStart", () => {
  it("is a whole number of device columns, so column contents don't change as it scrolls", () => {
    for (const t of [0, 1.23456, 77.7]) {
      const start = centredStart(t, 700, 46.5, 2);
      expect((start * 93) % 1 === 0 || Math.abs(((start * 93) % 1) - Math.round((start * 93) % 1)) < 1e-6).toBe(true);
      expect(Math.abs(start + 700 / 93 - t)).toBeLessThanOrEqual(0.5 / 93 + 1e-9);
    }
  });

  it("runs past the start of the track", () => {
    expect(centredStart(0, 700, 50, 1)).toBe(-14);
  });
});
