/**
 * The Simple view's styles: a recipe, a length and two amounts write every lane of a plan, in the browser, so a change is
 * heard at once. Nothing generates plans on the server yet (#53); #57 brings styles as data per genre, and this table
 * is what it replaces. Each recipe follows Seedbox 1's technique of that name (`musicreco/mixplan.py`).
 *
 * Beat 0 is where B comes in, as everywhere in a plan: a blend runs from there, unless A's file would run out before it
 * ends, and a cut's lead-in comes before it.
 */
import { LANE_DEFAULTS, LANES } from "@/api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan, StemLanes } from "@/api/mixplan.generated";
import { laneValue } from "@/features/waveform/stems";
import { type DeckId, flatStemLanes } from "./lanes";

export type StyleId = "long_blend" | "bass_swap" | "filter_sweep" | "echo_out" | "quick_cut";

export interface Style {
  id: StyleId;
  label: string;
  hint: string;
  technique: string;
  bars: number;
  /** Share of the blend B's fader takes to come up, and from where A's starts down. */
  bIn: number;
  aOutFrom: number;
  /** Where in the blend the bass changes hands. */
  swap: number;
  /** At full FX: how far A's high-pass goes (0..1), and the peak of its echo and reverb sends. */
  filter: number;
  echo: number;
  reverb: number;
  /** A plays on to the cut at beat 0, and the blend is its lead-in. */
  cut?: boolean;
}

export const STYLES: Style[] = [
  { id: "long_blend", label: "Long blend", hint: "Both tracks play together for the whole blend; the bass changes hands halfway.", technique: "long blend", bars: 16, bIn: 0.4, aOutFrom: 0.6, swap: 0.5, filter: 0.25, echo: 0, reverb: 0.15 },
  { id: "bass_swap", label: "Bass swap", hint: "B comes in without its lows; the bass trades places, then A fades.", technique: "bass swap", bars: 8, bIn: 0.25, aOutFrom: 0.6, swap: 0.5, filter: 0, echo: 0, reverb: 0.1 },
  { id: "filter_sweep", label: "Filter sweep", hint: "B opens up from a low-pass while A rides a high-pass out.", technique: "filter sweep", bars: 8, bIn: 0.4, aOutFrom: 0.6, swap: 0.6, filter: 0.85, echo: 0, reverb: 0.2 },
  { id: "echo_out", label: "Echo out", hint: "A's last beats are thrown into an echo that rings over B.", technique: "echo out", bars: 4, bIn: 0.5, aOutFrom: 0.75, swap: 0.75, filter: 0.2, echo: 0.8, reverb: 0.2 },
  { id: "quick_cut", label: "Quick cut", hint: "A runs into a cut on the downbeat, with its echo over the top.", technique: "quick cut", bars: 1, bIn: 0, aOutFrom: 1, swap: 1, filter: 0, echo: 0.6, reverb: 0.2, cut: true },
];

const STYLE_OF_TECHNIQUE: Record<string, StyleId> = {
  "long blend": "long_blend",
  "phrase blend": "long_blend",
  "bass swap": "bass_swap",
  "filter sweep": "filter_sweep",
  "echo out": "echo_out",
  "reverb wash": "echo_out",
  "quick cut": "quick_cut",
  "drop swap": "quick_cut",
};

/** The lengths on the Simple view's slider, in bars. */
export const LENGTHS = [1, 2, 4, 8, 16, 32] as const;

export interface SimpleSettings {
  style: StyleId;
  bars: number;
  /** 0: hard swaps and short ramps, 1: long eased ones. */
  smooth: number;
  /** 0: none of the recipe's effects, 1: all of them. */
  fx: number;
}

const WHAT_MADE = new WeakMap<MixPlan, SimpleSettings>();
const DEFAULT_SMOOTH = 0.5;
const DEFAULT_FX = 0.5;

/** The sliders for this plan: what made it, or what it looks like when it came from somewhere else. */
export function settingsOf(plan: MixPlan): SimpleSettings {
  const made = WHAT_MADE.get(plan);
  if (made) return made;
  const style = STYLE_OF_TECHNIQUE[plan.technique] ?? "long_blend";
  const recipe = STYLES.find((s) => s.id === style)!;
  const peak = (id: DeckId, name: "echo_send" | "reverb_send") => Math.max(...plan[id].lanes[name].map((k) => k.value));
  const sent = Math.max(peak("a", "echo_send"), peak("a", "reverb_send"));
  const full = Math.max(recipe.echo, recipe.reverb);
  return { style, bars: plan.bars, smooth: DEFAULT_SMOOTH, fx: sent > 0 && full > 0 ? Math.min(1, sent / full) : DEFAULT_FX };
}

