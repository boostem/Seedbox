/** Master beats and seconds, and lanes as functions of the beat: `_Clock` and `lane_value` of the two Python engines. */
import type { Keyframe } from "../../api/mixplan.generated";

const CODE = { hold: 0, linear: 1, ease: 2 } as const;

/** A lane, compiled for lookups that mostly move forward. */
export class Lane {
  readonly beats: Float64Array;
  readonly values: Float64Array;
  readonly codes: Uint8Array;
  readonly constant: boolean;
  private hint = 0;

  constructor(keyframes: readonly Keyframe[]) {
    const n = keyframes.length;
    this.beats = new Float64Array(n);
    this.values = new Float64Array(n);
    this.codes = new Uint8Array(n);
    let constant = true;
    keyframes.forEach((k, i) => {
      this.beats[i] = k.beat;
      this.values[i] = k.value;
      this.codes[i] = CODE[k.curve];
      if (k.value !== keyframes[0].value) constant = false;
    });
    this.constant = constant;
  }

  /** The value at a beat: constant before the first and after the last keyframe; of two on one beat the later wins. */
  at(beat: number): number {
    const { beats, values, codes } = this;
    const last = beats.length - 1;
    if (this.constant || beat < beats[0]) return values[0];
    let i = this.hint;
    if (beats[i] > beat || (i < last && beats[i + 1] <= beat && (i + 2 > last || beats[i + 2] <= beat))) {
      let lo = 0;
      let hi = last + 1; // the first keyframe after the beat
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (beats[mid] <= beat) lo = mid + 1;
        else hi = mid;
      }
      i = lo - 1;
    } else if (i < last && beats[i + 1] <= beat) {
      i++;
    }
    this.hint = i;
    if (i >= last) return values[last];
    const span = beats[i + 1] - beats[i];
    const code = codes[i];
    if (code === 0 || span <= 0) return values[i];
    const t = (beat - beats[i]) / span;
    return values[i] + (values[i + 1] - values[i]) * (code === 2 ? t * t * (3 - 2 * t) : t);
  }

  /** Is any keyframe off this value? */
  differsFrom(value: number): boolean {
    return this.values.some((v) => v !== value);
  }
}

const STEP = 1 / 128;
const TEMPO_MIN = 40;
const TEMPO_MAX = 300;

function interpAt(xs: Float64Array, ys: Float64Array, x: number, i: number): number {
  return ys[i] + ((ys[i + 1] - ys[i]) * (x - xs[i])) / (xs[i + 1] - xs[i]);
}

function bracket(xs: Float64Array, x: number, hint: number): number {
  const last = xs.length - 2;
  let i = hint;
  if (i > last || xs[i] > x || xs[i + 1] < x) {
    let lo = 0;
    let hi = last;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (xs[mid] <= x) lo = mid;
      else hi = mid - 1;
    }
    i = lo;
  }
  return i;
}

/** Master beat <-> time (s, 0 at the window's start), integrating 60 / tempo(beat) on a table of 1/128 beats. */
export class Clock {
  private readonly grid: Float64Array;
  private readonly seconds: Float64Array;
  private readonly bpms: Float64Array;
  private readonly lo: number;
  private beatHint = 0;
  private timeHint = 0;

  constructor(tempo: readonly Keyframe[], beatLo: number, beatHi: number, windowStart: number) {
    const lane = new Lane(tempo);
    const lo = Math.floor(Math.min(beatLo, windowStart)) - 2;
    const hi = Math.ceil(beatHi) + 2;
    const n = Math.floor((hi - lo) / STEP + 0.5) + 1;
    this.lo = lo;
    this.grid = new Float64Array(n);
    this.bpms = new Float64Array(n);
    this.seconds = new Float64Array(n);
    let spbBefore = 0;
    for (let i = 0; i < n; i++) {
      const beat = lo + i * STEP;
      this.grid[i] = beat;
      this.bpms[i] = Math.min(TEMPO_MAX, Math.max(TEMPO_MIN, lane.at(beat)));
      const spb = 60 / this.bpms[i];
      if (i > 0) this.seconds[i] = this.seconds[i - 1] + ((spb + spbBefore) / 2) * STEP;
      spbBefore = spb;
    }
    const zero = this.interp(this.grid, this.seconds, windowStart, this.indexOfBeat(windowStart));
    for (let i = 0; i < n; i++) this.seconds[i] -= zero;
  }

  private interp(xs: Float64Array, ys: Float64Array, x: number, i: number): number {
    if (x <= xs[0]) return ys[0];
    if (x >= xs[xs.length - 1]) return ys[ys.length - 1];
    return interpAt(xs, ys, x, i);
  }

  private indexOfBeat(beat: number): number {
    return Math.min(this.grid.length - 2, Math.max(0, Math.floor((beat - this.lo) / STEP)));
  }

  /** Seconds from the window's start at a master beat. */
  time(beat: number): number {
    return this.interp(this.grid, this.seconds, beat, this.indexOfBeat(beat));
  }

  /** The master beat at a time. */
  beat(t: number): number {
    this.timeHint = bracket(this.seconds, t, this.timeHint);
    return this.interp(this.seconds, this.grid, t, this.timeHint);
  }

  bpm(beat: number): number {
    this.beatHint = this.indexOfBeat(beat);
    return this.interp(this.grid, this.bpms, beat, this.beatHint);
  }
}
