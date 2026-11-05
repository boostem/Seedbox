import { describe, expect, it } from "vitest";
import { LANE_DEFAULTS, LANES, STEM_LANES } from "../../api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan } from "../../api/mixplan.generated";
import { Clock, Lane } from "./clock";
import { pyRound, SR } from "./constants";
import { EngineCore } from "./core";
import { Isolator } from "./dsp";
import { foldStemLanes } from "./fold";
import { ChunkGone, ChunkLoader, HORIZON_S, MAX_DECODED_BYTES, neededChunks } from "./loader";
import type { PlannedDeck } from "./loader";
import { LATENCY } from "./master";

const BPM = 120;
const CHUNK = 882000;
const kf = (beat: number, value: number, curve: Keyframe["curve"] = "linear"): Keyframe => ({ beat, value, curve });

function deck(track_id: string, over: Partial<Deck> = {}): Deck {
  const lanes = Object.fromEntries(LANES.map((n) => [n, [kf(0, LANE_DEFAULTS[n], "hold")]])) as unknown as Lanes;
  return {
    track_id,
    grid: [{ start_beat: 0, start_s: 0, bpm: BPM }],
    pinned: [],
    key_shift: 0,
    play_from_beat: 0,
    play_to_beat: 64,
    lanes: { ...lanes, volume: [kf(0, 0.5, "hold")] },
    stem_lanes: null,
    sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "pre_eq" },
    loops: [],
    fx: [],
    ...over,
  };
}

function plan(a: Deck, b: Deck, end = 32): MixPlan {
  return {
    version: 3,
    technique: "test",
    bars: 8,
    key_lock: true,
    window: { start_beat: 0, end_beat: end },
    tempo: [kf(0, BPM, "hold")],
    a,
    b,
    markers: [],
    notes: [],
    edited: false,
  };
}

/** A ramp: every sample says where in the file it is, so where a read landed can be read off the output. */
function core(p: MixPlan, frames = SR * 20): EngineCore {
  const c = new EngineCore();
  const left = new Float32Array(frames);
  for (let i = 0; i < frames; i++) left[i] = (i / frames) * 0.5;
  const silent = new Float32Array(frames);
  c.addStream("a", frames, frames);
  c.addStream("b", frames, frames);
  c.setChunk("a", 0, left, left);
  c.setChunk("b", 0, silent, silent);
  const audio = (key: string) => ({ sources: [{ key, stem: null }], trimDb: 0 });
  c.setPlan(p, { a: audio("a"), b: audio("b") });
  return c;
}

function run(c: EngineCore, frames: number, from = 0): Float32Array {
  const left = new Float32Array(frames);
  const right = new Float32Array(frames);
  for (let at = 0; at < frames; at += 128) {
    const n = Math.min(128, frames - at);
    c.process(left.subarray(at, at + n), right.subarray(at, at + n), n, from + at);
  }
  return left;
}

describe("lanes", () => {
  const lane = new Lane([kf(0, 0), kf(4, 1, "ease"), kf(8, 0, "hold"), kf(12, 1), kf(12, 0.25, "hold")]);

  it("follows each keyframe's curve", () => {
    expect(lane.at(-1)).toBe(0);
    expect(lane.at(2)).toBeCloseTo(0.5);
    expect(lane.at(5)).toBeCloseTo(1 - (0.25 * 0.25 * (3 - 0.5)));
    expect(lane.at(9)).toBe(0);
    expect(lane.at(100)).toBe(0.25);
  });

  it("takes the later of two keyframes on a beat, looked up in any order", () => {
    expect(lane.at(12)).toBe(0.25);
    expect(lane.at(3)).toBeCloseTo(0.75);
    expect(lane.at(11.999999)).toBe(0);
  });

  it("rounds as Python does, or a sample is lost", () => {
    expect([pyRound(0.5), pyRound(1.5), pyRound(2.5), pyRound(220.5), pyRound(2.6), pyRound(-0.5)]).toEqual([0, 2, 2, 220, 3, -0]);
  });
});

