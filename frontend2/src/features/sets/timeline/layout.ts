/**
 * Where everything sits on the set's own clock. Pure, so a reorder lays out in the same frame it's dropped.
 *
 * A track is a run of pieces, each a stretch of its file on a stretch of the set: the transition it comes in on, its body
 * at its own speed and the transition it leaves on. In a transition both tracks last what the plan's master clock says,
 * so their beats line up however far they're pitched. A transition without a plan yet is estimated, and marked.
 */
import type { SetOrderTrack, SetTimelineData, SetTransition, TimelineTrack, TimelineTransition, TimelineTransitionState } from "@/api/types";

export interface Piece {
  /** On the set, seconds. */
  t0: number;
  t1: number;
  /** In the file, seconds. */
  f0: number;
  f1: number;
}

export interface PlacedTrack {
  position: number;
  order: SetOrderTrack;
  view: TimelineTrack | null;
  start: number;
  end: number;
  /** 0 or 1: neighbours take turns, so they overlap without covering each other. */
  lane: number;
  pieces: Piece[];
}

export interface PlacedTransition {
  /** Between tracks `position` and `position + 1`. */
  position: number;
  from: string;
  to: string;
  t0: number;
  t1: number;
  state: TimelineTransitionState;
  /** Why it `failed`. */
  error?: string;
  technique: string | null;
  bars: number | null;
  edited: boolean;
  /** The placement in the files, the plan's or the estimate. */
  aIn: number;
  aOut: number;
  bIn: number;
  bOut: number;
  /** Why the order costs what it does here. */
  explained: SetTransition | null;
}

export interface TimelineLayout {
  tracks: PlacedTrack[];
  transitions: PlacedTransition[];
  total: number;
}

const ESTIMATE_BARS = 16;
const FALLBACK_BAR_S = 2;
const FALLBACK_TRACK_S = 240;

const barOf = (t: TimelineTrack | null, order: SetOrderTrack): number => t?.bar_s ?? (order.bpm ? 240 / order.bpm : FALLBACK_BAR_S);
const lengthOf = (t: TimelineTrack | null): number => (t && t.duration_s > 0 ? t.duration_s : FALLBACK_TRACK_S);

interface Placement {
  seconds: number;
  aIn: number;
  aOut: number;
  bIn: number;
  bOut: number;
}

/** What a transition's place would be without a plan: 16 bars of A's end over the start of B, short tracks permitting. */
export function estimatePlacement(a: TimelineTrack | null, aOrder: SetOrderTrack, b: TimelineTrack | null): Placement {
  const lenA = lengthOf(a);
  const seconds = Math.min(ESTIMATE_BARS * barOf(a, aOrder), lenA / 3, lengthOf(b) / 3);
  return { seconds, aIn: lenA - seconds, aOut: lenA, bIn: 0, bOut: seconds };
}

function planned(t: TimelineTransition | undefined): Placement | null {
  if (t?.state !== "ready" || !t.seconds || t.a_in_s === undefined || t.b_in_s === undefined) return null;
  // a loop can end a transition earlier in the file than it began: what's drawn then runs at the set's speed
  const aOut = t.a_out_s !== undefined && t.a_out_s > t.a_in_s ? t.a_out_s : t.a_in_s + t.seconds;
  const bOut = t.b_out_s !== undefined && t.b_out_s > t.b_in_s ? t.b_out_s : t.b_in_s + t.seconds;
  return { seconds: t.seconds, aIn: t.a_in_s, aOut, bIn: t.b_in_s, bOut };
}

