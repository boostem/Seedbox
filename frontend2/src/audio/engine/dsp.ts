/** The signal stages, each the renderer's (`musicreco/mixrender.py`) as a causal stateful block. No allocation here after construction. */
import {
  ECHO_RETURN,
  FILTER_BLOCK,
  FILTER_HP_MAX_HZ,
  FILTER_HP_MIN_HZ,
  FILTER_LP_MAX_HZ,
  FILTER_LP_MIN_HZ,
  FILTER_Q_BASE,
  FILTER_Q_MAX,
  FILTER_RES_ONSET,
  REVERB_BAND_HZ,
  REVERB_RETURN,
  BLOCK,
  SR,
  XOVER_HIGH_HZ,
  XOVER_LOW_HZ,
} from "./constants";
import type { ControlLane } from "./control";

/** One stereo biquad, transposed direct form II, in double precision like scipy's `sosfilt`. */
export class Biquad {
  b0 = 1;
  b1 = 0;
  b2 = 0;
  a1 = 0;
  a2 = 0;
  private l1 = 0;
  private l2 = 0;
  private r1 = 0;
  private r2 = 0;

  static lowpass(fc: number, q: number): Biquad {
    return new Biquad().setLowpass(fc, q);
  }

  static highpass(fc: number, q: number): Biquad {
    return new Biquad().setHighpass(fc, q);
  }

  setLowpass(fc: number, q: number, gain = 1): this {
    const w = (2 * Math.PI * fc) / SR;
    const c = Math.cos(w);
    const al = Math.sin(w) / (2 * q);
    const a0 = 1 + al;
    this.b0 = (((1 - c) / 2) * gain) / a0;
    this.b1 = ((1 - c) * gain) / a0;
    this.b2 = this.b0;
    this.a1 = (-2 * c) / a0;
    this.a2 = (1 - al) / a0;
    return this;
  }

  setHighpass(fc: number, q: number, gain = 1): this {
    const w = (2 * Math.PI * fc) / SR;
    const c = Math.cos(w);
    const al = Math.sin(w) / (2 * q);
    const a0 = 1 + al;
    this.b0 = (((1 + c) / 2) * gain) / a0;
    this.b1 = (-(1 + c) * gain) / a0;
    this.b2 = this.b0;
    this.a1 = (-2 * c) / a0;
    this.a2 = (1 - al) / a0;
    return this;
  }

  /** The all-pass that makes the isolator's low band line up with the split above it (`_allpass_sos`). */
  setAllpass(fc: number): this {
    const w = (2 * Math.PI * fc) / SR;
    const al = Math.sin(w) / (2 * Math.SQRT1_2);
    const c = Math.cos(w);
    const a0 = 1 + al;
    this.b0 = (1 - al) / a0;
    this.b1 = (-2 * c) / a0;
    this.b2 = 1;
    this.a1 = this.b1;
    this.a2 = this.b0;
    return this;
  }

  reset(): void {
    this.l1 = this.l2 = this.r1 = this.r2 = 0;
  }

  /** In place over [from, to). */
  run(left: Float64Array, right: Float64Array, from: number, to: number): void {
    const { b0, b1, b2, a1, a2 } = this;
    let { l1, l2, r1, r2 } = this;
    for (let i = from; i < to; i++) {
      const x = left[i];
      const y = b0 * x + l1;
      l1 = b1 * x - a1 * y + l2;
      l2 = b2 * x - a2 * y;
      left[i] = y;
      const u = right[i];
      const v = b0 * u + r1;
      r1 = b1 * u - a1 * v + r2;
      r2 = b2 * u - a2 * v;
      right[i] = v;
    }
    this.l1 = Math.abs(l1) < 1e-30 ? 0 : l1;
    this.l2 = Math.abs(l2) < 1e-30 ? 0 : l2;
    this.r1 = Math.abs(r1) < 1e-30 ? 0 : r1;
    this.r2 = Math.abs(r2) < 1e-30 ? 0 : r2;
  }
}

const BUTTERWORTH_Q = Math.SQRT1_2;

/** Isolator band gain: dB below -24 fades to a true kill at -30 (`_iso_gain`). */
export function isoGain(db: number): number {
  const fade = (db + 30) / 6;
  return Math.pow(10, db / 20) * (fade <= 0 ? 0 : fade >= 1 ? 1 : fade);
}