const SNAP = 1 / 16;
const snap = (beat: number) => Math.round(beat / SNAP) * SNAP;
const BEATS_PER_BAR = 4;
const LEAD_BEATS = 8; // of each track alone on the timeline, either side of the blend
const SEND_TAIL = 4; // beats A keeps playing after its fader is down, so its sends ring out
const KILL_DB = -30;
const MID_DUCK_DB = -9; // without stems, the vocal hand-off is as much as this on B's mids
const OUT_RES = 0.35;
const B_LOW_PASS = 0.6; // B's low-pass at the start of a sweep, as a share of A's high-pass

/** Linear: where the deck's file runs out, in master beats. Loops aren't counted: this only moves a blend's start. */
export function fileEndBeat(deck: Deck, duration: number): number {
  let i = deck.grid.length - 1;
  while (i > 0 && deck.grid[i].start_s > duration) i--;
  const g = deck.grid[i];
  return g.start_beat + ((duration - g.start_s) * g.bpm) / 60;
}

export interface StyleContext {
  /** Which decks can play stem lanes. */
  stems: Record<DeckId, boolean>;
  /** The plan as it came: the timeline stays at least its size, so the waveforms don't rescale under a slider. */
  base: MixPlan;
  /** How long A's file is, when known: a blend ends before it does. */
  aDuration?: number | null;
}

/** Keyframes from (beat, value) pairs: a pair on one beat is a step, and the last one's curve doesn't matter. */
function lane(points: [number, number][], curve: Keyframe["curve"]): Keyframe[] {
  const sorted = points.map(([b, v]) => [snap(b), v] as [number, number]);
  return sorted.map(([beat, value], i) => ({ beat, value, curve: i + 1 < sorted.length && sorted[i + 1][1] !== value ? curve : "linear" }));
}

function flatLanes(beat: number): Lanes {
  const flat = Object.fromEntries(LANES.map((n) => [n, [{ beat, value: LANE_DEFAULTS[n], curve: "linear" }]]));
  return flat as unknown as Lanes;
}

/** The whole plan for these settings. It's undoable like any other edit: what the lanes were goes back with undo. */
export function applyStyle(plan: MixPlan, settings: SimpleSettings, context: StyleContext): MixPlan {
  const style = STYLES.find((s) => s.id === settings.style)!;
  const { smooth, fx } = settings;
  const beats = settings.bars * BEATS_PER_BAR;
  let start = style.cut ? -beats : 0;
  const { aDuration } = context;
  if (aDuration) {
    const lastBar = Math.floor(fileEndBeat(plan.a, aDuration) / BEATS_PER_BAR) * BEATS_PER_BAR;
    if (start + beats > lastBar) start = lastBar - beats;
  }
  const end = start + beats;
  const at = (t: number) => start + t * beats;
  const curve: Keyframe["curve"] = smooth >= 0.25 ? "ease" : "linear";
  const ramp = 0.35 + 0.65 * smooth; // how much of the recipe's ramps a low "smooth" keeps
  const window0 = Math.min(context.base.window.start_beat, start - LEAD_BEATS);
  const window1 = Math.max(context.base.window.end_beat, end + LEAD_BEATS);

  const a = flatLanes(window0);
  const b = flatLanes(window0);
  a.volume = style.cut ? lane([[end, 1], [end, 0]], "hold") : lane([[at(1 - (1 - style.aOutFrom) * ramp), 1], [end, 0]], curve);
  b.volume = style.cut ? lane([[end, 0], [end, 1]], "hold") : lane([[start, 0], [at(style.bIn * ramp), 1]], curve);

  // the bass, and without stems the mids that carry the vocal, change hands around here
  const swap = at(style.swap);
  const half = style.cut ? 0 : (beats / 8) * smooth;
  const stemsOf = (id: DeckId, out: boolean): StemLanes | null => {
    if (!context.stems[id]) return null;
    const stems = flatStemLanes(window0);
    if (style.cut) return stems;
    stems.gain_bass = lane(out ? [[swap - half, 1], [swap + half, 0]] : [[swap - half, 0], [swap + half, 1]], curve);
    stems.gain_vocals = lane(out ? [[swap - 2 * half, 1], [swap, 0]] : [[swap, 0], [swap + 2 * half, 1]], curve);
    return stems;
  };
  const stemsA = stemsOf("a", true);
  const stemsB = stemsOf("b", false);
  if (!style.cut) {
    if (!stemsA) a.eq_low = lane([[swap - half, 0], [swap + half, KILL_DB]], curve);
    if (!stemsB) {
      b.eq_low = lane([[swap - half, KILL_DB], [swap + half, 0]], curve);
      b.eq_mid = lane([[start, MID_DUCK_DB], [swap, MID_DUCK_DB], [swap + 2 * half, 0]], curve);
    }
  }

  // the effects, all on the way out of A; B only opens from its low-pass
  const fxFrom = at(Math.min(style.swap, 0.6));
  if (style.filter * fx > 0) {
    a.filter = lane([[fxFrom, 0], [end, style.filter * fx]], curve);
    a.filter_res = lane([[fxFrom, 0], [end, OUT_RES * fx]], curve);
    b.filter = lane([[start, -style.filter * B_LOW_PASS * fx], [swap, 0]], curve);
  }
  if (style.echo * fx > 0) a.echo_send = lane([[fxFrom, 0], [end, style.echo * fx], [end + SEND_TAIL, 0]], curve);
  if (style.reverb * fx > 0) a.reverb_send = lane([[fxFrom, 0], [end, style.reverb * fx], [end + SEND_TAIL, 0]], curve);
  const usesSends = style.echo * fx > 0 || style.reverb * fx > 0;

  const next: MixPlan = {
    ...plan,
    technique: style.technique,
    bars: settings.bars,
    edited: true,
    window: { start_beat: window0, end_beat: window1 },
    a: { ...plan.a, lanes: a, stem_lanes: stemsA, loops: [], fx: [], pinned: [], play_from_beat: Math.min(plan.a.play_from_beat, start - LEAD_BEATS), play_to_beat: end + (usesSends ? SEND_TAIL : 0) },
    b: { ...plan.b, lanes: b, stem_lanes: stemsB, loops: [], fx: [], pinned: [], play_from_beat: style.cut ? end : start, play_to_beat: Math.max(context.base.b.play_to_beat, window1) },
  };
  WHAT_MADE.set(next, settings);
  return next;
}

