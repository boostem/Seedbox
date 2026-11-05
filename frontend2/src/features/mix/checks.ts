/**
 * The transition editor's checks, worked out from the plan and the tracks' waveforms: nothing has been rendered, and an
 * edit has to show in them at once. So these are estimates of what `seedbox2/eval/mixmetrics.py` measures on a render,
 * graded with the same limits (`mixmetrics.checks`).
 *
 * Each `fix*` returns the plan with that problem dealt with, as an ordinary edit.
 */
import type { Deck, Keyframe, MixPlan } from "@/api/mixplan.generated";
import type { MixCheck, MixCheckStatus } from "@/api/types";
import { deckBpm, deckTime } from "@/audio/engine/position";
import type { WaveData } from "@/features/waveform/prepare";
import { laneValue, type StemWave } from "@/features/waveform/stems";
import { parseCamelot } from "@/lib/camelot";
import { type DeckId, withLane } from "./lanes";

export interface CheckInput {
  plan: MixPlan;
  waves: Record<DeckId, WaveData | null>;
  /** A deck's stems, when it plays them. */
  stems: Record<DeckId, StemWave | null>;
  /** Camelot codes, as the library knows them. */
  keys: Record<DeckId, string | null>;
}

const DECKS: DeckId[] = ["a", "b"];
const STEP = 0.25; // beats between samples
const BASS_RANGE_DB = 20; // as mixmetrics: audible within this of the deck's own loudest
const VOCAL_RANGE_DB = 25;
const NO_SOURCE = 0.1; // a stem whose peak is this far under the file's ceiling isn't there: it's what leaked into it
const AUDIBLE_FADER = 0.02;
const KILL_FLOOR = 10 ** (-30 / 20); // a stem lane on a deck without stems plays on its EQ, which can't go lower
const SHORT_TERM_S = 3;
const MIN_OVERLAP_S = 1;
const HAND_OFF_BEATS = 0.5;
const KEY_SHIFT_MAX = 2;
const BEATS_PER_BAR = 4;
const MAX_RIDE_UP_EQ_DB = 6;
const KILLED_DB = -20; // an EQ band down this far is a kill: riding doesn't open it

const grade = (value: number, warn: number, fail: number): MixCheckStatus => (value <= warn ? "pass" : value <= fail ? "warn" : "fail");
const WORST: Record<MixCheckStatus, number> = { pass: 0, na: 0, warn: 1, fail: 2 };
const dbLinear = (db: number) => 10 ** (db / 20);
const linearDb = (x: number) => 20 * Math.log10(Math.max(x, 1e-6));

/** The master beats where both decks' faders are up. Null: they never are, so it's a cut. */
export function overlapOf(plan: MixPlan): [number, number] | null {
  const from = Math.max(plan.a.play_from_beat, plan.b.play_from_beat, plan.window.start_beat);
  const to = Math.min(plan.a.play_to_beat, plan.b.play_to_beat, plan.window.end_beat);
  let first: number | null = null;
  let last = 0;
  for (let beat = from; beat < to; beat += STEP) {
    if (laneValue(plan.a.lanes.volume, beat) > AUDIBLE_FADER && laneValue(plan.b.lanes.volume, beat) > AUDIBLE_FADER) {
      first ??= beat;
      last = beat + STEP;
    }
  }
  return first === null ? null : [first, last];
}

function peak(plane: Float32Array, pps: number, t0: number, t1: number): number {
  const i0 = Math.max(0, Math.floor(t0 * pps));
  const i1 = Math.min(plane.length, Math.max(i0 + 1, Math.ceil(t1 * pps)));
  let top = 0;
  for (let i = i0; i < i1; i++) if (plane[i] > top) top = plane[i];
  return top;
}

const beatSeconds = (plan: MixPlan, beat: number) => 60 / laneValue(plan.tempo, beat);

/** How much of a high-pass's cut reaches the bass, and a low-pass's the rest: a rough law, for grading only. */
const highPassLows = (filter: number) => (filter > 0.1 ? Math.max(0, 1 - (filter - 0.1) / 0.4) : 1);
const lowPassHighs = (filter: number) => (filter < -0.1 ? Math.max(0, 1 + (filter + 0.1) / 0.6) : 1);

function stemGain(deck: Deck, name: "gain_bass" | "gain_vocals" | "gain_drums" | "gain_other", beat: number, hasStems: boolean): number {
  if (!deck.stem_lanes) return 1;
  const gain = laneValue(deck.stem_lanes[name], beat);
  return hasStems ? gain : Math.max(gain, KILL_FLOOR);
}