/** The 3-band DJ isolator: Linkwitz-Riley 4th order at 200 Hz and 2.5 kHz, low band all-pass compensated: bands sum flat. */
export class Isolator {
  private readonly low = [
    Biquad.lowpass(XOVER_LOW_HZ, BUTTERWORTH_Q),
    Biquad.lowpass(XOVER_LOW_HZ, BUTTERWORTH_Q),
    new Biquad().setAllpass(XOVER_HIGH_HZ),
  ];
  private readonly rest = [Biquad.highpass(XOVER_LOW_HZ, BUTTERWORTH_Q), Biquad.highpass(XOVER_LOW_HZ, BUTTERWORTH_Q)];
  private readonly mid = [Biquad.lowpass(XOVER_HIGH_HZ, BUTTERWORTH_Q), Biquad.lowpass(XOVER_HIGH_HZ, BUTTERWORTH_Q)];
  private readonly high = [Biquad.highpass(XOVER_HIGH_HZ, BUTTERWORTH_Q), Biquad.highpass(XOVER_HIGH_HZ, BUTTERWORTH_Q)];
  private readonly lowL = new Float64Array(BLOCK);
  private readonly lowR = new Float64Array(BLOCK);
  private readonly midL = new Float64Array(BLOCK);
  private readonly midR = new Float64Array(BLOCK);

  /** In place over [0, n), with per-sample linear band gains. */
  run(left: Float64Array, right: Float64Array, n: number, gl: Float64Array, gm: Float64Array, gh: Float64Array): void {
    const { lowL, lowR, midL, midR } = this;
    lowL.set(left.subarray(0, n));
    lowR.set(right.subarray(0, n));
    for (const f of this.low) f.run(lowL, lowR, 0, n);
    for (const f of this.rest) f.run(left, right, 0, n);
    midL.set(left.subarray(0, n));
    midR.set(right.subarray(0, n));
    for (const f of this.mid) f.run(midL, midR, 0, n);
    for (const f of this.high) f.run(left, right, 0, n);
    for (let i = 0; i < n; i++) {
      left[i] = lowL[i] * gl[i] + midL[i] * gm[i] + left[i] * gh[i];
      right[i] = lowR[i] * gl[i] + midR[i] * gm[i] + right[i] * gh[i];
    }
  }

  reset(): void {
    for (const bank of [this.low, this.rest, this.mid, this.high]) for (const f of bank) f.reset();
  }
}

const LOOKAROUND = 24; // blocks a filter runs before and after it opens, so it starts and ends settled

/** One side of the bipolar filter: two biquads whose coefficients change per FILTER_BLOCK samples. */
class FilterSide {
  readonly s1 = new Biquad();
  readonly s2 = new Biquad();
  activeUntil = -1;
  running = false;
}

/** The bipolar filter (`_dj_filter`): low-pass to 80 Hz below 0, high-pass to 10 kHz above, 24 dB/oct, with resonance. */
export class DjFilter {
  private readonly lp = new FilterSide();
  private readonly hp = new FilterSide();
  private readonly wetL = new Float64Array(BLOCK);
  private readonly wetR = new Float64Array(BLOCK);
  private readonly values = new Float64Array(BLOCK);
  private readonly blocks: number;
  private block = -1;

  constructor(
    private readonly f: ControlLane,
    private readonly res: ControlLane | null,
    private readonly size: number,
  ) {
    this.blocks = (size + FILTER_BLOCK - 1) >> 6;
  }

  private mid(b: number): number {
    return Math.min(b * FILTER_BLOCK + (FILTER_BLOCK >> 1), this.size - 1);
  }

  /** Does this block open the filter on that side? (`sel & (mag >= 0.003)`) */
  private look(b: number): void {
    if (b < 0 || b >= this.blocks) return;
    const v = this.f.at(this.mid(b));
    if (Math.abs(v) < 0.003) return;
    const side = v < 0 ? this.lp : this.hp;
    side.activeUntil = Math.max(side.activeUntil, b + LOOKAROUND);
  }

