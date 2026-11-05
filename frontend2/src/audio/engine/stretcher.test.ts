/** The stretcher in the engine (#66): on the master grid, at the right pitch, whatever the blocks, jumps and effects. */
import { describe, expect, it } from "vitest";
import { wasmOfPackage } from "../../../scripts/stretch-package";
import { LANE_DEFAULTS, LANES, PLAN_VERSION } from "../../api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan } from "../../api/mixplan.generated";
import { Clock } from "./clock";
import { SR } from "./constants";
import { EngineCore } from "./core";
import type { DeckAudio } from "./core";
import { LATENCY } from "./master";
import packaged from "../../../node_modules/signalsmith-stretch/SignalsmithStretch.mjs?raw";
import { STRETCH_WASM_BASE64 } from "./stretch-wasm.generated";

const FILE_BPM = 120;
const CHUNK = 882000;
const kf = (beat: number, value: number, curve: Keyframe["curve"] = "linear"): Keyframe => ({ beat, value, curve });

function deck(over: Partial<Deck> = {}): Deck {
  const lanes = Object.fromEntries(LANES.map((n) => [n, [kf(0, n === "volume" ? 0.25 : LANE_DEFAULTS[n], "hold")]])) as unknown as Lanes;
  return {
    track_id: "t",
    grid: [{ start_beat: 0, start_s: 1, bpm: FILE_BPM }],
    pinned: [],
    key_shift: 0,
    play_from_beat: 0,
    play_to_beat: 64,
    lanes,
    stem_lanes: null,
    sends: { echo_beats: 0.75, echo_tap: "post_fader", reverb_size: 0.5, reverb_tap: "post_fader" },
    loops: [],
    fx: [],
    ...over,
  };
}

function planOf(b: Deck, tempo: Keyframe[], end: number, keyLock = true): MixPlan {
  return {
    version: PLAN_VERSION,
    technique: "t",
    bars: 4,
    key_lock: keyLock,
    window: { start_beat: 0, end_beat: end },
    tempo,
    a: deck({ play_to_beat: 0, play_from_beat: 0 }),
    b,
    markers: [],
    notes: [],
    edited: false,
  };
}

const AUDIO: Record<"a" | "b", DeckAudio> = {
  a: { sources: [{ key: "b", stem: null }], trimDb: 0 },
  b: { sources: [{ key: "b", stem: null }], trimDb: 0 },
};

function coreOn(source: Float32Array): EngineCore {
  const core = new EngineCore();
  core.addStream("b", source.length, CHUNK);
  for (let i = 0; i * CHUNK < source.length; i++) {
    const part = source.subarray(i * CHUNK, Math.min(source.length, (i + 1) * CHUNK));
    core.setChunk("b", i, part, part);
  }
  return core;
}

/** The core's output, with the limiter's latency taken off: sample 0 is the window's first. */
function run(core: EngineCore, frames: number, block = 128): Float32Array {
  const left = new Float32Array(frames + LATENCY);
  const right = new Float32Array(frames + LATENCY);
  for (let at = 0; at < left.length; at += block) {
    const n = Math.min(block, left.length - at);
    core.process(left.subarray(at, at + n), right.subarray(at, at + n), n, at);
  }
  return left.subarray(LATENCY);
}

/** A click on each of the file's beats. */
function clicks(seconds: number): Float32Array {
  const out = new Float32Array(Math.round(seconds * SR));
  const spb = (60 / FILE_BPM) * SR;
  for (let b = 0; 1 * SR + b * spb < out.length; b++) {
    const at = Math.round(1 * SR + b * spb);
    for (let i = 0; i < 64 && at + i < out.length; i++) out[at + i] = (i % 2 ? -1 : 1) * Math.exp(-i / 12);
  }
  return out;
}

/** Where each beat is, by the centre of its energy (a phase vocoder spreads a click out, but evenly). */
function beatErrors(out: Float32Array, clock: Clock, beats: number[]): number[] {
  const reach = Math.round(0.05 * SR);
  return beats.map((beat) => {
    const expected = clock.time(beat) * SR;
    const from = Math.round(expected) - reach;
    let total = 0;
    let moment = 0;
    for (let i = from; i <= from + 2 * reach; i++) {
      const e = out[i] * out[i];
      total += e;
      moment += e * i;
    }
    return (moment / total - expected) / SR;
  });
}

/** The frequency of a steady tone, by counting rising zero crossings. */
function frequency(out: Float32Array, from: number, to: number): number {
  let first = -1;
  let last = -1;
  let count = 0;
  for (let i = from; i < to; i++) {
    if (out[i - 1] < 0 && out[i] >= 0) {
      const at = i - out[i] / (out[i] - out[i - 1]);
      if (first < 0) first = at;
      else count++;
      last = at;
    }
  }
  return (count * SR) / (last - first);
}

