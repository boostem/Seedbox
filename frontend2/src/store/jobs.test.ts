import { describe, expect, it } from "vitest";
import { applyJobEvent, type JobsState, jobsByTrack, pendingTracks, queuedByKind, queueEta } from "./jobs";

type S = JobsState;
const base = (): S => ({ running: {}, queued: {}, queuedHash: {}, seconds: {}, pools: { waveform: "light", stems: "heavy" }, workers: { light: 2, heavy: 1 }, pausedPools: [] });

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
    expect(queueEta({ ...s, queued: { e: "stems" }, pausedPools: ["heavy"] }, 0)).toBeNull(); // low-power mode: no end in sight
    // learnt times win over the defaults
    expect(queueEta({ ...s, queued: { e: "stems" }, seconds: { stems: 100 } }, 0)).toBe(100);
  });

  it("counts the tracks with work left, and notices when only stems are", () => {
    let s = base();
    const apply = (type: string, data: unknown) => {
      s = { ...s, ...applyJobEvent(s, type, data, 0) };
    };
    expect(pendingTracks(s)).toEqual({ tracks: new Set(), stemsOnly: false, stemsProgress: null });
    apply("job.queued", { id: "1", kind: "beats", payload: { hash: "a" } });
    apply("job.queued", { id: "2", kind: "stems", payload: { hash: "a" } });
    apply("job.queued", { id: "3", kind: "stems", payload: { hash: "b" } });
    apply("job.queued", { id: "4", kind: "set_export", payload: { set_id: "s" } }); // about no track
    expect(s.queuedHash).toEqual({ "1": "a", "2": "a", "3": "b" });
    expect(pendingTracks(s)).toMatchObject({ tracks: new Set(["a", "b"]), stemsOnly: false });
    apply("job.started", { id: "1", kind: "beats" }); // the hash comes from the queued event
    expect(s.running["1"].hash).toBe("a");
    expect(jobsByTrack(s).get("a")).toEqual({ running: [{ kind: "beats", progress: 0 }], queued: 1 });
    apply("job.done", { id: "1", kind: "beats" });
    apply("job.started", { id: "2", kind: "stems", payload: { hash: "a" } });
    apply("job.progress", { id: "2", kind: "stems", progress: 0.4 });
    expect(pendingTracks(s)).toEqual({ tracks: new Set(["a", "b"]), stemsOnly: true, stemsProgress: 0.4 });
    apply("job.done", { id: "2", kind: "stems" });
    apply("job.failed", { id: "3", kind: "stems" });
    expect(pendingTracks(s).tracks.size).toBe(0);
    expect(s.queuedHash).toEqual({});
  });
});
