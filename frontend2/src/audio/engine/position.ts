/** Where a deck is in its file at a master beat, and the spliced source the renderer builds from that (`_source_mapped`). */
import type { Deck } from "../../api/mixplan.generated";
import { pyRound, SEAM_S, SR } from "./constants";

/** The position on the deck's grid: the beat itself, or inside a loop source + ((beat - start) mod length). */
export function deckPosition(deck: Deck, beat: number): number {
  let u = beat;
  for (const lp of deck.loops) {
    if (beat >= lp.start_beat && beat < lp.end_beat) u = lp.source_beat + ((beat - lp.start_beat) % lp.length_beats);
  }
  return u;
}

function segmentAt(deck: Deck, position: number): Deck["grid"][number] {
  let lo = 0;
  let hi = deck.grid.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (deck.grid[mid].start_beat <= position) lo = mid;
    else hi = mid - 1;
  }
  return deck.grid[lo];
}

/** File time (s) at a master beat: `seedbox2.mix.plan.deck_time`. */
export function deckTime(deck: Deck, beat: number): number {
  const u = deckPosition(deck, beat);
  const s = segmentAt(deck, u);
  return s.start_s + ((u - s.start_beat) * 60) / s.bpm;
}

/** The master beat at which a deck without a loop reaches file time `seconds`: `deckTime`'s inverse. The grid's first
 * segment holds before it and its last after it. */
export function beatAtFile(deck: Deck, seconds: number): number {
  let lo = 0;
  let hi = deck.grid.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (deck.grid[mid].start_s <= seconds) lo = mid;
    else hi = mid - 1;
  }
  const s = deck.grid[lo];
  return s.start_beat + ((seconds - s.start_s) * s.bpm) / 60;
}

/** The deck's own tempo at a master beat. */
export function deckBpm(deck: Deck, beat: number): number {
  return segmentAt(deck, deckPosition(deck, beat)).bpm;
}

export interface Piece {
  /** Samples [from, to) of the spliced source are file samples file, file + 1, ... */
  from: number;
  to: number;
  file: number;
  /** A jump: it fades in over the `seam` samples before `from`. */
  seam: boolean;
}

export interface SourceMap {
  pieces: Piece[];
  knotBeats: Float64Array;
  knotSamples: Float64Array;
  length: number;
  seam: number;
}

const NUDGE = 1e-9;

/** (start, end, file time, seconds per beat) of pieces of [b0, b1) on which file time runs straight. */
function segments(deck: Deck, b0: number, b1: number): [number, number, number, number][] {
  const cuts = new Set<number>([b0, b1]);
  for (const lp of deck.loops) {
    const { start_beat: s, end_beat: e, length_beats: n } = lp;
    if (e <= b0 || s >= b1) continue;
    cuts.add(s);
    cuts.add(e);
    const k0 = Math.max(1, Math.ceil((b0 - s) / n));
    const k1 = Math.floor((Math.min(e, b1) - s) / n);
    for (let k = k0; k <= k1; k++) cuts.add(s + k * n);
  }
  let pts = [...cuts].filter((c) => b0 <= c && c <= b1).sort((x, y) => x - y);
  if (deck.grid.length > 1) {
    const more: number[] = [];
    for (let i = 0; i + 1 < pts.length; i++) {
      const u0 = deckPosition(deck, pts[i] + NUDGE) - NUDGE;
      for (let g = 1; g < deck.grid.length; g++) {
        const knot = deck.grid[g].start_beat;
        if (u0 < knot && knot < u0 + (pts[i + 1] - pts[i])) more.push(pts[i] + (knot - u0));
      }
    }
    pts = [...new Set([...pts, ...more])].sort((x, y) => x - y);
  }
  const out: [number, number, number, number][] = [];
  for (let i = 0; i + 1 < pts.length; i++) {
    const [s, e] = [pts[i], pts[i + 1]];
    if (e - s > NUDGE) {
      const spb = 60 / deckBpm(deck, s + NUDGE);
      out.push([s, e, deckTime(deck, s + NUDGE) - NUDGE * spb, spb]);
    }
  }
  return out;
}

/** The deck's audio for master beats [b0, b1), each piece at its own tempo, loops spliced in on equal-power seams. */
export function sourceMap(deck: Deck, b0: number, b1: number): SourceMap {
  const raw = segments(deck, b0, b1);
  const knotBeats = new Float64Array(raw.length + 1);
  const knotSamples = new Float64Array(raw.length + 1);
  knotBeats[0] = b0;
  raw.forEach(([s, e, , spb], i) => {
    knotBeats[i + 1] = e;
    knotSamples[i + 1] = knotSamples[i] + (e - s) * SR * spb;
  });
  const length = pyRound(knotSamples[raw.length]);
  const seam = Math.max(8, pyRound(SEAM_S * SR));
  const pieces: Piece[] = [];
  let prevEnd: number | null = null;
  raw.forEach(([, , ft], i) => {
    const from = pyRound(knotSamples[i]);
    const to = Math.min(length, pyRound(knotSamples[i + 1]));
    if (to <= from) return;
    const file = pyRound(ft * SR);
    const jump = prevEnd !== null && Math.abs(file - prevEnd) > 1 && from >= seam;
    pieces.push({ from, to, file, seam: jump });
    prevEnd = file + (to - from);
  });
  return { pieces, knotBeats, knotSamples, length, seam };
}

/** `_interp_ext`: the piecewise-linear map, extended past both ends with the end pieces' slopes. */
export function interpExtended(xp: Float64Array, fp: Float64Array, x: number): number {
  const last = xp.length - 1;
  if (last < 1) return fp[0] ?? 0;
  if (x <= xp[0]) return fp[0] + ((x - xp[0]) * (fp[1] - fp[0])) / (xp[1] - xp[0]);
  if (x >= xp[last]) return fp[last] + ((x - xp[last]) * (fp[last] - fp[last - 1])) / (xp[last] - xp[last - 1]);
  let lo = 0;
  let hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xp[mid] <= x) lo = mid;
    else hi = mid;
  }
  return fp[lo] + ((x - xp[lo]) * (fp[lo + 1] - fp[lo])) / (xp[lo + 1] - xp[lo]);
}