function sine(hz: number, seconds: number): Float32Array {
  const out = new Float32Array(Math.round(seconds * SR));
  for (let i = 0; i < out.length; i++) out[i] = 0.3 * Math.sin((2 * Math.PI * hz * i) / SR);
  return out;
}

const cents = (ratio: number): number => 1200 * Math.log2(ratio);

describe("the stretcher", () => {
  it("is the npm package's", () => {
    expect(wasmOfPackage(packaged) === STRETCH_WASM_BASE64).toBe(true);
  });

  it("stays on the master grid over 64 bars of a tempo ramp", () => {
    const beats = 256;
    const tempo = [kf(0, 124), kf(beats, 128)];
    const plan = planOf(deck({ play_to_beat: beats }), tempo, beats);
    const core = coreOn(clicks(140));
    core.setPlan(plan, AUDIO);
    core.play();
    const clock = new Clock(tempo, 0, beats + 2, 0);
    const out = run(core, core.windowFrames);
    const wanted = Array.from({ length: beats - 2 }, (_, i) => i + 1);
    const errors = beatErrors(out, clock, wanted);
    const worst = Math.max(...errors.map(Math.abs));
    const head = errors.slice(0, 16).reduce((a, b) => a + b) / 16;
    const tail = errors.slice(-16).reduce((a, b) => a + b) / 16;
    expect(worst).toBeLessThan(0.001);
    expect(Math.abs(tail - head)).toBeLessThan(0.001);
  }, 120000);

  it("keeps the pitch under key lock, and shifts it by key_shift", () => {
    for (const shift of [0, 1, -1]) {
      const tempo = [kf(0, 126), kf(32, 132)];
      const core = coreOn(sine(440, 30));
      core.setPlan(planOf(deck({ play_to_beat: 32, key_shift: shift }), tempo, 32), AUDIO);
      core.play();
      const out = run(core, core.windowFrames);
      const got = frequency(out, SR, out.length - SR);
      expect(Math.abs(cents(got / 440) - 100 * shift)).toBeLessThan(5);
    }
  }, 60000);

  it("follows the tempo without key lock when it has a key shift to apply", () => {
    const core = coreOn(sine(440, 30));
    core.setPlan(planOf(deck({ play_to_beat: 32, key_shift: 1 }), [kf(0, 132, "hold")], 32, false), AUDIO);
    core.play();
    const out = run(core, core.windowFrames);
    const got = frequency(out, SR, out.length - SR);
    expect(Math.abs(cents(got / 440) - (100 + cents(132 / FILE_BPM)))).toBeLessThan(5);
  }, 30000);

  it("is still on the grid in low-power mode, and when that comes on while playing", () => {
    const tempo = [kf(0, 126), kf(64, 130)];
    const core = coreOn(clicks(40));
    core.setPlan(planOf(deck(), tempo, 64), AUDIO);
    core.play();
    const clock = new Clock(tempo, 0, 66, 0);
    const frames = core.windowFrames;
    const out = new Float32Array(frames + LATENCY);
    const right = new Float32Array(frames + LATENCY);
    const half = Math.floor(frames / 2 / 128) * 128;
    for (let at = 0; at < out.length; at += 128) {
      if (at === half) core.setLowPower(true);
      const n = Math.min(128, out.length - at);
      core.process(out.subarray(at, at + n), right.subarray(at, at + n), n, at);
    }
    const errors = beatErrors(out.subarray(LATENCY), clock, Array.from({ length: 62 }, (_, i) => i + 1));
    const change = clock.beat(half / SR); // the 5 ms fade between the two lands in this beat's reach, and pulls its centre about
    const steady = errors.filter((_, i) => Math.abs(i + 1 - change) > 1);
    expect(Math.max(...steady.map(Math.abs))).toBeLessThan(0.001);
    expect(Math.max(...errors.map(Math.abs))).toBeLessThan(0.002);
  }, 30000);

  it("lands on the grid and at full level straight after a seek", () => {
    const tempo = [kf(0, 128, "hold")];
    const core = coreOn(sine(440, 30));
    core.setPlan(planOf(deck(), tempo, 64), AUDIO);
    core.seek(16);
    core.play();
    const out = run(core, Math.round(SR * 0.5));
    const rms = (a: number, b: number): number => {
      let e = 0;
      for (let i = a; i < b; i++) e += out[i] * out[i];
      return Math.sqrt(e / (b - a));
    };
    expect(rms(1024, 2048) / rms(SR / 4, SR / 4 + 2048)).toBeGreaterThan(0.9);

    const grid = coreOn(clicks(40));
    grid.setPlan(planOf(deck(), tempo, 64), AUDIO);
    grid.seek(16);
    grid.play();
    const clock = new Clock(tempo, 0, 66, 0);
    const heard = run(grid, Math.round(SR * 4));
    const shifted = new Float32Array(Math.round(clock.time(16) * SR) + heard.length);
    shifted.set(heard, Math.round(clock.time(16) * SR));
    const errors = beatErrors(shifted, clock, [17, 18, 19, 20, 21, 22]);
    expect(Math.max(...errors.map(Math.abs))).toBeLessThan(0.001);
  }, 30000);

  it("goes round a deck's loop and a playback loop on the grid", () => {
    const tempo = [kf(0, 126, "hold")];
    const looped = deck({ loops: [{ start_beat: 8, end_beat: 24, source_beat: 4, length_beats: 4 }] });
    const core = coreOn(clicks(40));
    core.setPlan(planOf(looped, tempo, 64), AUDIO);
    core.setLoop(16, 32);
    core.play();
    const clock = new Clock(tempo, 0, 66, 0);
    const span = clock.time(32) - clock.time(16);
    const out = run(core, Math.round((clock.time(32) + span) * SR));
    const later = out.subarray(Math.round(span * SR));
    const errors = beatErrors(later, clock, [18, 19, 20, 21, 22, 23, 26, 28, 29, 30]);
    expect(Math.max(...errors.map(Math.abs))).toBeLessThan(0.001);
  }, 30000);

  it("plays effects from what it has already stretched", () => {
    const tempo = [kf(0, 128, "hold")];
    for (const type of ["spinback", "brake", "stutter"] as const) {
      const ev = { type, start_beat: 16, end_beat: 20, ...(type === "stutter" ? { div: 0.5 } : {}) } as Deck["fx"][number];
      const core = coreOn(sine(440, 30));
      core.setPlan(planOf(deck({ fx: [ev] }), tempo, 64), AUDIO);
      core.play();
      const clock = new Clock(tempo, 0, 66, 0);
      const out = run(core, Math.round(clock.time(24) * SR));
      const at = Math.round(clock.time(16) * SR);
      let peak = 0;
      for (let i = at + 1024; i < at + 4096; i++) peak = Math.max(peak, Math.abs(out[i]));
      expect(peak, type).toBeGreaterThan(0.02);
      expect(out.every(Number.isFinite), type).toBe(true);
    }
  }, 30000);

  it("hears a stem's gain where its sample is heard", () => {
    const tempo = [kf(0, 128, "hold")];
    const drop = 16;
    const stems = {
      gain_drums: [kf(0, 1, "hold")], gain_bass: [kf(0, 1, "hold")],
      gain_vocals: [kf(0, 1, "hold")], gain_other: [kf(0, 1, "hold"), kf(drop, 0, "hold")],
    };
    const source = sine(440, 30);
    const core = new EngineCore();
    core.addStream("o", source.length, CHUNK);
    core.setChunk("o", 0, source, source);
    const audio: Record<"a" | "b", DeckAudio> = {
      a: { sources: [{ key: "o", stem: "gain_other" }], trimDb: 0 },
      b: { sources: [{ key: "o", stem: "gain_other" }], trimDb: 0 },
    };
    core.setPlan(planOf(deck({ stem_lanes: stems }), tempo, 64), audio);
    core.play();
    const clock = new Clock(tempo, 0, 66, 0);
    const out = run(core, Math.round(clock.time(24) * SR));
    const at = Math.round(clock.time(drop) * SR);
    const level = (a: number, b: number): number => {
      let e = 0;
      for (let i = a; i < b; i++) e += out[i] * out[i];
      return Math.sqrt(e / (b - a));
    };
    const before = level(at - 8192, at - 4096);
    // half the level within the stretcher's block either side of the drop, and nothing a block after it
    expect(level(at + 8192, at + 12288)).toBeLessThan(before * 0.01);
    expect(before).toBeGreaterThan(0.02);
  }, 30000);

  it("fades over a change of key while playing, without a click", () => {
    const tempo = [kf(0, 128, "hold")];
    const core = coreOn(sine(440, 30));
    core.setPlan(planOf(deck(), tempo, 64), AUDIO);
    core.play();
    const out = new Float32Array(SR * 2);
    const right = new Float32Array(SR * 2);
    for (let at = 0; at < out.length; at += 128) {
      if (at === SR) core.setPlan(planOf(deck({ key_shift: 1 }), tempo, 64), AUDIO);
      core.process(out.subarray(at, at + 128), right.subarray(at, at + 128), Math.min(128, out.length - at), at);
    }
    let jump = 0;
    for (let i = SR / 2; i < out.length - LATENCY; i++) jump = Math.max(jump, Math.abs(out[i] - out[i - 1]));
    // the steepest a 0.3 x volume sine this high can go is well under this; a cut would be many times it
    expect(jump).toBeLessThan(0.05);
  }, 30000);

  it("scrubs on the varispeed read, so a burst starts at once and costs no seek", () => {
    const core = coreOn(sine(440, 30));
    core.setPlan(planOf(deck(), [kf(0, 128, "hold")], 64), AUDIO);
    core.scrub(16);
    const out = run(core, Math.round(SR * 0.04));
    expect(Math.max(...out.map(Math.abs))).toBeGreaterThan(0.02);
    expect(frequency(out, 512, out.length - 256)).toBeGreaterThan(440 * 1.04);
  });
});
