/**
 * The master chain, live. The renderer's leveller and limiter look ahead as far as they like (zero-phase). Here:
 * - the same leveller law, causal: it reacts about a second late. Below its threshold it's the same flat make-up gain.
 * - the same limiter envelope (minimum over 25 ms, then averaged), with the 25 ms as real latency and on sample peaks
 *   rather than 4x true peaks. It costs nothing until a sample goes over the ceiling.
 * #58 replaces both with the real master chain.
 */
import { CEILING_DBTP, DECK_LUFS, LEVEL_RATIO, LEVEL_THRESHOLD_DB, MAKEUP_DB, SR } from "./constants";
import { Biquad } from "./dsp";

const HOP = 1024;
const POWER_BLOCKS = Math.max(1, Math.trunc((1.5 * SR) / HOP));
const REDUCTION_BLOCKS = Math.max(1, Math.trunc((1 * SR) / HOP));
const REF_POWER = Math.pow(10, (DECK_LUFS + 0.691) / 10);
export const MAKEUP = Math.pow(10, MAKEUP_DB / 20);

function kWeighting(): [Biquad, Biquad] {
  const a = Math.pow(10, 4 / 40);
  let w = (2 * Math.PI * 1500) / SR;
  let al = Math.sin(w) / (2 * Math.SQRT1_2);
  let c = Math.cos(w);
  const sa = 2 * Math.sqrt(a) * al;
  const a0 = a + 1 - (a - 1) * c + sa;
  const shelf = new Biquad();
  shelf.b0 = (a * (a + 1 + (a - 1) * c + sa)) / a0;
  shelf.b1 = (-2 * a * (a - 1 + (a + 1) * c)) / a0;
  shelf.b2 = (a * (a + 1 + (a - 1) * c - sa)) / a0;
  shelf.a1 = (2 * (a - 1 - (a + 1) * c)) / a0;
  shelf.a2 = (a + 1 - (a - 1) * c - sa) / a0;
  w = (2 * Math.PI * 38) / SR;
  al = Math.sin(w) / (2 * 0.5);
  c = Math.cos(w);
  const hp = new Biquad();
  hp.b0 = (1 + c) / 2 / (1 + al);
  hp.b1 = -(1 + c) / (1 + al);
  hp.b2 = hp.b0;
  hp.a1 = (-2 * c) / (1 + al);
  hp.a2 = (1 - al) / (1 + al);
  return [shelf, hp];
}

/** Slow gain on the dry decks' blend bump: above the deck reference + threshold, LEVEL_RATIO:1, then make-up. */
export class Leveller {
  private readonly weighting = kWeighting();
  private readonly powers = new Float64Array(POWER_BLOCKS);
  private readonly reductions = new Float64Array(REDUCTION_BLOCKS);
  private readonly wL = new Float64Array(HOP);
  private readonly wR = new Float64Array(HOP);
  private powerSum = 0;
  private reductionSum = 0;
  private filled = 0;
  private blockIndex = 0;
  private from = MAKEUP;
  private to = MAKEUP;

  /** Fills `gain[0..n)` and takes in the dry mix. Works in hops, so n is at most HOP. */
  run(left: Float64Array, right: Float64Array, n: number, gain: Float64Array): void {
    let i = 0;
    while (i < n) {
      const take = Math.min(n - i, HOP - this.filled);
      for (let s = 0; s < take; s++) {
        const u = (this.filled + s) / HOP;
        gain[i + s] = this.from + (this.to - this.from) * u;
        this.wL[this.filled + s] = left[i + s];
        this.wR[this.filled + s] = right[i + s];
      }
      this.filled += take;
      i += take;
      if (this.filled === HOP) this.closeHop();
    }
  }