describe("the clock", () => {
  it("is the inverse of itself across a tempo ramp", () => {
    const clock = new Clock([kf(0, 120), kf(32, 130)], -8, 40, -4);
    expect(clock.time(-4)).toBeCloseTo(0, 9);
    for (const b of [-4, 0, 3.3, 16, 31.9]) expect(clock.beat(clock.time(b))).toBeCloseTo(b, 6);
    expect(clock.bpm(16)).toBeCloseTo(125, 6);
  });
});

describe("the isolator", () => {
  it("sums flat with the bands up, and kills a band", () => {
    const iso = new Isolator();
    const n = 128;
    const unit = new Float64Array(n).fill(1);
    const zero = new Float64Array(n);
    const tone = (f: number, g: [Float64Array, Float64Array, Float64Array]) => {
      iso.reset();
      let peak = 0;
      for (let block = 0; block < 400; block++) {
        const l = new Float64Array(n);
        const r = new Float64Array(n);
        for (let i = 0; i < n; i++) l[i] = r[i] = Math.sin((2 * Math.PI * f * (block * n + i)) / SR);
        iso.run(l, r, n, g[0], g[1], g[2]);
        if (block > 200) for (let i = 0; i < n; i++) peak = Math.max(peak, Math.abs(l[i]));
      }
      return peak;
    };
    for (const f of [60, 200, 900, 2500, 9000]) expect(tone(f, [unit, unit, unit])).toBeCloseTo(1, 2);
    expect(tone(60, [zero, unit, unit])).toBeLessThan(0.02);
    expect(tone(9000, [unit, unit, zero])).toBeLessThan(0.02);
  });
});

describe("transport", () => {
  const ramp = plan(deck("a"), deck("b"));

  it("puts a sample on the frame it's scheduled for, whatever the quantum", () => {
    const at = 1000;
    const c = core(ramp);
    c.play(at);
    const out = run(c, at + LATENCY + 2000);
    for (let i = 0; i < at + LATENCY; i++) expect(out[i]).toBe(0);
    const first = out.findIndex((v) => v !== 0);
    // the first master sample is silent on the window's fade-in (0 / 219) and the ramp's sample 0: it starts just after
    expect(first).toBe(at + LATENCY + 1);
  });

  it("jumps to the loop region on the sample, again and again", () => {
    const c = core(ramp);
    c.seek(4);
    c.setLoop(4, 8);
    c.play();
    const span = 2 * SR; // 4 beats at 120
    const out = run(c, LATENCY + span * 2 + 1000);
    const ratio = (i: number) => out[LATENCY + i];
    // a loop back to where it started: the same audio one region later, away from the region's fades
    for (const i of [400, 5000, 44100, 80000]) expect(ratio(i + span)).toBeCloseTo(ratio(i), 5);
    expect(ratio(5000 + span)).not.toBeCloseTo(ratio(5000 + 44100), 3);
  });

  it("pause fades and resumes where it was heard", () => {
    const c = core(ramp);
    c.play();
    const before = run(c, 20000);
    expect(Math.abs(before[before.length - 1])).toBeGreaterThan(0);
    c.pause();
    const fading = run(c, 220 + LATENCY + 128, 20000);
    expect(Math.abs(fading[fading.length - 1])).toBe(0);
    expect(c.playing).toBe(false);
    const held = c.position;
    run(c, 1000, 40000);
    expect(c.position).toBe(held);
    c.play();
    const again = run(c, LATENCY + 2000, 41000);
    const tail = again[again.length - 1];
    expect(tail).toBeGreaterThan(before[before.length - 1] * 0.9);
  });

  it("stops at the window's end, fading out on the last sample", () => {
    const c = core(plan(deck("a"), deck("b"), 2));
    c.play();
    const out = run(c, SR + LATENCY + 500);
    expect(c.takeEnded()).toBe(true);
    expect(c.playing).toBe(false);
    expect(Math.abs(out[SR + LATENCY - 1])).toBeLessThan(1e-5);
    expect(Math.abs(out[SR + LATENCY + 200])).toBe(0);
  });

  it("is silent past a stop effect, and a loop reads its source", () => {
    const stop = plan(deck("a", { fx: [{ type: "brake", start_beat: 2, end_beat: 4 }] }), deck("b"));
    const out = run(((c) => (c.play(), c))(core(stop)), LATENCY + 3 * SR);
    expect(out[LATENCY + Math.round(0.5 * SR)]).toBeGreaterThan(0);
    expect(out[LATENCY + 2 * SR + 2000]).toBe(0);

    const held = plan(deck("a", { loops: [{ start_beat: 4, end_beat: 12, source_beat: 0, length_beats: 4 }] }), deck("b"));
    const looped = run(((c) => (c.play(), c))(core(held)), LATENCY + 5 * SR);
    expect(looped[LATENCY + 3 * SR]).toBeCloseTo(looped[LATENCY + SR], 5);
  });

  it("counts audio that hadn't arrived, and plays silence for it", () => {
    const c = new EngineCore();
    c.addStream("a", SR * 40, CHUNK);
    c.addStream("b", SR * 40, CHUNK);
    const audio = (key: string) => ({ sources: [{ key, stem: null }], trimDb: 0 });
    c.setPlan(plan(deck("a"), deck("b")), { a: audio("a"), b: audio("b") });
    c.play();
    const out = run(c, 5000);
    expect(out.every((v) => v === 0)).toBe(true);
    expect(c.missing).toBeGreaterThan(0);
  });
});