  private enter(b: number): void {
    if (this.block === -1) {
      for (let k = b - LOOKAROUND; k < b + LOOKAROUND; k++) this.look(k);
    }
    this.block = b;
    this.look(b + LOOKAROUND);
    const v = this.f.at(this.mid(b));
    const mag = Math.abs(v);
    const lpOn = b <= this.lp.activeUntil;
    const hpOn = b <= this.hp.activeUntil;
    if (!lpOn && !hpOn) {
      this.lp.running = false;
      this.hp.running = false;
      return;
    }
    const light = FILTER_Q_BASE + 0.6 * Math.pow(Math.min(1, mag), 1.5);
    const raw = this.res ? Math.min(1, Math.max(0, this.res.at(this.mid(b)))) : 0;
    const r = Math.round(raw * Math.min(1, mag / FILTER_RES_ONSET) * 1000) / 1000;
    const q2 = light + r * Math.max(FILTER_Q_MAX - light, 0);
    const comp = Math.pow(light / q2, 0.25);
    const top = 0.45 * SR;
    if (lpOn) {
      const m = v < 0 ? mag : 0;
      const fc = Math.max(10, Math.min(top, FILTER_LP_MAX_HZ * Math.pow(FILTER_LP_MIN_HZ / FILTER_LP_MAX_HZ, m)));
      this.lp.s1.setLowpass(fc, 0.5412);
      this.lp.s2.setLowpass(fc, q2, comp);
      if (!this.lp.running) {
        this.lp.s1.reset();
        this.lp.s2.reset();
        this.lp.running = true;
      }
    } else {
      this.lp.running = false;
    }
    if (hpOn) {
      const m = v > 0 ? mag : 0;
      const fc = Math.max(10, Math.min(top, FILTER_HP_MIN_HZ * Math.pow(FILTER_HP_MAX_HZ / FILTER_HP_MIN_HZ, m)));
      this.hp.s1.setHighpass(fc, 0.5412);
      this.hp.s2.setHighpass(fc, q2, comp);
      if (!this.hp.running) {
        this.hp.s1.reset();
        this.hp.s2.reset();
        this.hp.running = true;
      }
    } else {
      this.hp.running = false;
    }
  }

  private side(side: FilterSide, sign: number, left: Float64Array, right: Float64Array, a: number, b: number): void {
    const { wetL, wetR, values } = this;
    for (let i = a; i < b; i++) {
      wetL[i] = left[i];
      wetR[i] = right[i];
    }
    side.s1.run(wetL, wetR, a, b);
    side.s2.run(wetL, wetR, a, b);
    for (let i = a; i < b; i++) {
      const v = values[i] * sign;
      if (v <= 0) continue;
      const w = v >= 0.03 ? 1 : v / 0.03;
      left[i] = left[i] * (1 - w) + wetL[i] * w;
      right[i] = right[i] * (1 - w) + wetR[i] * w;
    }
  }

  /** In place over [0, n); sample 0 is sample `start` of the deck's range. */
  run(left: Float64Array, right: Float64Array, start: number, n: number): void {
    this.f.fill(start, n, this.values);
    let i = 0;
    while (i < n) {
      const sample = start + i;
      const b = sample >> 6;
      if (b !== this.block) this.enter(b);
      const end = Math.min(n, i + FILTER_BLOCK - (sample & (FILTER_BLOCK - 1)));
      // both sides read the dry signal: a block has only one side's samples unless the lane crosses zero in it
      if (this.lp.running) this.side(this.lp, -1, left, right, i, end);
      if (this.hp.running) this.side(this.hp, 1, left, right, i, end);
      i = end;
    }
  }

  /** The lane changed under a live edit: look around again, with the filter's state left as it is. */
  relook(): void {
    this.block = -1;
  }

  relocate(): void {
    this.block = -1;
    this.lp.running = this.hp.running = false;
    this.lp.activeUntil = this.hp.activeUntil = -1;
  }
}

/** The feedback delay (`_echo`): repeats only, each thinner and darker through a band-limit in the loop. */
export class Echo {
  private readonly lineL: Float64Array;
  private readonly lineR: Float64Array;
  private readonly mask: number;
  private readonly hp = Biquad.highpass(220, BUTTERWORTH_Q);
  private readonly lp = Biquad.lowpass(6000, BUTTERWORTH_Q);
  private readonly tapL = new Float64Array(BLOCK);
  private readonly tapR = new Float64Array(BLOCK);
  private write = 0;

  /** `maxDelay`: the longest delay it will be asked for, in samples. */
  constructor(maxDelay: number) {
    let size = 1;
    while (size < maxDelay + BLOCK + 4) size *= 2;
    this.lineL = new Float64Array(size);
    this.lineR = new Float64Array(size);
    this.mask = size - 1;
  }

  /** Adds the repeats over [0, n) to `outL`/`outR` at `offset`. inL/inR is what the send feeds it. */
  run(
    inL: Float64Array,
    inR: Float64Array,
    feedback: Float64Array,
    delayFrom: number,
    delayTo: number,
    n: number,
    outL: Float64Array,
    outR: Float64Array,
  ): void {
    const { lineL, lineR, mask, tapL, tapR } = this;
    let w = this.write;
    for (let i = 0; i < n; i++) {
      const d = delayFrom + ((delayTo - delayFrom) * i) / n;
      const whole = Math.floor(d);
      const fr = d - whole;
      const p = w + i - whole; // older than this block (a delay is over 1000 samples), so a block is one pass
      const a = (p - 1) & mask;
      const b = p & mask;
      tapL[i] = lineL[a] * fr + lineL[b] * (1 - fr);
      tapR[i] = lineR[a] * fr + lineR[b] * (1 - fr);
    }
    this.hp.run(tapL, tapR, 0, n);
    this.lp.run(tapL, tapR, 0, n);
    for (let i = 0; i < n; i++) {
      const k = (w + i) & mask;
      lineL[k] = inL[i] + tapL[i] * feedback[i];
      lineR[k] = inR[i] + tapR[i] * feedback[i];
      outL[i] += ECHO_RETURN * tapL[i];
      outR[i] += ECHO_RETURN * tapR[i];
    }
    w = (w + n) & mask;
    this.write = w;
  }

