/**
 * The master chain, live (#58): bus glue, make-up to the target, true-peak limiter. It is `seedbox2/mix/master.py` sample
 * for sample, which is possible because that one is causal too: `tests/test_seedbox2_engine_parity.py` nulls them.
 */
import {
  CEILING_DBTP, DECK_LUFS, DEFAULT_TARGET_LUFS, GLUE_ATTACK_S, GLUE_DETECT_S, GLUE_HOP, GLUE_KNEE_DB, GLUE_RATIO,
  GLUE_RELEASE_S, GLUE_THRESHOLD_DB, LIMIT_WINDOW_S, OPEN_LOOP_DB, OPEN_LOOP_PER_LU, SR, TP_PHASES, TP_TAPS,
} from "./constants";

/** With no render measured, the make-up that lands near the target: the tracks reach the bus with their bodies at DECK_LUFS. */
export function openLoopMakeupDb(targetLufs: number = DEFAULT_TARGET_LUFS): number {
  return targetLufs - DECK_LUFS + OPEN_LOOP_DB + OPEN_LOOP_PER_LU * (targetLufs - DEFAULT_TARGET_LUFS);
}

const pole = (seconds: number) => 1 - Math.exp(-GLUE_HOP / (seconds * SR));
const DETECT = pole(GLUE_DETECT_S);
const ATTACK = pole(GLUE_ATTACK_S);
const RELEASE = pole(GLUE_RELEASE_S);

/** 2:1 feed-forward bus compressor, stereo linked. A level every GLUE_HOP samples sets the gain the next hop ramps to. */
export class Glue {
  private env = 0;
  private reductionDb = 0;
  private from = 1;
  private to = 1;
  private filled = 0;
  private power = 0;
  /** Deepest reduction since the last read, in dB. */
  deepest = 0;

  /** Fills `gain[0..n)` and takes in the audio. */
  run(left: Float64Array, right: Float64Array, n: number, gain: Float64Array): void {
    for (let i = 0; i < n; i++) {
      gain[i] = this.from + ((this.to - this.from) * this.filled) / GLUE_HOP;
      this.power += (left[i] * left[i] + right[i] * right[i]) / 2;
      if (++this.filled === GLUE_HOP) this.closeHop();
    }
  }

  private closeHop(): void {
    this.env += DETECT * (this.power / GLUE_HOP - this.env);
    this.power = 0;
    this.filled = 0;
    const over = 10 * Math.log10(this.env + 1e-12) - GLUE_THRESHOLD_DB;
    const half = GLUE_KNEE_DB / 2;
    const slope = 1 - 1 / GLUE_RATIO;
    let want = 0;
    if (over >= half) want = over * slope;
    else if (over > -half) want = (slope * (over + half) * (over + half)) / (2 * GLUE_KNEE_DB);
    this.reductionDb += (want > this.reductionDb ? ATTACK : RELEASE) * (want - this.reductionDb);
    if (this.reductionDb > this.deepest) this.deepest = this.reductionDb;
    this.from = this.to;
    this.to = Math.pow(10, -this.reductionDb / 20);
  }

  /** After a seek: what was playing says nothing about what comes next. */
  reset(): void {
    this.env = this.reductionDb = this.power = this.filled = 0;
    this.from = this.to = 1;
  }
}

const TP_DELAY = TP_TAPS / 2;
/** The Hann-windowed sinc that reads between samples, for phases 1..3; tap m is the sample m back. */
const TP_FILTERS: Float64Array[] = [];
for (let phase = 1; phase < TP_PHASES; phase++) {
  const h = new Float64Array(TP_TAPS);
  let sum = 0;
  for (let m = 0; m < TP_TAPS; m++) {
    const o = TP_DELAY - phase / TP_PHASES - m;
    const sinc = o === 0 ? 1 : Math.sin(Math.PI * o) / (Math.PI * o);
    h[m] = sinc * (0.5 + 0.5 * Math.cos((Math.PI * o) / TP_DELAY));
    sum += h[m];
  }
  for (let m = 0; m < TP_TAPS; m++) h[m] /= sum;
  TP_FILTERS.push(h);
}

const WIDTH = Math.trunc(LIMIT_WINDOW_S * SR) | 1;
/** Samples between what goes into the limiter and what comes out. */
export const LATENCY = WIDTH - 1 + TP_DELAY;
const CEILING = Math.pow(10, CEILING_DBTP / 20);

/** Look-ahead true-peak limiter. It costs a peak reading per sample, and the envelope only once one goes over the ceiling. */
export class Limiter {
  private readonly lineL = new Float64Array(LATENCY);
  private readonly lineR = new Float64Array(LATENCY);
  private write = 0;
  // the last TP_TAPS samples, twice over so a read never wraps
  private readonly tapsL = new Float64Array(TP_TAPS * 2);
  private readonly tapsR = new Float64Array(TP_TAPS * 2);
  private tapAt = 0;
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

  /** The true peak of the sample TP_DELAY back, having just taken in a new one. */
  private peak(): number {
    const { tapsL, tapsR } = this;
    // newest first: tap m reads [at + m]
    const at = this.tapAt;
    let peak = Math.max(Math.abs(tapsL[at + TP_DELAY]), Math.abs(tapsR[at + TP_DELAY]));
    for (let f = 0; f < TP_FILTERS.length; f++) {
      const h = TP_FILTERS[f];
      let l = 0;
      let r = 0;
      for (let m = 0; m < TP_TAPS; m++) {
        l += h[m] * tapsL[at + m];
        r += h[m] * tapsR[at + m];
      }
      if (l < 0) l = -l;
      if (r < 0) r = -r;
      if (l > peak) peak = l;
      if (r > peak) peak = r;
    }
    return peak;
  }

  /** Takes in samples and writes them out [offset..offset + n) in Float32 for the context: delayed by LATENCY. */
  run(left: Float64Array, right: Float64Array, n: number, outL: Float32Array, outR: Float32Array, offset: number): void {
    const { lineL, lineR, required, requiredAt, smooth, tapsL, tapsR } = this;
    for (let i = 0; i < n; i++) {
      const l = left[i];
      const r = right[i];
      const at = this.tapAt === 0 ? TP_TAPS - 1 : this.tapAt - 1;
      tapsL[at] = tapsL[at + TP_TAPS] = l;
      tapsR[at] = tapsR[at + TP_TAPS] = r;
      this.tapAt = at;
      const peak = this.peak();
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
    this.tapsL.fill(0);
    this.tapsR.fill(0);
  }
}