describe("the memory budget", () => {
  const eight = (frames: number): PlannedDeck[] =>
    ["a", "b"].map((id, n) => ({
      plan: deck(id, { play_to_beat: 4000, grid: [{ start_beat: 0, start_s: n * 7.3, bpm: BPM }] }),
      keys: STEM_LANES.map((s) => `${id}/${s}`),
      frames,
      chunkFrames: CHUNK,
    }));

  /** Plays `seconds` of a plan against a fake server, in steps: what's held, what played missing. */
  async function simulate(decks: PlannedDeck[], seconds: number, loop: { from: number; to: number } | null = null) {
    const clock = new Clock([kf(0, BPM, "hold")], -4, 4000, 0);
    const held = new Set<string>();
    const samples = new Float32Array(CHUNK); // one array: the loader only asks how big it is
    const loader = new ChunkLoader({
      fetchChunk: () => Promise.resolve({ left: samples, right: samples }),
      post: (k, i) => held.add(`${k}#${i}`),
      drop: (k, i) => held.delete(`${k}#${i}`),
      chunkBytes: () => CHUNK * 8,
    });
    let late = 0;
    let most = 0;
    for (let t = 0; t < seconds; t += 0.5) {
      loader.update(neededChunks(decks, clock, t, 4000, loop));
      for (let i = 0; i < 64; i++) await Promise.resolve(); // what's on its way arrives
      if (t > 1) {
        for (const n of neededChunks(decks, clock, t, 4000, loop, 0)) if (!held.has(`${n.key}#${n.index}`)) late++;
      }
      most = Math.max(most, held.size * CHUNK * 8);
    }
    return { peak: loader.peakBytes, most, late, held: held.size };
  }

  it("is never exceeded by 2 decks x 4 stems over ten minutes, and nothing plays late", async () => {
    const r = await simulate(eight(SR * 600), 600);
    expect(r.peak).toBeLessThanOrEqual(MAX_DECODED_BYTES);
    expect(r.most).toBeLessThanOrEqual(MAX_DECODED_BYTES);
    expect(r.late).toBe(0);
    expect(r.peak).toBeGreaterThan(CHUNK * 8 * 8 * 2); // it does read ahead
  });

  it("keeps a loop region's chunks while it loops", async () => {
    const r = await simulate(eight(SR * 600), 120, { from: 15, to: 45 });
    expect(r.peak).toBeLessThanOrEqual(MAX_DECODED_BYTES);
    expect(r.late).toBe(0);
  });

  it("asks for chunks soonest first, and reads a loop's source", () => {
    const clock = new Clock([kf(0, BPM, "hold")], -4, 200, 0);
    const looped = deck("a", { play_to_beat: 200, loops: [{ start_beat: 8, end_beat: 200, source_beat: 80, length_beats: 4 }] });
    const n = neededChunks([{ plan: looped, keys: ["a/mix"], frames: SR * 100, chunkFrames: CHUNK }], clock, 0, 100);
    expect(n.map((c) => c.index)).toEqual([0, 2]); // beat 80 is at 40 s: chunk 2
    expect(n[0].at).toBeLessThan(n[1].at);
    expect(HORIZON_S).toBeGreaterThan(20);
  });

  it("tells the engine when stems have gone, and tries other failures again later", async () => {
    const gone: string[] = [];
    let time = 0;
    let calls = 0;
    const loader = new ChunkLoader(
      {
        fetchChunk: (key) => {
          calls++;
          return Promise.reject(key === "gone" ? new ChunkGone(key) : new Error("boom"));
        },
        post: () => {},
        drop: () => {},
        chunkBytes: () => 8,
        onGone: (k) => gone.push(k),
      },
      100,
      () => time,
    );
    const want = [{ key: "gone", index: 0, at: 0 }, { key: "bad", index: 0, at: 0 }];
    loader.update(want);
    await loader.whenLoaded(want);
    expect(gone).toEqual(["gone"]);
    expect(loader.usedBytes).toBe(0);
    loader.update(want);
    expect(calls).toBe(2);
    time = 10000;
    loader.update(want);
    expect(calls).toBe(4);
  });
});

