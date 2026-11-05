/**
 * A deck's source on its way through the stretcher, in time with the master grid (#66).
 *
 * Nothing is measured and nothing is corrected. The plan says which source sample every output sample should be
 * (`sourceAt`), and a stretcher has a fixed latency on each side, so each block feeds it exactly up to where the
 * source will be `outputLatency` samples from now, plus `inputLatency`. The ratio of that block's input to its output is
 * the map's slope there, and any rounding is taken back on the next block, so the error can't add up. Seedbox plays from
 * a plan, not from a live input, so the source can be read ahead, and nothing comes out later than it should.
 *
 * It can only go forwards, so a jump lands here as `relocate`, and the next read starts it again with a seek and enough
 * output, unheard, to fill its overlap. Effects read the recent past from `past`.
 */
import { BLOCK } from "./constants";
import { STRETCH_BUFFER } from "./stretcher";
import type { Stretcher } from "./stretcher";

/** What a stretched deck reads: the sources mixed, with their gains where they'll be heard. */
export interface StretchInput {
  /** The (fractional) spliced-source sample at sample `k` of the deck's range. */
  sourceAt(k: number): number;
  /** Spliced-source samples [from, from + n) into left/right, under the gains at samples [k0, k1] of the deck's range. */
  read(from: number, n: number, k0: number, k1: number, left: Float64Array, right: Float64Array): void;
}

export class StretchedSource {
  private readonly left = new Float64Array(STRETCH_BUFFER);
  private readonly right = new Float64Array(STRETCH_BUFFER);
  private readonly pastL: Float32Array;
  private readonly pastR: Float32Array;
  private readonly discardL = new Float64Array(BLOCK);
  private readonly discardR = new Float64Array(BLOCK);
  private next = NaN; // the deck sample that comes out next
  private fed = 0; // the spliced-source sample that goes in next
  /** The stretcher's whole delay, input side and output side, for where a sample going in now will be heard. */
  private readonly ahead: number;

  constructor(
    readonly stretcher: Stretcher,
    private readonly source: StretchInput,
    /** The pitch against the file's. */
    private readonly shift: number,
    /** Without key lock the pitch also follows the tempo. */
    private readonly followTempo: boolean,
    history: number,
  ) {
    this.ahead = stretcher.outputLatency + stretcher.inputLatency;
    this.pastL = new Float32Array(Math.max(history, BLOCK));
    this.pastR = new Float32Array(this.pastL.length);
    if (!followTempo) stretcher.transpose(shift);
  }

  /** The playhead jumped. */
  relocate(): void {
    this.next = NaN;
  }

  /** How far ahead of the output the input goes, for the gains: it's heard this far after it goes in. */
  private target(k: number): number {
    const { stretcher } = this;
    return this.source.sourceAt(k + stretcher.outputLatency) + stretcher.inputLatency;
  }

  private seat(k: number): void {
    const { stretcher, left, right } = this;
    stretcher.configure(stretcher.profileNow);
    if (!this.followTempo) stretcher.transpose(this.shift);
    const from = k - stretcher.settleSamples;
    const end = Math.round(this.target(from));
    const n = stretcher.seekSamples;
    this.source.read(end - n, n, from + this.ahead - n, from + this.ahead, left, right);
    const inL = stretcher.input(0);
    const inR = stretcher.input(1);
    for (let i = 0; i < n; i++) {
      inL[i] = left[i];
      inR[i] = right[i];
    }
    const rate = (this.target(from + BLOCK) - this.target(from)) / BLOCK;
    stretcher.seek(n, Math.max(rate, 1e-3));
    this.fed = end;
    for (let at = from; at < k; at += BLOCK) this.advance(at, Math.min(BLOCK, k - at), this.discardL, this.discardR, 0, false);
    this.next = k;
  }

  private advance(k: number, m: number, left: Float64Array, right: Float64Array, at: number, keep: boolean): void {
    const { stretcher, ahead } = this;
    const before = this.target(k);
    const after = this.target(k + m);
    if (this.followTempo) stretcher.transpose(((after - before) / m) * this.shift);
    const n = Math.min(Math.max(Math.round(after) - this.fed, 0), STRETCH_BUFFER);
    if (n > 0) {
      this.source.read(this.fed, n, k + ahead, k + m + ahead, this.left, this.right);
      const inL = stretcher.input(0);
      const inR = stretcher.input(1);
      for (let i = 0; i < n; i++) {
        inL[i] = this.left[i];
        inR[i] = this.right[i];
      }
    }
    this.fed = Math.max(this.fed + n, Math.round(after) - STRETCH_BUFFER);
    stretcher.process(n, m);
    const outL = stretcher.output(0);
    const outR = stretcher.output(1);
    for (let i = 0; i < m; i++) {
      left[at + i] = outL[i];
      right[at + i] = outR[i];
    }
    if (keep) {
      const { pastL, pastR } = this;
      const size = pastL.length;
      let w = k % size;
      for (let i = 0; i < m; i++) {
        pastL[w] = outL[i];
        pastR[w] = outR[i];
        if (++w === size) w = 0;
      }
    }
  }

  /** The next samples [k, k + n) of the deck's range (n <= BLOCK) into left/right from `at`. After a jump it starts from k. */
  read(k: number, n: number, left: Float64Array, right: Float64Array, at: number): void {
    if (this.next !== k) this.seat(k);
    this.advance(k, n, left, right, at, true);
    this.next = k + n;
  }

  /**
   * What has come out, for an effect: samples [k, k + n) into left/right from `at`. An effect's linear read can look one
   * sample past what's out, which holds the last one, and further back than `history` is silence.
   */
  past(k: number, n: number, left: Float64Array, right: Float64Array, at: number): void {
    const { pastL, pastR, next } = this;
    const size = pastL.length;
    for (let i = 0; i < n; i++) {
      const s = Math.min(k + i, next - 1);
      if (s >= next - size && s >= 0) {
        left[at + i] = pastL[s % size];
        right[at + i] = pastR[s % size];
      } else {
        left[at + i] = 0;
        right[at + i] = 0;
      }
    }
  }
}