  private closeHop(): void {
    const { wL, wR } = this;
    for (const f of this.weighting) f.run(wL, wR, 0, HOP);
    let power = 0;
    for (let s = 0; s < HOP; s++) power += wL[s] * wL[s] + wR[s] * wR[s];
    power /= HOP;
    const index = this.blockIndex % POWER_BLOCKS;
    this.powerSum += power - this.powers[index];
    this.powers[index] = power;
    const average = Math.max(this.powerSum, 0) / POWER_BLOCKS;
    const level = 10 * Math.log10(average + 1e-12) - 10 * Math.log10(REF_POWER);
    const reduction = -Math.max(level - LEVEL_THRESHOLD_DB, 0) * (1 - 1 / LEVEL_RATIO);
    const r = this.blockIndex % REDUCTION_BLOCKS;
    this.reductionSum += reduction - this.reductions[r];
    this.reductions[r] = reduction;
    this.blockIndex++;
    this.from = this.to;
    const smooth = this.reductionSum / REDUCTION_BLOCKS;
    this.to = Math.abs(smooth) < 1e-9 ? MAKEUP : Math.pow(10, (smooth + MAKEUP_DB) / 20);
    this.filled = 0;
  }
}

const WIDTH = Math.trunc(0.025 * SR) | 1;
/** Samples between what goes into the limiter and what comes out. */
export const LATENCY = WIDTH - 1;
const CEILING = Math.pow(10, CEILING_DBTP / 20);

export class Limiter {
  private readonly lineL = new Float64Array(LATENCY);
  private readonly lineR = new Float64Array(LATENCY);
  private write = 0;
  private readonly required = new Float64Array(WIDTH); // minimum-filter deque, by arrival
  private readonly requiredAt = new Float64Array(WIDTH);
  private head = 0;
  private tail = 0;
  private readonly smooth = new Float64Array(WIDTH);
  private smoothIndex = 0;
  private smoothSum = WIDTH;
  private clock = 0;
  private busyFor = 0;
  /** Deepest reduction since the last read, in dB (a meter, and a warning that the blend is too loud). */
  reduction = 0;

  constructor() {
    this.smooth.fill(1);
  }

  /** Takes in samples and writes them out [offset..offset + n) in Float32 for the context: delayed by LATENCY. */
  run(left: Float64Array, right: Float64Array, n: number, outL: Float32Array, outR: Float32Array, offset: number): void {
    const { lineL, lineR, required, requiredAt, smooth } = this;
    for (let i = 0; i < n; i++) {
      const l = left[i];
      const r = right[i];
      const peak = Math.max(Math.abs(l), Math.abs(r));
      const over = peak > CEILING;
      if (over) this.busyFor = 2 * WIDTH + 2;
      let gain = 1;
      if (this.busyFor > 0) {
        const clock = ++this.clock;
        if (over) {
          const need = CEILING / peak;
          while (this.tail !== this.head && required[(this.tail + WIDTH - 1) % WIDTH] >= need) {
            this.tail = (this.tail + WIDTH - 1) % WIDTH;
          }
          required[this.tail] = need;
          requiredAt[this.tail] = clock;
          this.tail = (this.tail + 1) % WIDTH;
        }
        while (this.tail !== this.head && requiredAt[this.head] <= clock - WIDTH) this.head = (this.head + 1) % WIDTH;
        const lowest = this.tail !== this.head ? required[this.head] : 1;
        this.smoothSum += lowest - smooth[this.smoothIndex];
        smooth[this.smoothIndex] = lowest;
        this.smoothIndex = (this.smoothIndex + 1) % WIDTH;
        gain = this.smoothSum / WIDTH;
        if (--this.busyFor === 0) {
          smooth.fill(1);
          this.smoothSum = WIDTH;
          this.head = this.tail = 0;
        }
      }
      const w = this.write;
      const dl = lineL[w] * gain;
      const dr = lineR[w] * gain;
      lineL[w] = l;
      lineR[w] = r;
      this.write = w + 1 === LATENCY ? 0 : w + 1;
      if (gain < 1) this.reduction = Math.max(this.reduction, -20 * Math.log10(gain));
      outL[offset + i] = dl > CEILING ? CEILING : dl < -CEILING ? -CEILING : dl;
      outR[offset + i] = dr > CEILING ? CEILING : dr < -CEILING ? -CEILING : dr;
    }
  }

  clear(): void {
    this.lineL.fill(0);
    this.lineR.fill(0);
  }
}
