import { describe, expect, it, vi } from "vitest";
import { LANE_DEFAULTS, LANES } from "@/api/mixplan.generated";
import type { Deck, Lanes, MixPlan } from "@/api/mixplan.generated";
import type { SetOrderTrack, SetTimelineData } from "@/api/types";
import type { EngineState } from "@/audio/engine";
import type { DeckCarry } from "@/audio/engine/core";
import { beatAtFile } from "@/audio/engine/position";
import { layoutTimeline } from "./layout";
import { beatAt, segmentIndexAt, segmentOf, type SetEngine, SetPlayback, timeAt } from "./playback";

const BPM = 120; // 2 beats a second
const DUR = 300;
const OVERLAP = 32;

const order = (hashes: string[]): SetOrderTrack[] =>
  hashes.map((hash, position) => ({ position, hash, title: hash, artist: "", bpm: BPM, key: "8A", energy: 0.5, rating: 5, shift: 0, pinned: false, target_energy: 0.5, target_bpm: null }));

/** Master beat 0 is where B comes in: A's 268 s, B's 0 s. */
function deck(id: string, start_s: number, from: number, to: number): Deck {
  const lanes = Object.fromEntries(LANES.map((n) => [n, [{ beat: 0, value: LANE_DEFAULTS[n], curve: "linear" as const }]])) as unknown as Lanes;
  return { track_id: id, grid: [{ start_beat: 0, start_s, bpm: BPM }], key_shift: 0, play_from_beat: from, play_to_beat: to, lanes, stem_lanes: null, sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "pre_eq" }, loops: [], fx: [], pinned: [] };
}

function plan(a: string, b: string): MixPlan {
  return {
    version: 3,
    technique: "t",
    bars: 16,
    key_lock: true,
    window: { start_beat: -32, end_beat: 96 },
    tempo: [{ beat: 0, value: BPM, curve: "hold" }, { beat: 80, value: BPM, curve: "hold" }],
    a: deck(a, 268, -536, 64),
    b: deck(b, 0, 0, 600),
    markers: [{ beat: 8, label: "swap" }],
    notes: [],
    edited: false,
  };
}

const data: SetTimelineData = {
  tracks: ["a", "b", "c"].map((hash) => ({ hash, duration_s: DUR, sections: [], curve: [], bar_s: 2 })),
  transitions: [
    { from: "a", to: "b", state: "ready", seconds: OVERLAP, a_in_s: 268, a_out_s: 300, b_in_s: 0, b_out_s: 32 },
    { from: "b", to: "c", state: "ready", seconds: OVERLAP, a_in_s: 268, a_out_s: 300, b_in_s: 0, b_out_s: 32 },
  ],
};

const layout = layoutTimeline(order(["a", "b", "c"]), [], data);

describe("beats and the set's seconds", () => {
  const s = segmentOf(layout, 0, plan("a", "b"));

  it("opens the plan out to the whole of the first track", () => {
    expect(s.plan.window.start_beat).toBe(-536);
    expect(s.plan.a.play_from_beat).toBe(-536);
    expect(s.beats).toEqual([0, 64]);
    expect(s.times).toEqual([268, 300]);
  });

  it("goes both ways, through the body, the blend and the next body", () => {
    for (const t of [0, 100, 268, 280, 300, 400, 560]) expect(timeAt(s, beatAt(s, t))).toBeCloseTo(t, 6);
    expect(beatAt(s, 268)).toBe(0);
    expect(beatAt(s, 300)).toBe(64);
    expect(beatAt(s, 0)).toBe(-536);
  });

  it("hands over once A's gone and the tempo has settled, and before the next blend", () => {
    expect(s.handOverBeat).toBe(80);
    const late = segmentOf(layout, 0, { ...plan("a", "b"), tempo: [{ beat: 0, value: BPM, curve: "hold" }, { beat: 10_000, value: BPM, curve: "hold" }] });
    expect(late.handOverBeat).toBe(beatAtFile(late.plan.b, 268)); // where B's next transition starts
    expect(late.plan.window.end_beat).toBeGreaterThan(late.handOverBeat);
  });

  it("lets the last track play out", () => {
    const last = segmentOf(layout, 1, plan("b", "c"));
    expect(last.handOverBeat).toBe(Infinity);
    expect(last.plan.window.end_beat).toBe(600);
  });

  it("finds the transition that plays at a time", () => {
    expect(segmentIndexAt(layout, 0)).toBe(0);
    expect(segmentIndexAt(layout, 280)).toBe(0);
    expect(segmentIndexAt(layout, 300)).toBe(1);
    expect(segmentIndexAt(layout, layout.total)).toBe(1);
  });
});

class FakeEngine implements SetEngine {
  state: EngineState = { status: "idle", decks: { a: null, b: null }, error: null };
  now = 0;
  scheduled: { plan: MixPlan; carry?: DeckCarry }[] = [];
  seeks: number[] = [];
  private listeners = new Set<(s: EngineState) => void>();
  subscribe(l: (s: EngineState) => void) {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }
  emit(status: EngineState["status"]) {
    this.state = { ...this.state, status };
    for (const l of this.listeners) l(this.state);
  }
  load = vi.fn(() => Promise.resolve());
  schedule(plan: MixPlan, carry?: DeckCarry) {
    this.scheduled.push({ plan, carry });
  }
  play = vi.fn(() => Promise.resolve());
  pause = vi.fn();
  seek(b: number) {
    this.seeks.push(b);
    this.now = b;
  }
  beat() {
    return this.now;
  }
  close = vi.fn(() => Promise.resolve());
}

