/**
 * The beatgrid as the overlays read it: beat and bar times on a piecewise grid (`TrackAnalysis.grid`, the segments of
 * `seedbox2/analysis/grid.py`) or, without one, the constant grid of `first_downbeat_s` and `bar_s`; and the phrase
 * level (8, 16 or 32 bars) a bar opens. Beats count from the first downbeat, so bar k starts at beat 4k.
 */
import type { GridSegment, TrackAnalysis } from "@/api/types";

export type GridSource = Pick<TrackAnalysis, "first_downbeat_s" | "bar_s"> & { grid?: GridSegment[] | null };
type PhraseSource = Pick<TrackAnalysis, "bar_count" | "phrase_offset" | "phrases">;

const BEATS_PER_BAR = 4;

/** Whether the analysis has a grid at all. */
export const hasGrid = (an: GridSource | null | undefined): an is GridSource => !!an && (an.bar_s > 0 || !!an.grid?.length);

/** The last segment starting at or before `x` (by `key`), or the first one. */
function segAt(segs: GridSegment[], x: number, key: "start_beat" | "start_s"): GridSegment {
  let lo = 0;
  let hi = segs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (segs[mid][key] <= x) lo = mid;
    else hi = mid - 1;
  }
  return segs[lo];
}

const segTime = (segs: GridSegment[], beat: number) => {
  const s = segAt(segs, beat, "start_beat");
  return s.start_s + ((beat - s.start_beat) * 60) / s.bpm;
};
const segBeat = (segs: GridSegment[], t: number) => {
  const s = segAt(segs, t, "start_s");
  return s.start_beat + ((t - s.start_s) * s.bpm) / 60;
};

/** The grid's beat index of the first downbeat, per segment list (the grid's beat 0 needn't be a downbeat). */
const downbeatIndex = new WeakMap<GridSegment[], Map<number, number>>();
function firstBeat(an: GridSource, segs: GridSegment[]): number {
  let m = downbeatIndex.get(segs);
  if (!m) downbeatIndex.set(segs, (m = new Map()));
  let b = m.get(an.first_downbeat_s);
  if (b == null) m.set(an.first_downbeat_s, (b = Math.round(segBeat(segs, an.first_downbeat_s))));
  return b;
}

const segments = (an: GridSource): GridSegment[] | null => (an.grid?.length ? an.grid : null);

/** File time of beat `b` (fractional), counted from the first downbeat. */
export function beatTime(an: GridSource, b: number): number {
  const segs = segments(an);
  if (segs) return segTime(segs, firstBeat(an, segs) + b);
  return an.first_downbeat_s + (b * an.bar_s) / BEATS_PER_BAR;
}

/** Beat position (fractional) of file time `t`, counted from the first downbeat. The inverse of `beatTime`. */
export function beatAt(an: GridSource, t: number): number {
  const segs = segments(an);
  if (segs) return segBeat(segs, t) - firstBeat(an, segs);
  return ((t - an.first_downbeat_s) / an.bar_s) * BEATS_PER_BAR;
}

/** File time of bar k's downbeat. */
export const barTime = (an: GridSource, k: number): number => beatTime(an, k * BEATS_PER_BAR);

/** Bar position (fractional) of file time `t`. */
export const barAt = (an: GridSource, t: number): number => beatAt(an, t) / BEATS_PER_BAR;

const levelMaps = new WeakMap<object, Map<number, number>>();

/**
 * The length of the phrase bar k opens: 32, 16 or 8, or 0 when no phrase starts there. From `phrases` when analysed;
 * otherwise the 8-bar grid from `phrase_offset`, a 16 every other phrase and a 32 every fourth.
 */
export function phraseLevel(an: PhraseSource, k: number): 0 | 8 | 16 | 32 {
  const list = an.phrases;
  if (list?.length) {
    let m = levelMaps.get(list);
    if (!m) levelMaps.set(list, (m = new Map(list.map((p) => [p.bar, p.level]))));
    return (m.get(k) ?? 0) as 0 | 8 | 16 | 32;
  }
  const off = ((an.phrase_offset % 8) + 8) % 8;
  const r = k - off;
  if (r < 0 || r % 8 !== 0) return 0;
  return r % 32 === 0 ? 32 : r % 16 === 0 ? 16 : 8;
}

/** The phrase starts as bars, in order: `phrases` when analysed, or the 8-bar grid. */
export function phraseStarts(an: PhraseSource): number[] {
  if (an.phrases?.length) return an.phrases.map((p) => p.bar);
  const off = ((an.phrase_offset % 8) + 8) % 8;
  const out: number[] = [];
  for (let k = off; k < an.bar_count; k += 8) out.push(k);
  return out;
}