type Source = { plane: Float32Array; pps: number; gain: (deck: Deck, beat: number) => number };

function bassSource(input: CheckInput, id: DeckId): Source | null {
  const stems = input.stems[id];
  const wave = input.waves[id];
  const gain = (d: Deck, beat: number) =>
    laneValue(d.lanes.volume, beat) * dbLinear(laneValue(d.lanes.eq_low, beat)) * highPassLows(laneValue(d.lanes.filter, beat)) * stemGain(d, "gain_bass", beat, !!stems);
  if (stems) return { plane: stems.bass, pps: stems.pps, gain };
  return wave ? { plane: wave.low, pps: wave.pps, gain } : null;
}

function vocalSource(input: CheckInput, id: DeckId): Source | null {
  const stems = input.stems[id];
  if (!stems) return null;
  const gain = (d: Deck, beat: number) => {
    const filter = Math.abs(laneValue(d.lanes.filter, beat));
    return laneValue(d.lanes.volume, beat) * dbLinear(laneValue(d.lanes.eq_mid, beat)) * (filter > 0.7 ? 0.25 : 1) * stemGain(d, "gain_vocals", beat, true);
  };
  return { plane: stems.vocals, pps: stems.pps, gain };
}

/** The beats, in a list, where both decks' source is audible. */
function bothAudible(plan: MixPlan, overlap: [number, number], a: Source, b: Source, rangeDb: number): number[] {
  const range = dbLinear(-rangeDb);
  const sample = (id: DeckId, s: Source, beat: number) => {
    const deck = plan[id];
    if (beat < deck.play_from_beat || beat >= deck.play_to_beat) return 0;
    return peak(s.plane, s.pps, deckTime(deck, beat), deckTime(deck, beat + STEP));
  };
  const reference = (id: DeckId, s: Source) => {
    let top = 0;
    for (let beat = overlap[0]; beat < overlap[1]; beat += STEP) top = Math.max(top, sample(id, s, beat));
    return top;
  };
  const refA = reference("a", a);
  const refB = reference("b", b);
  if (refA < NO_SOURCE || refB < NO_SOURCE) return [];
  const beats: number[] = [];
  for (let beat = overlap[0]; beat < overlap[1]; beat += STEP) {
    const levelA = sample("a", a, beat) * a.gain(plan.a, beat);
    const levelB = sample("b", b, beat) * b.gain(plan.b, beat);
    if (levelA > refA * range && levelB > refB * range) beats.push(beat);
  }
  return beats;
}

const secondsOf = (plan: MixPlan, beats: number[]) => beats.reduce((sum, beat) => sum + beatSeconds(plan, beat) * STEP, 0);

/** The camelot wheel's distance between two codes; a relative major or minor counts as none. Null: a key is unknown. */
export function keySteps(a: string | null, b: string | null, semitones = 0): number | null {
  const x = parseCamelot(a);
  const y = parseCamelot(b);
  if (!x || !y) return null;
  const shifted = (((y.n + 7 * semitones - 1) % 12) + 12) % 12 + 1;
  const around = Math.min(Math.abs(x.n - shifted), 12 - Math.abs(x.n - shifted));
  return around + (x.letter === y.letter || around === 0 ? 0 : 1);
}

function maxStretch(plan: MixPlan, overlap: [number, number] | null): { pct: number; a: number; b: number } {
  const from = overlap ? overlap[0] : plan.window.start_beat;
  const to = overlap ? overlap[1] : plan.window.end_beat;
  const out = { pct: 0, a: 1, b: 1 };
  const look = (id: DeckId, beat: number, tempo: number) => {
    const deck = plan[id];
    if (beat < deck.play_from_beat || beat >= deck.play_to_beat) return;
    const rate = tempo / deckBpm(deck, beat);
    const pct = Math.abs(rate - 1) * 100;
    if (pct > Math.abs(out[id] - 1) * 100) out[id] = rate;
    if (pct > out.pct) out.pct = pct;
  };
  for (let beat = from; beat < to; beat += BEATS_PER_BAR) {
    const tempo = laneValue(plan.tempo, beat);
    look("a", beat, tempo);
    look("b", beat, tempo);
  }
  return out;
}

// ------------------------------------------------------------------------------------------------ loudness

