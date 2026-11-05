// Generated from seedbox2/mix/plan.py by `python -m seedbox2.mix.typescript`. Don't edit it by hand.

export const PLAN_VERSION = 3 as const;
export const LANES = ["volume", "eq_low", "eq_mid", "eq_high", "filter", "filter_res", "echo_send", "echo_feedback", "reverb_send"] as const;
export const STEM_LANES = ["gain_drums", "gain_bass", "gain_vocals", "gain_other"] as const;
export const LANE_RANGES = {"volume": [0.0, 1.0], "eq_low": [-30.0, 6.0], "eq_mid": [-30.0, 6.0], "eq_high": [-30.0, 6.0], "filter": [-1.0, 1.0], "filter_res": [0.0, 1.0], "echo_send": [0.0, 1.0], "echo_feedback": [0.0, 0.95], "reverb_send": [0.0, 1.0], "gain_drums": [0.0, 1.0], "gain_bass": [0.0, 1.0], "gain_vocals": [0.0, 1.0], "gain_other": [0.0, 1.0]} as const;
export const LANE_DEFAULTS = {"volume": 1.0, "eq_low": 0.0, "eq_mid": 0.0, "eq_high": 0.0, "filter": 0.0, "filter_res": 0.0, "echo_send": 0.0, "echo_feedback": 0.55, "reverb_send": 0.0, "gain_drums": 1.0, "gain_bass": 1.0, "gain_vocals": 1.0, "gain_other": 1.0} as const;
export const TEMPO_RANGE = [40.0, 300.0] as const;
export const KEY_SHIFT_MAX = 2 as const;
export const LOOP_DIVISIONS = [0.5, 0.25, 0.125, 0.0625] as const;
export const STEM_EQ_FALLBACK = {"gain_bass": "eq_low", "gain_vocals": "eq_mid", "gain_drums": "eq_high"} as const;

/** One track in the mix. The master position maps to file time through `grid`, sorted and continuous; its first segment also holds before its start, the last one after it. Times outside the file are silence. */
export interface Deck {
  track_id: string;
  grid: GridSegment[];
  /** Semitones. */
  key_shift: number;
  play_from_beat: number;
  play_to_beat: number;
  lanes: Lanes;
  /** Null: the deck plays the full mix. */
  stem_lanes: StemLanes | null;
  sends: Sends;
  loops: Loop[];
  fx: (StopFx | StutterFx)[];
  /** Set by a person: never regenerated. */
  pinned: string[];
}

/** From `start_beat` (master beats) up to the next segment's: the deck runs at `bpm` from `start_s` in the file. */
export interface GridSegment {
  start_beat: number;
  start_s: number;
  bpm: number;
}

/** A lane's value at a master beat. The shape up to the next keyframe is this one's curve: linear, ease (smoothstep) or hold. Two keyframes on one beat are an instant step. */
export interface Keyframe {
  beat: number;
  value: number;
  curve: "linear" | "hold" | "ease";
}

/** A deck's automation. volume and the sends are 0..1, the EQ bands dB (-30 is a kill), filter -1..1 (low-pass below 0, high-pass above). */
export interface Lanes {
  volume: Keyframe[];
  eq_low: Keyframe[];
  eq_mid: Keyframe[];
  eq_high: Keyframe[];
  filter: Keyframe[];
  filter_res: Keyframe[];
  echo_send: Keyframe[];
  echo_feedback: Keyframe[];
  reverb_send: Keyframe[];
}

/** Slip loop: over [start_beat, end_beat) the deck plays source_beat + ((beat - start_beat) mod length_beats), and on release it is where it would have been. All on whole beats; `length_beats` may also be 1/2, 1/4, 1/8 or 1/16. */
export interface Loop {
  start_beat: number;
  end_beat: number;
  source_beat: number;
  length_beats: number;
}

export interface Marker {
  beat: number;
  label: string;
}

/** The settings of a deck's echo and reverb. A send taps the deck before its EQ, filter and fader, or after them. */
export interface Sends {
  echo_beats: number;
  echo_tap: "pre_eq" | "post_fader";
  reverb_size: number;
  reverb_tap: "pre_eq" | "post_fader";
}

/** Linear gain of each stem, 0..1: 0 is true silence, 1 the stem as separated. */
export interface StemLanes {
  gain_drums: Keyframe[];
  gain_bass: Keyframe[];
  gain_vocals: Keyframe[];
  gain_other: Keyframe[];
}

/** The record stops over the span and the deck is silent after it: pushed back (spinback) or braked. */
export interface StopFx {
  start_beat: number;
  end_beat: number;
  type: "spinback" | "brake";
}

/** A gated retrigger of the slice at start_beat, repeated every `div` beats. */
export interface StutterFx {
  start_beat: number;
  end_beat: number;
  type: "stutter";
  div: number;
}

/** The master beats a render or preview covers. */
export interface Window {
  start_beat: number;
  end_beat: number;
}

/** One A -> B transition. `bars` is the blend length; for a cut it is the length of A's lead-in effect before the cut. `edited`: a person changed it, so nothing may regenerate it. */
export interface MixPlan {
  version: 3;
  technique: string;
  bars: number;
  key_lock: boolean;
  window: Window;
  /** The master tempo map: BPM keyframes in master beats. */
  tempo: Keyframe[];
  a: Deck;
  b: Deck;
  markers: Marker[];
  notes: string[];
  edited: boolean;
}