  clear(): void {
    this.lineL.fill(0);
    this.lineR.fill(0);
    this.hp.reset();
    this.lp.reset();
  }
}

const COMB_TUNINGS = [1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617];
const ALLPASS_TUNINGS = [556, 441, 341, 225];
const STEREO_SPREAD = 23;

/** The reverb the renderer gets from pedalboard: JUCE's `Reverb`, which is Freeverb, wet only at width 1. */
export class Reverb {
  private readonly combs: Float32Array[] = [];
  private readonly combIndex: Int32Array;
  private readonly combLast: Float64Array;
  private readonly passes: Float32Array[] = [];
  private readonly passIndex: Int32Array;
  private readonly pre = [Biquad.highpass(REVERB_BAND_HZ[0], BUTTERWORTH_Q), Biquad.lowpass(REVERB_BAND_HZ[1], BUTTERWORTH_Q)];
  private readonly feedback: number;
  private readonly damping: number;
  private readonly bufL = new Float64Array(BLOCK);
  private readonly bufR = new Float64Array(BLOCK);
  private readonly wetL = new Float64Array(BLOCK);
  private readonly wetR = new Float64Array(BLOCK);

  constructor(size: number) {
    const room = 0.6 + 0.37 * size;
    const damp = 0.6 - 0.3 * size;
    this.feedback = room * 0.28 + 0.7;
    this.damping = damp * 0.4;
    for (const spread of [0, STEREO_SPREAD]) {
      for (const t of COMB_TUNINGS) this.combs.push(new Float32Array(t + spread));
      for (const t of ALLPASS_TUNINGS) this.passes.push(new Float32Array(t + spread));
    }
    this.combIndex = new Int32Array(this.combs.length);
    this.combLast = new Float64Array(this.combs.length);
    this.passIndex = new Int32Array(this.passes.length);
  }

  /** Adds the wet signal over [0, n) to outL/outR. Reads inL/inR and leaves them alone. */
  run(inL: Float64Array, inR: Float64Array, n: number, outL: Float64Array, outR: Float64Array): void {
    const { bufL, bufR, feedback, damping } = this;
    bufL.set(inL.subarray(0, n));
    bufR.set(inR.subarray(0, n));
    for (const f of this.pre) f.run(bufL, bufR, 0, n);
    const count = COMB_TUNINGS.length;
    const passes = ALLPASS_TUNINGS.length;
    const undamp = 1 - damping;
    for (let side = 0; side < 2; side++) {
      const out = side === 0 ? this.wetL : this.wetR;
      out.fill(0, 0, n);
      for (let c = 0; c < count; c++) {
        const slot = side * count + c;
        const line = this.combs[slot];
        const size = line.length;
        let index = this.combIndex[slot];
        let last = this.combLast[slot];
        for (let i = 0; i < n; i++) {
          const output = line[index];
          last = output * undamp + last * damping;
          line[index] = (bufL[i] + bufR[i]) * 0.015 + last * feedback;
          if (++index === size) index = 0;
          out[i] += output;
        }
        this.combIndex[slot] = index;
        this.combLast[slot] = Math.abs(last) < 1e-30 ? 0 : last;
      }
      for (let p = 0; p < passes; p++) {
        const slot = side * passes + p;
        const line = this.passes[slot];
        const size = line.length;
        let index = this.passIndex[slot];
        for (let i = 0; i < n; i++) {
          const input = out[i];
          const held = line[index];
          line[index] = input + held * 0.5;
          if (++index === size) index = 0;
          out[i] = held - input;
        }
        this.passIndex[slot] = index;
      }
    }
    // width 1: each side is only itself, at wet 1 x JUCE's wet scale of 3
    for (let i = 0; i < n; i++) {
      outL[i] += REVERB_RETURN * 3 * this.wetL[i];
      outR[i] += REVERB_RETURN * 3 * this.wetR[i];
    }
  }

  clear(): void {
    for (const line of this.combs) line.fill(0);
    for (const line of this.passes) line.fill(0);
    this.combLast.fill(0);
    for (const f of this.pre) f.reset();
  }
}