// ------------------------------------------------------------------------------------------------ the ribbon

export type RibbonKind = "a" | "b" | "both" | "swap" | "fx" | "gap";

export interface RibbonPiece {
  kind: RibbonKind;
  from: number;
  to: number;
}

export const RIBBON_LABEL: Record<RibbonKind, string> = { a: "A", b: "B", both: "Both", swap: "Bass swap", fx: "FX", gap: "Silence" };

const dbLinear = (db: number) => 10 ** (db / 20);
const AUDIBLE = 0.02;

/** The plan as the Simple view's ribbon: what's going on at each beat of the timeline, in pieces. */
export function planRibbon(plan: MixPlan): RibbonPiece[] {
  const { start_beat: w0, end_beat: w1 } = plan.window;
  const fader = (d: Deck, beat: number) => (beat >= d.play_from_beat && beat < d.play_to_beat ? laneValue(d.lanes.volume, beat) : 0);
  // the bass's own path, without the fader: a fade isn't a bass swap
  const bass = (d: Deck, beat: number) => dbLinear(laneValue(d.lanes.eq_low, beat)) * (d.stem_lanes ? laneValue(d.stem_lanes.gain_bass, beat) : 1);
  const busy = (d: Deck, beat: number) => laneValue(d.lanes.echo_send, beat) > 0.1 || laneValue(d.lanes.reverb_send, beat) > 0.1 || Math.abs(laneValue(d.lanes.filter, beat)) > 0.1;
  const pieces: RibbonPiece[] = [];
  for (let beat = Math.floor(w0); beat < w1; beat++) {
    const mid = beat + 0.5;
    const onA = fader(plan.a, mid) > AUDIBLE;
    const onB = fader(plan.b, mid) > AUDIBLE;
    let kind: RibbonKind;
    if (onA && onB) {
      const moving = (d: Deck) => Math.abs(bass(d, beat) - bass(d, beat + 1)) > 0.01;
      if (moving(plan.a) || moving(plan.b)) kind = "swap";
      else if (busy(plan.a, mid) || busy(plan.b, mid)) kind = "fx";
      else kind = "both";
    } else {
      kind = onA ? "a" : onB ? "b" : "gap";
    }
    const last = pieces[pieces.length - 1];
    if (last?.kind === kind) last.to = Math.min(w1, beat + 1);
    else pieces.push({ kind, from: Math.max(w0, beat), to: Math.min(w1, beat + 1) });
  }
  return pieces;
}