function powerOf(input: CheckInput, id: DeckId, beat: number, unity: boolean): number {
  const deck = input.plan[id];
  if (beat < deck.play_from_beat || beat >= deck.play_to_beat) return 0;
  const t0 = deckTime(deck, beat);
  const t1 = deckTime(deck, beat + STEP);
  const eq = (name: "eq_low" | "eq_mid" | "eq_high") => (unity ? 1 : dbLinear(laneValue(deck.lanes[name], beat)));
  const filter = unity ? 0 : laneValue(deck.lanes.filter, beat);
  const fader = unity ? 1 : laneValue(deck.lanes.volume, beat);
  const stems = input.stems[id];
  if (stems) {
    const g = (name: "gain_bass" | "gain_vocals" | "gain_drums" | "gain_other") => (unity ? 1 : stemGain(deck, name, beat, true));
    const low = highPassLows(filter);
    const high = lowPassHighs(filter);
    return (
      fader ** 2 *
      ((peak(stems.bass, stems.pps, t0, t1) * g("gain_bass") * eq("eq_low") * low) ** 2 +
        (peak(stems.vocals, stems.pps, t0, t1) * g("gain_vocals") * eq("eq_mid")) ** 2 +
        (peak(stems.drums, stems.pps, t0, t1) * g("gain_drums") * eq("eq_high") * high) ** 2 +
        (peak(stems.other, stems.pps, t0, t1) * g("gain_other") * eq("eq_mid")) ** 2)
    );
  }
  const wave = input.waves[id];
  if (!wave) return 0;
  return (
    fader ** 2 *
    ((peak(wave.low, wave.pps, t0, t1) * eq("eq_low") * highPassLows(filter)) ** 2 +
      (peak(wave.mid, wave.pps, t0, t1) * eq("eq_mid")) ** 2 +
      (peak(wave.high, wave.pps, t0, t1) * eq("eq_high") * lowPassHighs(filter)) ** 2)
  );
}

/** Per bar of the overlap: the estimated short-term level of the sum against the line from A's level to B's, in LU. */
function loudnessCurve(input: CheckInput, overlap: [number, number]): { beat: number; lu: number }[] {
  const { plan } = input;
  const mean = (id: DeckId, from: number, to: number, unity: boolean) => {
    let sum = 0;
    let n = 0;
    for (let beat = from; beat < to; beat += STEP) {
      sum += powerOf(input, id, beat, unity);
      n++;
    }
    return n ? sum / n : 0;
  };
  const span = SHORT_TERM_S / beatSeconds(plan, overlap[0]); // beats in a short-term window
  const levelA = 10 * Math.log10(Math.max(mean("a", overlap[0] - span, overlap[0], true), 1e-9));
  const levelB = 10 * Math.log10(Math.max(mean("b", overlap[1], overlap[1] + span, true), 1e-9));
  const out: { beat: number; lu: number }[] = [];
  const hop = Math.max(BEATS_PER_BAR / 2, 1);
  for (let beat = overlap[0]; beat <= overlap[1]; beat += hop) {
    const from = beat - span / 2;
    const to = beat + span / 2;
    const sum = mean("a", from, to, false) + mean("b", from, to, false);
    const t = (beat - overlap[0]) / (overlap[1] - overlap[0] || 1);
    out.push({ beat, lu: 10 * Math.log10(Math.max(sum, 1e-9)) - (levelA + (levelB - levelA) * t) });
  }
  return out;
}

// ------------------------------------------------------------------------------------------------ the checks