describe("the fallback without stems", () => {
  const stemmed = deck("a", {
    stem_lanes: {
      gain_drums: [kf(0, 1), kf(8, 0)],
      gain_bass: [kf(0, 1, "hold"), kf(4, 1, "hold"), kf(4, 0, "hold")],
      gain_vocals: [kf(0, 1, "hold")],
      gain_other: [kf(0, 0.5, "hold")],
    },
  });

  it("leaves a deck with stems alone", () => {
    const p = plan(stemmed, deck("b"));
    expect(foldStemLanes(p, () => true)).toBe(p);
  });

  it("puts the lanes on the EQ and says so; other has no band", () => {
    const out = foldStemLanes(plan(stemmed, deck("b")), () => false);
    expect(out.a.stem_lanes).toBeNull();
    const low = new Lane(out.a.lanes.eq_low);
    expect(low.at(2)).toBe(0);
    expect(low.at(5)).toBe(-30);
    expect(new Lane(out.a.lanes.eq_high).at(4)).toBeCloseTo(20 * Math.log10(0.5), 3);
    expect(out.a.lanes.eq_mid).toEqual(deck("a").lanes.eq_mid);
    expect(out.notes).toEqual(["Deck A has no stems: its stem lanes play on its EQ."]);
  });
});

describe("live edits", () => {
  /** A steady tone, so that any jump in the output is the engine's doing. */
  function toneCore(p: MixPlan, tone = 220): EngineCore {
    const frames = SR * 40;
    const c = new EngineCore();
    const left = new Float32Array(frames);
    for (let i = 0; i < frames; i++) left[i] = 0.25 * Math.sin((2 * Math.PI * tone * i) / SR);
    c.addStream("a", frames, frames);
    c.addStream("b", frames, frames);
    c.setChunk("a", 0, left, left);
    c.setChunk("b", 0, new Float32Array(frames), new Float32Array(frames));
    c.setPlan(p, AUDIO);
    return c;
  }
  const AUDIO = {
    a: { sources: [{ key: "a", stem: null }], trimDb: 0 },
    b: { sources: [{ key: "b", stem: null }], trimDb: 0 },
  };
  const volume = (value: number): Deck => {
    const d = deck("a");
    return { ...d, lanes: { ...d.lanes, volume: [kf(0, value, "hold")] } };
  };
  const biggestStep = (x: Float32Array, from = 0, to = x.length): number => {
    let big = 0;
    for (let i = from + 1; i < to; i++) big = Math.max(big, Math.abs(x[i] - x[i - 1]));
    return big;
  };
  const dB = (a: Float32Array, b: Float32Array): number => {
    let err = 0;
    let sig = 0;
    for (let i = 0; i < a.length; i++) {
      err += (a[i] - b[i]) ** 2;
      sig += b[i] ** 2;
    }
    return 10 * Math.log10(err / sig);
  };
  const ONE_S = SR;
  const SETTLED = 2 * SR; // after the leveller's slow gain has settled on both

  it("moves a lane within 5 ms, without a click, and then sounds as if it had always been that plan", () => {
    const c = toneCore(plan(volume(0.8), deck("b", { play_from_beat: 40, play_to_beat: 41 }), 64));
    c.play();
    const PEAK = ONE_S + 50; // the tone's crest, a quarter of its period past a whole second: a jump here is the worst one
    const before = run(c, PEAK);
    c.setPlan(plan(volume(0.2), deck("b", { play_from_beat: 40, play_to_beat: 41 }), 64), AUDIO);
    const after = run(c, LATENCY + ONE_S, PEAK);
    // the steepest a 220 Hz tone at this level goes: more than that is a click
    const tone = biggestStep(before, ONE_S / 2);
    expect(biggestStep(after)).toBeLessThanOrEqual(tone * 1.02);
    // at a quarter of the level from 5 ms after it comes out of the limiter (these lanes have no ramp of their own)
    const peak = (x: Float32Array) => x.reduce((m, v) => Math.max(m, Math.abs(v)), 0);
    expect(peak(after.subarray(LATENCY + 225, LATENCY + 6000))).toBeLessThan(peak(before.subarray(ONE_S / 2)) * 0.27);
  });

  it("nulls against the new plan once an edit has played out", () => {
    const moved = (v: number) => plan(deck("a", { lanes: { ...deck("a").lanes, volume: [kf(0, 0.5), kf(16, v)] } }), deck("b"), 64);
    const edited = toneCore(moved(0.7));
    edited.play();
    run(edited, ONE_S);
    edited.setPlan(moved(0.3), AUDIO);
    const a = run(edited, SETTLED, ONE_S);
    const fresh = toneCore(moved(0.3));
    fresh.play();
    const b = run(fresh, ONE_S + SETTLED).subarray(ONE_S);
    // after the fade and what's in the limiter, away from the different gains the leveller saw before
    expect(dB(a.subarray(LATENCY + 1000), b.subarray(LATENCY + 1000))).toBeLessThan(-40);
  });

  it("fades to a new graph for an edit that changes the structure, keeping the beat", () => {
    const c = toneCore(plan(volume(0.5), deck("b"), 64));
    c.play();
    const before = run(c, ONE_S);
    const beat = c.beat;
    const d = volume(0.5);
    // the isolator switches on, flat: a new graph, sounding the same
    c.setPlan(plan({ ...d, lanes: { ...d.lanes, eq_low: [kf(0, 0.001, "hold")] } }, deck("b"), 64), AUDIO);
    expect(c.beat).toBeCloseTo(beat, 9);
    const after = run(c, 4000, ONE_S);
    expect(biggestStep(after)).toBeLessThanOrEqual(biggestStep(before, ONE_S / 2) * 1.1);
  });

  it("keeps the beat across a tempo edit and fades without a click", () => {
    const c = toneCore(plan(volume(0.5), deck("b"), 64));
    c.play();
    const before = run(c, ONE_S);
    const beat = c.beat;
    c.setPlan({ ...plan(volume(0.5), deck("b"), 64), tempo: [kf(0, 130, "hold")] }, AUDIO);
    expect(c.beat).toBeCloseTo(beat, 1); // the sample's rounding, and the limiter's latency on another tempo
    const after = run(c, 4000, ONE_S);
    expect(biggestStep(after)).toBeLessThanOrEqual(biggestStep(before, ONE_S / 2) * 1.5);
  });

  it("keeps an echo's tail ringing through an edit that makes a new graph", () => {
    const d = volume(0.5);
    const echo: Deck = {
      ...d,
      play_to_beat: 4,
      lanes: { ...d.lanes, echo_send: [kf(0, 1, "hold")], echo_feedback: [kf(0, 0.8, "hold")] },
    };
    const c = toneCore(plan(echo, deck("b"), 64));
    c.play();
    run(c, 3 * SR); // past the deck's end (beat 4 is 2 s), with the repeats going on
    c.setPlan(plan({ ...echo, lanes: { ...echo.lanes, eq_mid: [kf(0, -3, "hold")] } }, deck("b"), 64), AUDIO);
    const after = run(c, SR, 3 * SR);
    const peak = (x: Float32Array) => x.reduce((m, v) => Math.max(m, Math.abs(v)), 0);
    expect(peak(after.subarray(SR / 2))).toBeGreaterThan(0.002);
  });

  it("takes only the newest of the edits that come during a fade", () => {
    const c = toneCore(plan(volume(0.8), deck("b"), 64));
    c.play();
    run(c, ONE_S);
    c.setPlan(plan(volume(0.6), deck("b"), 64), AUDIO);
    c.setPlan(plan(volume(0.4), deck("b"), 64), AUDIO);
    c.setPlan(plan(volume(0.1), deck("b"), 64), AUDIO);
    const a = run(c, SETTLED, ONE_S);
    const peak = (x: Float32Array) => x.reduce((m, v) => Math.max(m, Math.abs(v)), 0);
    expect(peak(a.subarray(SETTLED - 2000))).toBeLessThan(0.1 * 0.25 * 2); // the tone at 0.1, plus the make-up gain
  });

  it("keeps the loop region through an edit", () => {
    const c = toneCore(plan(volume(0.5), deck("b"), 64));
    c.seek(4);
    c.setLoop(4, 6);
    c.play();
    run(c, 20000);
    c.setPlan({ ...plan(volume(0.5), deck("b"), 64), tempo: [kf(0, 130, "hold")] }, AUDIO);
    run(c, 3 * SR, 20000);
    expect(c.beat).toBeGreaterThanOrEqual(3.9);
    expect(c.beat).toBeLessThan(6.1);
  });

  it("seeks without a click: what's in the limiter's lookahead still plays out", () => {
    const c = toneCore(plan(volume(0.5), deck("b"), 64));
    c.play();
    const before = run(c, ONE_S);
    c.seek(20);
    const after = run(c, 8000, ONE_S);
    expect(biggestStep(after)).toBeLessThanOrEqual(biggestStep(before, ONE_S / 2) * 1.1);
  });
});