export function layoutTimeline(order: SetOrderTrack[], explained: SetTransition[], data: SetTimelineData | null): TimelineLayout {
  const views = new Map(data?.tracks.map((t) => [t.hash, t]));
  const known = new Map(data?.transitions.map((t) => [`${t.from}/${t.to}`, t]));
  const transitions: PlacedTransition[] = [];
  const tracks: PlacedTrack[] = [];
  let clock = 0;
  let enter: Placement | null = null;
  order.forEach((o, i) => {
    const view = views.get(o.hash) ?? null;
    const next = order[i + 1];
    const pieces: Piece[] = [];
    const start = clock;
    let file = 0;
    if (enter) {
      pieces.push({ t0: clock, t1: clock + enter.seconds, f0: enter.bIn, f1: enter.bOut });
      clock += enter.seconds;
      file = enter.bOut;
    }
    let leave: Placement | null = null;
    let item: TimelineTransition | undefined;
    if (next) {
      item = known.get(`${o.hash}/${next.hash}`);
      leave = planned(item) ?? estimatePlacement(view, o, views.get(next.hash) ?? null);
    }
    const bodyEnd = Math.max(file, leave ? leave.aIn : lengthOf(view));
    if (bodyEnd > file) {
      pieces.push({ t0: clock, t1: clock + bodyEnd - file, f0: file, f1: bodyEnd });
      clock += bodyEnd - file;
    }
    if (leave && next) {
      const t0 = clock;
      pieces.push({ t0, t1: t0 + leave.seconds, f0: leave.aIn, f1: leave.aOut });
      transitions.push({
        position: i,
        from: o.hash,
        to: next.hash,
        t0,
        t1: t0 + leave.seconds,
        state: item?.state ?? "pending",
        error: item?.error,
        technique: item?.technique ?? null,
        bars: item?.bars ?? null,
        edited: !!item?.edited,
        aIn: leave.aIn,
        aOut: leave.aOut,
        bIn: leave.bIn,
        bOut: leave.bOut,
        explained: explained[i] ?? null,
      });
    }
    tracks.push({ position: i, order: o, view, start, end: leave ? clock + leave.seconds : clock, lane: i % 2, pieces });
    enter = leave;
  });
  return { tracks, transitions, total: tracks.length ? tracks[tracks.length - 1].end : 0 };
}

/** The file time this track plays at set time `t`, held at its ends. */
export function fileAt(track: PlacedTrack, t: number): number {
  for (const p of track.pieces) if (t <= p.t1) return t <= p.t0 ? p.f0 : p.f0 + ((t - p.t0) / (p.t1 - p.t0)) * (p.f1 - p.f0);
  return track.pieces.length ? track.pieces[track.pieces.length - 1].f1 : 0;
}

/** The set time this track plays file time `f`, held at its ends. */
export function setTimeOf(track: PlacedTrack, f: number): number {
  for (const p of track.pieces) if (f <= p.f1) return f <= p.f0 ? p.t0 : p.t0 + ((f - p.f0) / (p.f1 - p.f0)) * (p.t1 - p.t0);
  return track.end;
}

const midpoint = (t: PlacedTrack) => (t.start + t.end) / 2;

/**
 * An arc's time (0-1) counts tracks, not seconds: the solver asks each position for `arc(position / (n - 1))`. So time on
 * the arc runs from the first track's middle to the last one's, evenly per track however long each is.
 */
export function arcTimeAt(layout: TimelineLayout, t: number): number {
  const { tracks } = layout;
  if (tracks.length < 2) return 0;
  const last = tracks.length - 1;
  if (t <= midpoint(tracks[0])) return 0;
  for (let i = 0; i < last; i++) {
    const a = midpoint(tracks[i]);
    const b = midpoint(tracks[i + 1]);
    if (t <= b) return (i + (t - a) / (b - a)) / last;
  }
  return 1;
}

/** The set time of an arc's time: the inverse of `arcTimeAt`. */
export function timeOfArc(layout: TimelineLayout, arcTime: number): number {
  const { tracks } = layout;
  if (!tracks.length) return 0;
  const last = tracks.length - 1;
  const at = Math.min(last, Math.max(0, arcTime * last));
  const i = Math.min(last, Math.floor(at));
  const a = midpoint(tracks[i]);
  return i === last ? a : a + (at - i) * (midpoint(tracks[i + 1]) - a);
}

/** The position a track dragged to set time `t` lands on. */
export function dropPosition(layout: TimelineLayout, from: number, t: number): number {
  let to = 0;
  for (const track of layout.tracks) if (track.position !== from && midpoint(track) < t) to++;
  return to;
}

/**
 * The running order with the track at `from` moved to position `to`. Pinned positions keep their tracks and the others
 * flow around them; a pinned track doesn't move. The same order as before comes back as null.
 */
export function moveTrack<T>(items: readonly T[], from: number, to: number, pinned: ReadonlySet<number> = new Set()): T[] | null {
  if (pinned.has(from) || from < 0 || from >= items.length) return null;
  const free = items.map((_, i) => i).filter((i) => !pinned.has(i));
  const moved = free.map((i) => items[i]);
  const was = free.indexOf(from);
  const target = Math.min(free.length - 1, free.filter((i) => i < Math.min(to, items.length - 1)).length);
  if (was === target) return null;
  const [item] = moved.splice(was, 1);
  moved.splice(target, 0, item);
  const out = items.slice();
  free.forEach((i, k) => {
    out[i] = moved[k];
  });
  return out;
}