const flush = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve();
};

function setup(plans: (from: string, to: string) => Promise<MixPlan> = (a, b) => Promise.resolve(plan(a, b))) {
  const engine = new FakeEngine();
  const make = vi.fn(() => Promise.resolve(engine as SetEngine));
  const beforePlay = vi.fn();
  const pb = new SetPlayback({ engine: make, plan: plans, beforePlay });
  pb.setLayout(layout);
  return { engine, make, pb, beforePlay };
}

describe("SetPlayback", () => {
  it("doesn't make an engine for a seek: that waits for Play", async () => {
    const { pb, make } = setup();
    pb.seek(100);
    expect(pb.time()).toBe(100);
    expect(make).not.toHaveBeenCalled();
    await pb.play();
    expect(make).toHaveBeenCalledOnce();
  });

  it("plays from anywhere", async () => {
    const { pb, engine, beforePlay } = setup();
    pb.seek(400); // in track b, which transition 1 plays
    await pb.play();
    expect(pb.status).toBe("playing");
    expect(beforePlay).toHaveBeenCalled();
    expect(engine.load).toHaveBeenCalledWith("a", { hash: "b", trimDb: 0 });
    expect(engine.load).toHaveBeenCalledWith("b", { hash: "c", trimDb: 0 });
    expect(pb.time()).toBeCloseTo(400);
    await pb.close();
  });

  it("hands over between transitions: B goes on as A, on its sample", async () => {
    const { pb, engine } = setup();
    await pb.play(290);
    pb.tick(0);
    await flush();
    expect(engine.scheduled).toHaveLength(1);
    engine.now = 90; // past the handover at 80
    pb.tick(100);
    await flush();
    expect(engine.scheduled).toHaveLength(2);
    expect(engine.scheduled[1].plan.a.track_id).toBe("b");
    expect(engine.scheduled[1].carry).toEqual({ from: "b", to: "a" });
    expect(engine.seeks).toHaveLength(1); // it carries on, it doesn't jump
    await pb.close();
  });

  it("holds when the next plan hasn't come, and picks up where it held when it does", async () => {
    let ready = false;
    const { pb, engine } = setup((a, b) => (a === "b" && !ready ? Promise.reject(new Error("no plan")) : Promise.resolve(plan(a, b))));
    await pb.play(290);
    pb.tick(0);
    await flush();
    const end = (pb as unknown as { current: { plan: MixPlan } }).current.plan.window.end_beat;
    engine.now = end - 0.5;
    pb.tick(100);
    await flush();
    expect(pb.status).toBe("waiting");
    expect(engine.pause).toHaveBeenCalled();
    ready = true;
    pb.tick(5000);
    await flush();
    pb.tick(5100);
    await flush();
    expect(pb.status).toBe("playing");
    expect(engine.scheduled[1].carry).toBeUndefined();
    const heldAt = (end - 0.5) / 2; // 2 beats a second, from B's 0 s
    expect(engine.seeks[1]).toBeCloseTo(beatAtFile(engine.scheduled[1].plan.a, heldAt));
    expect(engine.play).toHaveBeenCalledTimes(2);
    await pb.close();
  });

  it("brings each track to the deck level with its loudness", async () => {
    const loud: SetTimelineData = { ...data, tracks: data.tracks.map((t) => ({ ...t, integrated_lufs: t.hash === "a" ? -8 : null })) };
    const { pb, engine } = setup();
    pb.setLayout(layoutTimeline(order(["a", "b", "c"]), [], loud), true);
    await pb.play(10);
    expect(engine.load).toHaveBeenCalledWith("a", { hash: "a", trimDb: -12 - -8 });
    await pb.close();
  });

  it("reports a plan it can't open", async () => {
    const { pb } = setup(() => Promise.reject(new Error("no saved plan for this pair")));
    await pb.play(0);
    expect(pb.status).toBe("error");
    expect(pb.error).toMatch(/no saved plan/);
  });

  it("stops when the order changes, and says when the set has ended", async () => {
    const { pb, engine } = setup();
    await pb.play(10);
    pb.setLayout(layoutTimeline(order(["b", "a", "c"]), [], data));
    expect(pb.status).toBe("stopped");
    await pb.play(10);
    engine.emit("ended");
    expect(pb.status).toBe("ended");
    expect(pb.time()).toBe(pb["layout"].total);
    await pb.close();
  });

  it("the last play wins when they overlap", async () => {
    const { pb, engine } = setup();
    const first = pb.play(10);
    const second = pb.play(400);
    await Promise.all([first, second]);
    expect(engine.play).toHaveBeenCalledOnce();
    expect(pb.time()).toBeCloseTo(400);
    await pb.close();
  });
});