describe("scrubbing", () => {
  const AUDIO = {
    a: { sources: [{ key: "a", stem: null }], trimDb: 0 },
    b: { sources: [{ key: "b", stem: null }], trimDb: 0 },
  };
  const ramped = () => core(plan(deck("a"), deck("b"), 32));
  const peak = (x: Float32Array) => x.reduce((m, v) => Math.max(m, Math.abs(v)), 0);

  it("is heard from where the pointer is, from a standstill, in short bursts", () => {
    const c = ramped();
    c.scrub(16); // 8 s of the 20 s ramp, whose every sample says where it is: 0.2 x the volume and master gain
    expect(c.position).toBe(pyRound(8 * SR));
    expect(c.playing).toBe(false);
    const out = run(c, 8000);
    expect(peak(out)).toBeGreaterThan(0.05);
    // the ramp at 8 s, not the 0 s where the transport was
    const loudest = peak(out);
    c.scrub(4);
    const earlier = run(c, 8000, 8000);
    expect(peak(earlier.subarray(4000))).toBeLessThan(loudest);
  });

  it("goes quiet when the pointer rests, and stays where it let go", () => {
    const c = ramped();
    c.scrub(8);
    run(c, 4000);
    const resting = run(c, 8000, 4000);
    expect(peak(resting.subarray(4000))).toBe(0);
    c.endScrub();
    run(c, 4000, 12000);
    expect(c.playing).toBe(false);
    expect(c.position).toBe(pyRound(4 * SR));
  });

  it("carries on playing from where it let go, if it was playing", () => {
    const c = ramped();
    c.play();
    run(c, 4000);
    c.scrub(12);
    run(c, 8000, 4000);
    c.scrub(14);
    run(c, 4000, 12000);
    c.endScrub();
    const out = run(c, LATENCY + 8000, 16000);
    expect(c.playing).toBe(true);
    expect(peak(out.subarray(out.length - 1000))).toBeGreaterThan(0.05);
    expect(c.beat).toBeGreaterThan(14);
    expect(c.beat).toBeLessThan(14.5);
  });

  it("doesn't click: every burst fades", () => {
    const frames = SR * 40;
    const c = new EngineCore();
    const tone = new Float32Array(frames);
    for (let i = 0; i < frames; i++) tone[i] = 0.25 * Math.sin((2 * Math.PI * 220 * i) / SR);
    c.addStream("a", frames, frames);
    c.addStream("b", frames, frames);
    c.setChunk("a", 0, tone, tone);
    c.setChunk("b", 0, new Float32Array(frames), new Float32Array(frames));
    c.setPlan(plan(deck("a"), deck("b"), 64), AUDIO);
    c.play();
    const played = run(c, SR);
    let steepest = 0;
    for (let i = SR / 2; i < SR; i++) steepest = Math.max(steepest, Math.abs(played[i] - played[i - 1]));
    for (let k = 0; k < 40; k++) {
      c.scrub(2 + k * 0.37);
      const out = run(c, 735, SR + k * 735); // a pointer event every 1/60 s
      for (let i = 1; i < out.length; i++) expect(Math.abs(out[i] - out[i - 1])).toBeLessThanOrEqual(steepest * 1.2);
    }
  });

  it("takes a live edit between bursts and keeps the pointer's beat", () => {
    const c = ramped();
    c.scrub(16);
    run(c, 4000);
    c.setPlan({ ...plan(deck("a"), deck("b"), 32), tempo: [kf(0, 60, "hold")] }, AUDIO);
    expect(c.position).toBe(pyRound(16 * SR));
    expect(c.beat).toBeCloseTo(16, 6);
  });
});