/** Bass overlap, vocal overlap, key and tempo, and loudness: `MixChecks` lists them. */
export function planChecks(input: CheckInput): MixCheck[] {
  const { plan, keys } = input;
  const overlap = overlapOf(plan);
  const item = (id: MixCheck["id"], label: string, status: MixCheckStatus, value: string, detail: string): MixCheck => ({ id, label, status, value, detail });
  const cut = overlap === null;
  const out: MixCheck[] = [];

  const bassA = bassSource(input, "a");
  const bassB = bassSource(input, "b");
  if (cut) {
    out.push(item("bass", "One bassline at a time", "pass", "0.0 s", "A cut: the decks never play together"));
  } else if (!bassA || !bassB) {
    out.push(item("bass", "One bassline at a time", "na", "–", "The waveforms aren't there yet"));
  } else {
    const s = secondsOf(plan, bothAudible(plan, overlap, bassA, bassB, BASS_RANGE_DB));
    out.push(item("bass", "One bassline at a time", grade(s, 0.5, 2), `${s.toFixed(1)} s`, `Estimated from the plan: both decks' bass within ${BASS_RANGE_DB} dB of their peak at once`));
  }

  const vocalA = vocalSource(input, "a");
  const vocalB = vocalSource(input, "b");
  if (cut) {
    out.push(item("vocals", "One vocal at a time", "pass", "0.0 s", "A cut: the decks never play together"));
  } else if (!vocalA || !vocalB) {
    out.push(item("vocals", "One vocal at a time", "na", "–", "Needs the stems of both tracks"));
  } else {
    const s = secondsOf(plan, bothAudible(plan, overlap, vocalA, vocalB, VOCAL_RANGE_DB));
    out.push(item("vocals", "One vocal at a time", grade(s, 0.5, 2), `${s.toFixed(1)} s`, "Estimated from the plan and the vocal stems: both decks' vocals at once"));
  }

  const stretch = maxStretch(plan, overlap);
  const semitones = plan.b.key_shift - plan.a.key_shift;
  const steps = keySteps(keys.a, keys.b, semitones);
  const stretchStatus = grade(stretch.pct, 4, 8);
  const keyStatus: MixCheckStatus = steps === null ? "na" : cut ? "pass" : grade(steps, 1, 2);
  const shown = semitones ? ` (${semitones > 0 ? "+" : ""}${semitones} st)` : "";
  const keysText = steps === null ? "" : `${keys.a} → ${keys.b}${shown}, `;
  out.push(
    item(
      "key",
      "Key and tempo",
      WORST[keyStatus] > WORST[stretchStatus] ? keyStatus : stretchStatus,
      `${keysText}stretch ${stretch.pct.toFixed(1)}%`,
      `${steps === null ? "Keys unknown" : `${steps} ${steps === 1 ? "step" : "steps"} apart on the wheel`}; A x${stretch.a.toFixed(3)}, B x${stretch.b.toFixed(3)}`,
    ),
  );

  if (cut) {
    out.push(item("loudness", "Even loudness", "pass", "–", "A cut: nothing to even out"));
  } else if (secondsOf(plan, [overlap[0]]) * ((overlap[1] - overlap[0]) / STEP) < MIN_OVERLAP_S) {
    out.push(item("loudness", "Even loudness", "pass", "–", "The overlap is too short to measure"));
  } else if (!input.waves.a || !input.waves.b) {
    out.push(item("loudness", "Even loudness", "na", "–", "The waveforms aren't there yet"));
  } else {
    const curve = loudnessCurve(input, overlap);
    const dip = Math.min(0, ...curve.map((c) => c.lu));
    const bump = Math.max(0, ...curve.map((c) => c.lu));
    const sign = (x: number) => `${x >= 0 ? "+" : ""}${x.toFixed(1)}`;
    out.push(item("loudness", "Even loudness", grade(Math.max(-dip, bump), 1.5, 3), `${sign(dip)} / ${sign(bump)} LU`, "Estimated from the plan and the waveforms: dip / bump against the line from A's level to B's"));
  }
  return out;
}

// ------------------------------------------------------------------------------------------------ the fixes

/** Down to `floor` by `at`, from what the lane was doing up to there. */
function outLane(lane: Keyframe[], at: number, floor: number): Keyframe[] {
  const from = at - HAND_OFF_BEATS;
  return [...lane.filter((k) => k.beat < from), { beat: from, value: laneValue(lane, from), curve: "linear" }, { beat: at, value: floor, curve: "linear" }];
}

/** At `floor` until `at`, then on with what the lane was going to do. */
function inLane(lane: Keyframe[], at: number, floor: number): Keyframe[] {
  const to = at + HAND_OFF_BEATS;
  return [{ beat: at, value: floor, curve: "linear" }, { beat: to, value: laneValue(lane, to), curve: "linear" }, ...lane.filter((k) => k.beat > to)];
}

const onBar = (beat: number) => Math.round(beat / BEATS_PER_BAR) * BEATS_PER_BAR;

/** One deck's lane hands over to the other's on one bar: the middle of where they sound together. */
function handOff(input: CheckInput, beats: number[], stemLane: "gain_bass" | "gain_vocals"): MixPlan {
  const at = onBar((beats[0] + beats[beats.length - 1]) / 2);
  const { plan } = input;
  const lanesOf = (id: DeckId) => {
    const deck = plan[id];
    if (input.stems[id]) return { name: stemLane, lane: deck.stem_lanes?.[stemLane] ?? [{ beat: plan.window.start_beat, value: 1, curve: "linear" as const }], floor: 0 };
    return { name: "eq_low" as const, lane: deck.lanes.eq_low, floor: -30 };
  };
  const a = lanesOf("a");
  const b = lanesOf("b");
  return withLane(withLane(plan, "a", a.name, outLane(a.lane, at, a.floor)), "b", b.name, inLane(b.lane, at, b.floor));
}

