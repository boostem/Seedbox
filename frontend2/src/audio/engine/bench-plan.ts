/** The benchmark's plan and audio: the most the engine ever does at once, with no browser in it. */
import { LANE_DEFAULTS, LANES, PLAN_VERSION, STEM_LANES } from "../../api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan, StemLanes } from "../../api/mixplan.generated";
import type { DeckAudio } from "./core";

export const CHUNK = 882000;
export const BPM = 124;
const kf = (beat: number, value: number, curve: Keyframe["curve"] = "linear"): Keyframe => ({ beat, value, curve });

function lanes(over: Partial<Lanes>): Lanes {
  const out = Object.fromEntries(LANES.map((n) => [n, [kf(0, LANE_DEFAULTS[n], "hold")]])) as unknown as Lanes;
  return { ...out, ...over };
}

function stems(beats: number): StemLanes {
  const ramp = (a: number, b: number): Keyframe[] => [kf(0, a), kf(beats, b, "ease")];
  return { gain_drums: ramp(1, 0.4), gain_bass: ramp(0.6, 1), gain_vocals: ramp(1, 0.2), gain_other: ramp(0.3, 1) };
}

/** The most a deck does at once: every lane moving, both sends on, a loop, on four stems. */
export function worstPlan(beats: number, offTempo: boolean): MixPlan {
  const deck = (track_id: string, sign: number): Deck => ({
    track_id,
    grid: [{ start_beat: 0, start_s: 0.5, bpm: offTempo ? BPM * 1.02 : BPM }],
    pinned: [],
    key_shift: 0,
    play_from_beat: 0,
    play_to_beat: beats,
    lanes: lanes({
      volume: [kf(0, 0.3), kf(beats, 0.4)],
      eq_low: [kf(0, 0), kf(beats, -20 * sign)].map((k) => ({ ...k, value: Math.max(-30, Math.min(6, k.value)) })),
      eq_mid: [kf(0, -3), kf(beats, -9)],
      eq_high: [kf(0, 2), kf(beats, -6)],
      filter: [kf(0, -0.4 * sign), kf(beats, 0.5 * sign)],
      filter_res: [kf(0, 0.2), kf(beats, 0.5)],
      echo_send: [kf(0, 0.2), kf(beats, 0.3)],
      echo_feedback: [kf(0, 0.4), kf(beats, 0.6)],
      reverb_send: [kf(0, 0.1), kf(beats, 0.2)],
    }),
    stem_lanes: stems(beats),
    sends: { echo_beats: 0.75, echo_tap: "post_fader", reverb_size: 0.7, reverb_tap: "pre_eq" },
    loops: [{ start_beat: 8, end_beat: 16, source_beat: 4, length_beats: 1 }],
    fx: [],
  });
  return {
    version: PLAN_VERSION,
    technique: "bench",
    bars: 8,
    key_lock: !offTempo,
    window: { start_beat: 0, end_beat: beats },
    tempo: [kf(0, BPM, "hold")],
    a: deck("a", 1),
    b: deck("b", -1),
    markers: [],
    notes: [],
    edited: false,
  };
}

function synth(frames: number, seed: number): Float32Array {
  const out = new Float32Array(frames);
  let phase = seed * 0.1;
  let state = seed + 1;
  for (let i = 0; i < frames; i++) {
    state = (state * 1664525 + 1013904223) >>> 0;
    phase += 0.01 + 0.002 * seed;
    out[i] = 0.2 * Math.sin(phase) + 0.05 * (state / 2147483648 - 1);
  }
  return out;
}

export interface Source {
  key: string;
  left: Float32Array;
  right: Float32Array;
}

export function sources(frames: number): Source[] {
  const out: Source[] = [];
  let seed = 0;
  for (const deck of ["a", "b"]) {
    for (const stem of STEM_LANES) {
      out.push({ key: `${deck}/${stem}`, left: synth(frames, seed++), right: synth(frames, seed++) });
    }
  }
  return out;
}

export const audioOf = (deck: string): DeckAudio => ({
  sources: STEM_LANES.map((stem) => ({ key: `${deck}/${stem}`, stem })),
  trimDb: 0,
});

