import { describe, expect, it } from "vitest";
import { applyJobEvent, queuedByKind, queueEta, type RunningJob } from "./jobs";

interface S {
  running: Record<string, RunningJob>;
  queued: Record<string, string>;
  seconds: Record<string, number>;
  pools: Record<string, string>;
  workers: Record<string, number>;
}
const base = (): S => ({ running: {}, queued: {}, seconds: {}, pools: { waveform: "light", stems: "heavy" }, workers: { light: 2, heavy: 1 } });

describe("the analysis queue", () => {
  it("follows jobs from queued to done and learns how long they take", () => {
    let s = base();
    const apply = (type: string, data: unknown, t: number) => {
      s = { ...s, ...applyJobEvent(s, type, data, t) };
    };
    apply("job.queued", { id: "1", kind: "stems", payload: { hash: "h" } }, 0);
    apply("job.queued", { id: "2", kind: "waveform", payload: { hash: "h" } }, 0);
    expect(queuedByKind(s.queued)).toEqual({ stems: 1, waveform: 1 });
    apply("job.started", { id: "1", kind: "stems", payload: { hash: "h" } }, 1000);
    expect(s.running["1"]).toMatchObject({ kind: "stems", hash: "h", progress: 0, seenStart: true });
    expect(Object.keys(s.queued)).toEqual(["2"]);
    apply("job.progress", { id: "1", kind: "stems", progress: 0.5 }, 21000);
    expect(s.running["1"].progress).toBe(0.5);
    apply("job.done", { id: "1", kind: "stems", payload: { hash: "h" } }, 41000);
    expect(s.running).toEqual({});
    expect(s.seconds.stems).toBe(40);
    apply("job.cancelled", { id: "2", kind: "waveform" }, 42000);
    expect(s.queued).toEqual({});
    expect(applyJobEvent(s, "job.bumped", { ids: [] })).toBeNull();
  });

  it("picks up a job already running when we start listening, without learning from it", () => {
    let s = base();
    s = { ...s, ...applyJobEvent(s, "job.progress", { id: "9", kind: "stems", progress: 0.8 }, 0) };
    expect(s.running["9"]).toMatchObject({ seenStart: false, progress: 0.8 });
    s = { ...s, ...applyJobEvent(s, "job.done", { id: "9", kind: "stems" }, 5000) };
    expect(s.seconds).toEqual({});
  });

  it("estimates the time left per pool, the slowest pool's", () => {
    const s = base();
    expect(queueEta(s, 0)).toBeNull();
    // 4 waveforms (0.5 s each) over 2 light workers, 2 stems (60 s) on the heavy one
    const queued = { a: "waveform", b: "waveform", c: "waveform", d: "waveform", e: "stems", f: "stems" };
    expect(queueEta({ ...s, queued }, 0)).toBe(120);
    // a running stems job half done after 30 s has about 30 s left
    const running = { r: { id: "r", kind: "stems", hash: "h", progress: 0.5, startedAt: 0, seenStart: true } };
    expect(queueEta({ ...s, queued: { e: "stems" }, running }, 30000)).toBeCloseTo(90);
    // learnt times win over the defaults
    expect(queueEta({ ...s, queued: { e: "stems" }, seconds: { stems: 100 } }, 0)).toBe(100);
  });
});