function fixKey(input: CheckInput): MixPlan {
  const { keys } = input;
  let plan = input.plan;
  const overlap = overlapOf(plan);
  if (keySteps(keys.a, keys.b, plan.b.key_shift - plan.a.key_shift) !== null) {
    const shifts = [0, 1, -1, 2, -2].filter((s) => Math.abs(s) <= KEY_SHIFT_MAX);
    const best = shifts.reduce((won, s) => {
      const left = keySteps(keys.a, keys.b, s - plan.a.key_shift)!;
      const had = keySteps(keys.a, keys.b, won - plan.a.key_shift)!;
      return left < had ? s : won;
    }, plan.b.key_shift);
    if (best !== plan.b.key_shift) plan = { ...plan, b: { ...plan.b, key_shift: best }, edited: true };
  }
  if (maxStretch(plan, overlap).pct > 4) {
    // the tempo halfway between the decks (geometrically) stretches each as little as a blend can
    const from = overlap ? overlap[0] : plan.window.start_beat;
    const tempo = Math.sqrt(deckBpm(plan.a, from) * deckBpm(plan.b, from));
    plan = { ...plan, tempo: [{ beat: plan.window.start_beat, value: tempo, curve: "linear" }], key_lock: true, edited: true };
  }
  return plan;
}

/** A lane multiplied by a gain curve over a span, as its keyframes and the curve's knots. */
function rideLane(lane: Keyframe[], knots: number[], gain: (beat: number, value: number) => number, overlap: [number, number]): Keyframe[] {
  const inside = (beat: number) => beat >= overlap[0] && beat <= overlap[1];
  const kept = lane.filter((k) => !inside(k.beat));
  const own = lane.filter((k) => inside(k.beat)).map((k) => ({ ...k, value: gain(k.beat, k.value) }));
  const added = knots.filter((b) => !own.some((k) => k.beat === b)).map((beat) => ({ beat, value: gain(beat, laneValue(lane, beat)), curve: "linear" as const }));
  const sorted = [...kept, ...own, ...added].sort((x, y) => x.beat - y.beat);
  return sorted.length > 0 ? sorted : lane;
}

function fixLoudness(input: CheckInput, overlap: [number, number]): MixPlan {
  let plan = input.plan;
  for (let pass = 0; pass < 3; pass++) {
    const curve = loudnessCurve({ ...input, plan }, overlap);
    if (Math.max(...curve.map((c) => Math.abs(c.lu))) <= 1) break;
    const knots = curve.map((c) => c.beat);
    const wanted = (beat: number) => {
      let i = 0;
      while (i + 1 < curve.length && curve[i + 1].beat <= beat) i++;
      const a = curve[i];
      const b = curve[Math.min(i + 1, curve.length - 1)];
      const t = b.beat > a.beat ? (beat - a.beat) / (b.beat - a.beat) : 0;
      return -(a.lu + (b.lu - a.lu) * t);
    };
    for (const id of DECKS) {
      const deck = plan[id];
      const eqRoom = (beat: number) => {
        const up = Math.max(0, wanted(beat));
        const fader = laneValue(deck.lanes.volume, beat);
        return Math.max(0, up - Math.max(0, -linearDb(Math.max(fader, 1e-3))));
      };
      const volume = rideLane(deck.lanes.volume, knots, (beat, v) => Math.min(1, v * dbLinear(wanted(beat))), overlap);
      plan = withLane(plan, id, "volume", volume);
      for (const name of ["eq_mid", "eq_high"] as const) {
        const ridden = rideLane(deck.lanes[name], knots, (beat, v) => (v <= KILLED_DB ? v : Math.min(MAX_RIDE_UP_EQ_DB, v + eqRoom(beat))), overlap);
        plan = withLane(plan, id, name, ridden);
      }
    }
  }
  return plan;
}

/** The plan with this check's problem dealt with, or null when there's nothing a plan can do about it here. */
export function fixCheck(id: MixCheck["id"], input: CheckInput): MixPlan | null {
  const overlap = overlapOf(input.plan);
  if (id === "key") return fixKey(input);
  if (!overlap) return null;
  if (id === "loudness") return input.waves.a && input.waves.b ? fixLoudness(input, overlap) : null;
  const source = id === "bass" ? bassSource : id === "vocals" ? vocalSource : null;
  if (!source) return null;
  const a = source(input, "a");
  const b = source(input, "b");
  if (!a || !b) return null;
  const beats = bothAudible(input.plan, overlap, a, b, id === "bass" ? BASS_RANGE_DB : VOCAL_RANGE_DB);
  if (!beats.length) return null;
  return handOff(input, beats, id === "bass" ? "gain_bass" : "gain_vocals");
}
