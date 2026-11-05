/**
 * What the now-playing view reads off a plan, as pure functions: read them per frame, they allocate little. A set is played
 * as a chain of plans, each stretched over the whole outgoing track (`planChain`); track `i` of the set is on the upper deck
 * when `i` is even, so a track stays where it is when the next plan takes over.
 */
import type { Deck, MixPlan } from "@/api/mixplan.generated";
import type { Section, SectionKind, SessionTrack, SetSession, TrackAnalysis } from "@/api/types";
import { beatAtFile, deckBpm, deckTime } from "@/audio/engine/position";
import { laneValue } from "@/features/waveform/stems";
import { STEMS, type StemWave } from "@/features/waveform/stems";
import type { WaveData } from "@/features/waveform/prepare";
import type { WaveRegion } from "@/features/waveform";

/** The next plan takes over this many beats before the end of a plan's window: time to fetch what it needs. */
export const HANDOFF_BEATS = 8;
/** A plan's window starts this long before where it picks its track up, in seconds, and well past the window's fade-in. */
const ENTRY_MARGIN_S = 10;

export type PlanSide = "a" | "b";

/** A plan stretched over its tracks: its window starts where A comes in (`fromS`) and ends where B ends (`toS`). Before and
 * after their keyframes the lanes and tempo hold, and a grid's outer segments hold too. Null leaves that end as it is. */
export function extendPlan(plan: MixPlan, fromS: number | null, toS: number | null): MixPlan {
  const window = { ...plan.window };
  const a = { ...plan.a };
  const b = { ...plan.b };
  if (fromS !== null) {
    window.start_beat = Math.min(window.start_beat, beatAtFile(plan.a, fromS));
    a.play_from_beat = Math.min(a.play_from_beat, window.start_beat);
  }
  if (toS !== null) {
    window.end_beat = Math.max(window.end_beat, beatAtFile(plan.b, toS));
    b.play_to_beat = Math.max(b.play_to_beat, window.end_beat);
  }
  return { ...plan, window, a, b };
}

/** Where a set can play from: the first pair with a plan, and the run of plans from there until a pair has none. */
export function playableRun(session: Pick<SetSession, "plans">): { first: number; last: number } | null {
  const first = session.plans.findIndex(Boolean);
  if (first < 0) return null;
  let last = first;
  while (session.plans[last + 1]) last++;
  return { first, last };
}

/** The set's plans, stretched: the first over its A from the top and the last over its B to the end, and each one starting
 * early enough to pick up the track the plan before hands it. Null where there's no plan or it can't be reached. */
export function planChain(session: Pick<SetSession, "plans" | "tracks">): (MixPlan | null)[] {
  const chain: (MixPlan | null)[] = session.plans.map(() => null);
  const run = playableRun(session);
  if (!run) return chain;
  for (let i = run.first; i <= run.last; i++) {
    const before = i > run.first ? chain[i - 1]! : null;
    const fromS = before ? Math.max(0, deckTime(before.b, before.window.end_beat - HANDOFF_BEATS) - ENTRY_MARGIN_S) : 0;
    chain[i] = extendPlan(session.plans[i]!, fromS, i === run.last ? session.tracks[i + 1].duration : null);
  }
  return chain;
}