describe("a plan after another", () => {
  it("carries a deck's track on, from the same file time", () => {
    const core = new EngineCore();
    const stream = (key: string) => {
      core.addStream(key, CHUNK, CHUNK);
      const ramp = new Float32Array(CHUNK).map((_, i) => i / CHUNK);
      core.setChunk(key, 0, ramp, ramp.slice());
    };
    stream("x");
    stream("y");
    stream("z");
    const audio = (a: string, b: string) => ({ a: { sources: [{ key: a, stem: null }], trimDb: 0 }, b: { sources: [{ key: b, stem: null }], trimDb: 0 } });
    const first = plan(deck("x"), deck("y", { grid: [{ start_beat: 0, start_s: 10, bpm: BPM }] }), 64);
    const second = plan(deck("y", { grid: [{ start_beat: 100, start_s: 0, bpm: BPM }], play_from_beat: 100, play_to_beat: 300 }), deck("z", { play_from_beat: 200, play_to_beat: 300 }), 300);
    second.window.start_beat = 100;
    core.setPlan(first, audio("x", "y"));
    core.play(0);
    const l = new Float32Array(128);
    const r = new Float32Array(128);
    for (let i = 0; i < 2000; i++) core.process(l, r, 128);
    const at = core.beat + LATENCY / SR / (60 / BPM); // the deck's next sample, in the old plan's beats
    const fileTime = 10 + (at * 60) / BPM;
    core.setPlan(second, audio("y", "z"), { from: "b", to: "a" });
    // on the new plan, beat 100 is y's file time 0
    expect(core.beat + LATENCY / SR / (60 / BPM)).toBeCloseTo(100 + (fileTime * BPM) / 60, 3);
  });
});
