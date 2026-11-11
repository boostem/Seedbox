/** Where a track time falls in the beat grid: the beat-phase ruler and the bar counter to the next phrase. */
import type { TrackAnalysis } from "@/api/types";
import { barAt, type GridSource, hasGrid } from "./grid";

export interface BeatPhase {
  /** bar index from the first downbeat (negative before it) */
  bar: number;
  /** beat in the bar, 0-3 */
  beat: number;
  /** how far through the beat, 0-1 */
  frac: number;
  /** whole beats until the next phrase starts (at least 1), or null after the last one */
  beatsToPhrase: number | null;
  /** the length in bars of that phrase, when the analysis knows it */
  phraseLevel: number | null;
}

type Grid = GridSource & Pick<TrackAnalysis, "bar_count" | "phrase_offset" | "phrases">;

/** The first phrase start after bar position `rel`: `phrases` when analysed, or the 8-bar grid. */
function nextPhrase(an: Grid, rel: number): { bar: number; level: number | null } | null {
  const list = an.phrases;
  if (list?.length) {
    let lo = 0;
    let hi = list.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (list[mid].bar <= rel) lo = mid + 1;
      else hi = mid;
    }
    return lo < list.length ? { bar: list[lo].bar, level: list[lo].level } : null;
  }
  const off = ((an.phrase_offset % 8) + 8) % 8;
  const bar = Math.max(off, off + (Math.floor((rel - off) / 8) + 1) * 8);
  return bar < an.bar_count ? { bar, level: null } : null;
}

/** The grid position of file time `t`, or null without a grid. */
export function beatPhase(an: Grid, t: number): BeatPhase | null {
  if (!hasGrid(an)) return null;
  const rel = barAt(an, t);
  const bar = Math.floor(rel);
  const beats = (rel - bar) * 4;
  const beat = Math.min(3, Math.floor(beats));
  const next = nextPhrase(an, rel);
  return {
    bar,
    beat,
    frac: beats - beat,
    beatsToPhrase: next ? Math.max(1, Math.ceil((next.bar - rel) * 4 - 1e-9)) : null,
    phraseLevel: next?.level ?? null,
  };
}

/** Beats to go as bars.beats, as DJ software counts: 30 beats is "7.2". */
export function fmtBarsBeats(beats: number): string {
  return `${Math.floor(beats / 4)}.${beats % 4}`;
}
