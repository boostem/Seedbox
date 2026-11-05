/**
 * A lane as the audio hears it: sampled every CTL samples, steps softened by a centred moving average, interpolated to
 * samples (the renderer's `_ctl`). A centred average looks ahead, which is fine: a lane is a function of the clock, not
 * of the signal. A running sum keeps it to one lane lookup per control point, however long the average is.
 */
import { Clock, Lane } from "./clock";
import { CTL, pyRound, SR } from "./constants";

const RING = 1024; // control points kept: enough for what the filter looks ahead (24 of its blocks)

/** Samples a live edit takes to reach its new values: 5 ms. */
export const EDIT_FADE = pyRound(0.005 * SR);

export class ControlLane {
  lane: Lane;
  private readonly width: number;
  private readonly left: number;
  private readonly right: number;
  private readonly points: number;
  private ring = new Float64Array(RING);
  private lo = 0;
  private hi = -1;
  private sum = 0;
  /** The lane before a live edit, for the 5 ms that fades from it: kept and reused, drags come 60 times a second. */
  private previous: ControlLane | null = null;
  private fading = false;
  private fadeFrom = 0;

  /** The lane over `size` samples from master sample `start`. */
  constructor(
    lane: Lane,
    private readonly clock: Clock,
    private readonly start: number,
    private readonly size: number,
    private readonly smoothS: number,
  ) {
    this.lane = lane;
    this.width = Math.max(1, pyRound((smoothS * SR) / CTL));
    this.left = this.width >> 1;
    this.right = this.width - this.left - 1;
    this.points = Math.ceil(size / CTL) + 1;
  }

  get constant(): boolean {
    return this.lane.constant && !this.fading;
  }

  /** A live edit: this lane from now on, faded to over EDIT_FADE samples from sample `at` of the lane's range. */
  retarget(lane: Lane, at: number): void {
    const old = (this.previous ??= new ControlLane(lane, this.clock, this.start, this.size, this.smoothS));
    const ring = old.ring;
    old.lane = this.lane;
    old.ring = this.ring;
    old.lo = this.lo;
    old.hi = this.hi;
    old.sum = this.sum;
    this.ring = ring;
    this.lane = lane;
    this.lo = 0;
    this.hi = -1;
    this.sum = 0;
    this.fading = true;
    this.fadeFrom = at;
  }

  /** The edit's fade has played out. */
  settle(): void {
    this.fading = false;
  }

  private raw(k: number): number {
    const i = k < 0 ? 0 : k >= this.points ? this.points - 1 : k;
    return this.lane.at(this.clock.beat((this.start + i * CTL) / SR));
  }

  private point(k: number): number {
    if (k >= this.lo && k <= this.hi) return this.ring[k % RING];
    if (k < this.lo && this.hi >= 0) {
      // behind the ring (an effect reading the past): worked out on its own, leaving the running sum alone
      let total = 0;
      for (let j = k - this.left; j <= k + this.right; j++) total += this.raw(j);
      return total / this.width;
    }
    if (this.hi >= 0 && k - this.hi < RING) {
      for (let j = this.hi + 1; j <= k; j++) {
        this.sum += this.raw(j + this.right) - this.raw(j - this.left - 1);
        this.ring[j % RING] = this.sum / this.width;
      }
    } else {
      this.sum = 0;
      for (let j = k - this.left; j <= k + this.right; j++) this.sum += this.raw(j);
      this.lo = k;
      this.ring[k % RING] = this.sum / this.width;
    }
    this.hi = k;
    if (this.hi - this.lo >= RING) this.lo = this.hi - RING + 1;
    return this.ring[k % RING];
  }

  /** The value at sample `i` of the lane's range (past the end: its last). */
  at(i: number): number {
    if (this.fading && i < this.fadeFrom + EDIT_FADE) {
      const old = this.previous!.at(i);
      if (i <= this.fadeFrom) return old;
      return old + ((this.plain(i) - old) * (i - this.fadeFrom)) / EDIT_FADE;
    }
    return this.plain(i);
  }

  private plain(i: number): number {
    if (this.lane.constant) return this.lane.values[0];
    const s = i >= this.size ? this.size - 1 : i;
    const k = s >> 4;
    const a = this.point(k);
    const b = this.point(k + 1);
    return a + ((b - a) * (s & (CTL - 1))) / CTL;
  }

  /** Values of samples [i, i + n) into `out` from `offset`. True if they're all one value (for cheap gain stages). */
  fill(i: number, n: number, out: Float64Array, offset = 0): boolean {
    if (this.fading && i < this.fadeFrom + EDIT_FADE) {
      for (let o = 0; o < n; o++) out[offset + o] = this.at(i + o);
      return false;
    }
    if (this.lane.constant) {
      out.fill(this.lane.values[0], offset, offset + n);
      return true;
    }
    const end = offset + n;
    let s = i;
    let a = NaN;
    let b = NaN;
    let k = -1;
    let flat = true;
    const first = this.plain(i);
    for (let o = offset; o < end; o++, s++) {
      const c = s >= this.size ? this.size - 1 : s;
      const kk = c >> 4;
      if (kk !== k) {
        k = kk;
        a = this.point(k);
        b = this.point(k + 1);
        if (a !== first || b !== first) flat = false;
      }
      out[o] = a + ((b - a) * (c & (CTL - 1))) / CTL;
    }
    return flat;
  }

  /** Forget the running sum: the playhead jumped. */
  relocate(): void {
    this.hi = -1;
    this.lo = 0;
    this.fading = false;
  }
}