/** The part of the master timeline where both decks are heard. For a cut, the lead-in of `bars` before it. */
export function blendSpan(plan: MixPlan): { start: number; end: number } {
  const start = plan.b.play_from_beat;
  const end = plan.a.play_to_beat;
  if (end > start) return { start, end };
  return { start: end - plan.bars * 4, end };
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** A technique as words: "bass swap" -> "Bass swap". */
export const techniqueLabel = (plan: Pick<MixPlan, "technique">): string => cap(plan.technique.replaceAll("_", " "));

/** Where the plan's transition falls on a deck's track, for its waveform. */
export function deckRegions(plan: MixPlan, side: PlanSide): WaveRegion[] {
  const { start, end } = blendSpan(plan);
  const deck = plan[side];
  return [{ start_s: deckTime(deck, start), end_s: deckTime(deck, end), label: techniqueLabel(plan) }];
}

/** A deck's file time at a master beat: it waits at its cue before it comes in, and at its last sample once it has gone. */
export function heldTime(deck: Deck, beat: number): number {
  return deckTime(deck, Math.min(deck.play_to_beat, Math.max(deck.play_from_beat, beat)));
}

/** The master tempo at a beat. */
export const masterBpm = (plan: Pick<MixPlan, "tempo">, beat: number): number => laneValue(plan.tempo, beat);

/** Seconds of track per second of master at a beat; 0 while the deck waits. */
export function deckRate(plan: MixPlan, deck: Deck, beat: number): number {
  if (beat < deck.play_from_beat || beat >= deck.play_to_beat) return 0;
  return masterBpm(plan, beat) / deckBpm(deck, beat);
}

export type DeckRole = "cued" | "playing" | "gone";

export function deckRole(deck: Deck, beat: number): DeckRole {
  return beat < deck.play_from_beat ? "cued" : beat >= deck.play_to_beat ? "gone" : "playing";
}

/** Seconds of the track left, at the tempo it's playing at now. */
export function timeLeft(plan: MixPlan, deck: Deck, duration: number, beat: number): number {
  const rate = masterBpm(plan, Math.max(beat, deck.play_from_beat)) / deckBpm(deck, beat);
  return Math.max(0, (duration - heldTime(deck, beat)) / (rate || 1));
}

/** The deck's channel strip at a beat, written to `out` so a frame allocates nothing. */
export interface Strip {
  volume: number;
  eqLow: number;
  eqMid: number;
  eqHigh: number;
  filter: number;
}

export const newStrip = (): Strip => ({ volume: 1, eqLow: 0, eqMid: 0, eqHigh: 0, filter: 0 });

export function readStrip(deck: Deck, beat: number, out: Strip = newStrip()): Strip {
  const on = beat >= deck.play_from_beat && beat < deck.play_to_beat;
  out.volume = on ? laneValue(deck.lanes.volume, beat) : 0;
  out.eqLow = laneValue(deck.lanes.eq_low, beat);
  out.eqMid = laneValue(deck.lanes.eq_mid, beat);
  out.eqHigh = laneValue(deck.lanes.eq_high, beat);
  out.filter = laneValue(deck.lanes.filter, beat);
  return out;
}

const dbToGain = (db: number) => 10 ** (db / 20);

/** A row's peak around file time `t`: a stem waveform's or a band's, 0-1. */
function peakAt(v: ArrayLike<number>, pps: number, t: number): number {
  const i = Math.round(t * pps);
  let top = 0;
  for (let k = Math.max(0, i - 2); k <= Math.min(v.length - 1, i + 2); k++) if (v[k] > top) top = v[k];
  return top;
}

const STEM_LANE = { drums: "gain_drums", bass: "gain_bass", vocals: "gain_vocals", other: "gain_other" } as const;

/** What each stem contributes at a beat: its level at this point of the track, by the plan's gain and the fader. These come
 * from the stem waveforms and the plan, not the audio output: the worklet taps no stems. Order of `STEMS`. */
export function stemLevels(stems: StemWave, deck: Deck, t: number, beat: number, strip: Strip, out: Float32Array): Float32Array {
  STEMS.forEach((name, i) => {
    const gain = deck.stem_lanes ? laneValue(deck.stem_lanes[STEM_LANE[name]], beat) : 1;
    out[i] = peakAt(stems[name], stems.pps, t) * gain * strip.volume;
  });
  return out;
}

/** The same for a deck playing the full track: the three bands of its waveform, through the plan's EQ and the fader. */
export function bandLevels(wave: WaveData, t: number, strip: Strip, out: Float32Array): Float32Array {
  out[0] = Math.min(1, peakAt(wave.low, wave.pps, t) * dbToGain(strip.eqLow)) * strip.volume;
  out[1] = Math.min(1, peakAt(wave.mid, wave.pps, t) * dbToGain(strip.eqMid)) * strip.volume;
  out[2] = Math.min(1, peakAt(wave.high, wave.pps, t) * dbToGain(strip.eqHigh)) * strip.volume;
  return out;
}

export type UpNext =
  | { kind: "last" }
  /** The next pair has no plan: the set stops after this track. */
  | { kind: "stops"; track: SessionTrack }
  /** Next: this track, by this plan, `beats` from now. */
  | { kind: "coming"; track: SessionTrack; plan: MixPlan; beats: number }
  /** It's happening: `beats` to go. */
  | { kind: "blending"; track: SessionTrack; plan: MixPlan; beats: number };

/** The up-next strip at master beat `beat` of chain[index]. */
export function upNext(tracks: SessionTrack[], chain: (MixPlan | null)[], index: number, beat: number): UpNext {
  const plan = chain[index];
  if (!plan) return { kind: "last" };
  const { start, end } = blendSpan(plan);
  if (beat < end) {
    const track = tracks[index + 1];
    return beat < start ? { kind: "coming", track, plan, beats: start - beat } : { kind: "blending", track, plan, beats: end - beat };
  }
  const following = tracks[index + 2];
  if (!following) return { kind: "last" };
  const next = chain[index + 1];
  if (!next) return { kind: "stops", track: following };
  // the next plan counts on its own timeline: where this plan's B is now, on it
  const there = beatAtFile(next.a, deckTime(plan.b, beat));
  return { kind: "coming", track: following, plan: next, beats: Math.max(0, blendSpan(next).start - there) };
}

/** Beats as the deck's counters write them: "12.2 bars" is 12 bars and 2 beats, rounded up to a whole beat. */
export function fmtCountdown(beats: number): string {
  const whole = Math.max(0, Math.ceil(beats - 1e-6));
  return `${Math.floor(whole / 4)}.${whole % 4}`;
}

const SECTION_KINDS = new Set<string>(["intro", "build", "drop", "breakdown", "body", "outro"]);

/** The analysis `TrackWaveform` draws: the track's own grid, or the plan's until the track has been analysed. */
export function deckAnalysis(track: SessionTrack, deck: Deck): TrackAnalysis {
  const seg = deck.grid[0];
  const bpm = track.grid?.bpm ?? seg.bpm;
  const beatsPerBar = track.grid?.beats_per_bar ?? 4;
  const barS = (60 / bpm) * beatsPerBar;
  // on the plan's grid, downbeats are the multiples of a bar
  const planFirst = seg.start_s - (((seg.start_beat % beatsPerBar) + beatsPerBar) % beatsPerBar) * (60 / seg.bpm);
  const firstDownbeat = track.grid?.first_downbeat_s ?? ((planFirst % barS) + barS) % barS;
  const barOf = (s: number) => Math.round((s - firstDownbeat) / barS);
  const sections: Section[] = track.sections.map((s) => ({
    kind: (SECTION_KINDS.has(s.kind) ? s.kind : "body") as SectionKind,
    start_s: s.start_s,
    end_s: s.end_s,
    start_bar: barOf(s.start_s),
    end_bar: barOf(s.end_s),
  }));
  const edge = { bars: 0, start_s: 0, end_s: 0, drums_only: false, has_vocals: false };
  return {
    version: 0,
    track_id: track.hash,
    duration_s: track.duration,
    bpm,
    bpm_source: "detected",
    first_downbeat_s: firstDownbeat,
    bar_s: barS,
    bar_count: Math.max(0, Math.floor((track.duration - firstDownbeat) / barS)),
    phrase_offset: 0,
    energy_per_bar: [],
    low_per_bar: [],
    vocal_per_bar: [],
    sections,
    intro: edge,
    outro: edge,
    cues: [],
    waveform: undefined as unknown as TrackAnalysis["waveform"], // it comes separately: `useTrackWave`
  };
}
