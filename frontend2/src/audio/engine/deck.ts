/**
 * One deck's whole graph for one plan: sources (four stems or the full track) -> per-stem gain -> gate and trim -> FX ->
 * isolator -> filter -> fader, with echo and reverb sends tapping before the EQ or after the fader. `render` is a function
 * of the master sample, as the renderer's `_deck_stage` is, so a block costs the same anywhere in the plan.
 */
import type { Deck as DeckPlan, Keyframe } from "../../api/mixplan.generated";
import { Clock, Lane } from "./clock";
import { BLOCK, FILTER_SMOOTH_S, FX_FADE_S, pyRound, SPINBACK_SPEED, SR, STEP_RAMP_S, STUTTER_DUTY } from "./constants";
import { ControlLane } from "./control";
import { DjFilter, Echo, isoGain, Isolator, Reverb } from "./dsp";
import { deckBpm, interpExtended, sourceMap } from "./position";
import type { SourceMap } from "./position";
import type { AudioStream } from "./stream";
import { StretchedSource } from "./stretched";
import { giveBackStretcher, takeStretcher } from "./stretcher";
import type { StretchProfile } from "./stretcher";

export interface DeckSource {
  stream: AudioStream;
  /** The stem lane with this source's gain; null for the full track. */
  gain: readonly Keyframe[] | null;
}

interface FxEvent {
  type: "spinback" | "brake" | "stutter";
  startBeat: number;
  div: number;
  ts: number; // relative to the deck's first sample
  te: number;
  i0: number;
  i1: number;
}

const SINC_TAPS = 16;
const SINC_PHASES = 1024;
const SINC_CUT = 0.94;

function besselI0(x: number): number {
  let sum = 1;
  let term = 1;
  for (let k = 1; k < 32; k++) {
    term *= (x / (2 * k)) * (x / (2 * k));
    sum += term;
  }
  return sum;
}

let sincTable: Float32Array | null = null;

/** The varispeed read's Kaiser-windowed sinc: half the renderer's taps, as it runs live. */
function sinc(): Float32Array {
  if (sincTable) return sincTable;
  const table = new Float32Array(SINC_PHASES * SINC_TAPS);
  const half = SINC_TAPS / 2;
  const beta = 8.6;
  for (let p = 0; p < SINC_PHASES; p++) {
    const ph = p / SINC_PHASES;
    let total = 0;
    for (let t = 0; t < SINC_TAPS; t++) {
      const d = t - half + 1 - ph;
      const x = Math.PI * d * SINC_CUT;
      const s = Math.abs(x) < 1e-9 ? 1 : Math.sin(x) / x;
      const w = d / (half + 0.5);
      const win = Math.abs(w) >= 1 ? 0 : besselI0(beta * Math.sqrt(1 - w * w)) / besselI0(beta);
      table[p * SINC_TAPS + t] = s * SINC_CUT * win;
      total += table[p * SINC_TAPS + t];
    }
    for (let t = 0; t < SINC_TAPS; t++) table[p * SINC_TAPS + t] /= total;
  }
  sincTable = table;
  return table;
}

function sameLane(a: readonly Keyframe[], b: readonly Keyframe[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i].beat !== b[i].beat || a[i].value !== b[i].value || a[i].curve !== b[i].curve) return false;
  }
  return true;
}

function sameSends(a: DeckPlan, b: DeckPlan): boolean {
  const x = a.sends;
  const y = b.sends;
  return x.echo_beats === y.echo_beats && x.reverb_size === y.reverb_size;
}

const WIDE = 4096; // what one varispeed block may read: 128 samples at the 15x a 300 / 20 BPM deck could ask for
const SEAM_MAX = 512; // a seam is 5 ms
const SPREAD = 1024; // how far apart the samples an effect reads in one go may be

export class DeckVoice {
  readonly qa: number;
  readonly qb: number;
  /** Plays off its own tempo at some point. */
  readonly stretched: boolean;
  /** Through the stretcher: key lock off its own tempo, or a key shift. Without one or the other it's a copy or varispeed. */
  private stretch: StretchedSource | null = null;
  /** A scrub's bursts are 40 ms long: too short to hear a pitch, or to give a stretcher time to start. */
  private scrubRead = false;
  private fxReading = false;
  private readonly size: number;
  private readonly t0: number;
  private readonly t1: number;
  private readonly fadeIn: number;
  private readonly fadeOut: number;
  private readonly trim: number;
  private readonly map: SourceMap;
  private readonly start: number;
  private readonly seamIn: Float64Array;
  private readonly seamOut: Float64Array;
  private readonly sources: { stream: AudioStream; gain: ControlLane | null }[];
  private readonly gainKeyframes: (readonly Keyframe[] | null)[];
  private readonly filterLane: ControlLane;
  private readonly filterRes: ControlLane | null;
  private readonly allLanes: ControlLane[];
  private readonly events: FxEvent[];
  private readonly stopAt: number;
  private readonly eqOn: boolean;
  private readonly low: ControlLane;
  private readonly mid: ControlLane;
  private readonly high: ControlLane;
  private readonly isolator: Isolator | null;
  private readonly filter: DjFilter | null;
  private readonly volume: ControlLane;
  private echo: Echo | null;
  private readonly echoSend: ControlLane;
  private readonly echoFeedback: ControlLane;
  private reverb: Reverb | null;
  private readonly reverbSend: ControlLane;
  private readonly preTap: boolean;
  private echoStarted = false;
  private reverbStarted = false;
  private warming = false;

  private readonly xL = new Float64Array(BLOCK);
  private readonly xR = new Float64Array(BLOCK);
  private readonly preL = new Float64Array(BLOCK);
  private readonly preR = new Float64Array(BLOCK);
  private readonly tmpL = new Float64Array(BLOCK);
  private readonly tmpR = new Float64Array(BLOCK);
  private readonly gainA = new Float64Array(BLOCK);
  private readonly gainB = new Float64Array(BLOCK);
  private readonly gainC = new Float64Array(BLOCK);
  private readonly feedback = new Float64Array(BLOCK);
  private readonly sendL = new Float64Array(BLOCK);
  private readonly sendR = new Float64Array(BLOCK);
  private readonly fxL = new Float64Array(SPREAD + 2);
  private readonly fxR = new Float64Array(SPREAD + 2);
  private readonly fxPos = new Float64Array(BLOCK);
  private readonly fxGain = new Float64Array(BLOCK);
  private readonly seamL = new Float64Array(SEAM_MAX);
  private readonly seamR = new Float64Array(SEAM_MAX);
  private readonly jq = new Float64Array(BLOCK);
  private readonly wideL = new Float64Array(WIDE);
  private readonly wideR = new Float64Array(WIDE);
  private readonly wideStemL = new Float64Array(WIDE);
  private readonly wideStemR = new Float64Array(WIDE);

  /** null: the deck doesn't sound in the window. */
  static create(
    plan: DeckPlan,
    sources: DeckSource[],
    clock: Clock,
    w0: number,
    w1: number,
    total: number,
    trimDb: number,
    keyLock: boolean,
    profile: StretchProfile,
  ): DeckVoice | null {
    const p0 = Math.max(plan.play_from_beat, w0);
    const p1 = Math.min(plan.play_to_beat, w1);
    return p1 <= p0 ? null : new DeckVoice(plan, sources, clock, w0, w1, total, trimDb, p0, p1, keyLock, profile);
  }

  private constructor(
    private plan: DeckPlan,
    sources: DeckSource[],
    private readonly clock: Clock,
    w0: number,
    w1: number,
    total: number,
    trimDb: number,
    p0: number,
    p1: number,
    keyLock: boolean,
    profile: StretchProfile,
  ) {
    this.map = sourceMap(plan, p0 - 2, p1 + 1);
    this.trim = Math.pow(10, trimDb / 20);
    this.fadeIn = p0 > w0 ? pyRound(0.003 * SR) : 0;
    this.fadeOut = p1 < w1 ? pyRound(0.004 * SR) : 0;
    this.t0 = clock.time(p0) * SR;
    this.t1 = clock.time(p1) * SR;
    this.qa = Math.max(0, Math.floor(this.t0) - this.fadeIn);
    this.qb = Math.min(total, Math.ceil(this.t1) + Math.trunc(0.25 * SR));
    this.size = this.qb - this.qa;

    // the anchor: lined up on the sample where it starts sounding (`_to_master`)
    const anchor = Math.min(Math.max(pyRound(this.t0) - this.qa, 0), this.size - 1);
    this.start = pyRound(this.sourceAt(this.qa + anchor)) - anchor;

    const edge = Math.min(1 / 64, (p1 - p0) / 4);
    let stretched = false;
    const steps = Math.ceil((p1 - p0) * 16);
    for (let i = 0; i <= steps && !stretched; i++) {
      const held = Math.min(p1 - edge, Math.max(p0 + edge, p0 + (i * (p1 - p0)) / steps));
      stretched = Math.abs(clock.bpm(held) / deckBpm(plan, held) - 1) >= 1e-9;
    }
    this.stretched = stretched;

    const ctl = (lane: readonly Keyframe[], smooth = STEP_RAMP_S): ControlLane =>
      new ControlLane(new Lane(lane), clock, this.qa, this.size, smooth);
    this.sources = sources.map((s) => ({ stream: s.stream, gain: s.gain ? ctl(s.gain) : null }));
    this.gainKeyframes = sources.map((s) => s.gain);

    const seam = this.map.seam;
    this.seamIn = new Float64Array(seam);
    this.seamOut = new Float64Array(seam);
    for (let i = 0; i < seam; i++) {
      const f = i / seam + 0.5 / seam;
      this.seamIn[i] = Math.sin(0.5 * Math.PI * f);
      this.seamOut[i] = Math.cos(0.5 * Math.PI * f);
    }

    const { events, stopAt } = this.compileFx();
    this.events = events;
    this.stopAt = stopAt;
    if (plan.key_shift !== 0 || (keyLock && stretched)) {
      // what an effect reads back: a spinback goes as far back again as it lasts, the others less
      const past = events.reduce((most, ev) => Math.max(most, 2 * (ev.te - ev.ts)), 0);
      this.stretch = new StretchedSource(
        takeStretcher(profile),
        { sourceAt: (k) => this.sourceAt(this.qa + k), read: (...args) => this.readMixed(...args) },
        Math.pow(2, plan.key_shift / 12),
        !keyLock,
        past > 0 ? Math.ceil(past) + 2 * BLOCK : 0,
      );
    }

    const lanes = plan.lanes;
    this.low = ctl(lanes.eq_low);
    this.mid = ctl(lanes.eq_mid);
    this.high = ctl(lanes.eq_high);
    this.eqOn = this.low.lane.differsFrom(0) || this.mid.lane.differsFrom(0) || this.high.lane.differsFrom(0);
    this.isolator = this.eqOn ? new Isolator() : null;
    const filterLane = ctl(lanes.filter, FILTER_SMOOTH_S);
    const resOn = lanes.filter_res.some((k) => k.value > 0);
    this.filterLane = filterLane;
    this.filterRes = resOn ? ctl(lanes.filter_res, FILTER_SMOOTH_S) : null;
    this.filter = filterLane.lane.differsFrom(0) ? new DjFilter(filterLane, this.filterRes, this.size) : null;
    this.volume = ctl(lanes.volume);
    this.echoSend = ctl(lanes.echo_send);
    this.echoFeedback = ctl(lanes.echo_feedback);
    this.reverbSend = ctl(lanes.reverb_send);
    const echoOn = lanes.echo_send.some((k) => k.value > 0);
    const reverbOn = lanes.reverb_send.some((k) => k.value > 0);
    this.echo = echoOn ? new Echo(Math.ceil((plan.sends.echo_beats * 60 * SR) / 40)) : null;
    this.reverb = reverbOn ? new Reverb(plan.sends.reverb_size) : null;
    this.allLanes = [this.low, this.mid, this.high, this.volume, this.echoSend, this.echoFeedback, this.reverbSend, filterLane];
    if (this.filterRes) this.allLanes.push(this.filterRes);
    for (const source of this.sources) if (source.gain) this.allLanes.push(source.gain);
    this.preTap =
      (echoOn && plan.sends.echo_tap === "pre_eq") || (reverbOn && plan.sends.reverb_tap === "pre_eq");
  }

  /**
   * A live edit that only moved keyframes, from master sample `q` on: the lanes that changed fade to their new values
   * over EDIT_FADE samples. Filters and tails keep their state. `sources` are in the order this voice was made with.
   */
  retarget(plan: DeckPlan, sources: DeckSource[], q: number): void {
    const at = Math.max(q - this.qa, 0);
    const next = (lane: ControlLane, keyframes: readonly Keyframe[], before: readonly Keyframe[]): void => {
      if (!sameLane(keyframes, before)) lane.retarget(new Lane(keyframes), at);
    };
    const was = this.plan.lanes;
    const now = plan.lanes;
    next(this.low, now.eq_low, was.eq_low);
    next(this.mid, now.eq_mid, was.eq_mid);
    next(this.high, now.eq_high, was.eq_high);
    next(this.volume, now.volume, was.volume);
    next(this.echoSend, now.echo_send, was.echo_send);
    next(this.echoFeedback, now.echo_feedback, was.echo_feedback);
    next(this.reverbSend, now.reverb_send, was.reverb_send);
    next(this.filterLane, now.filter, was.filter);
    if (this.filterRes) next(this.filterRes, now.filter_res, was.filter_res);
    this.filter?.relook();
    sources.forEach((s, i) => {
      const gain = this.sources[i].gain;
      const before = this.gainKeyframes[i];
      if (gain && s.gain && before && !sameLane(s.gain, before)) gain.retarget(new Lane(s.gain), at);
      this.gainKeyframes[i] = s.gain;
    });
    this.plan = plan;
  }

  /** A live edit's fade has played out. */
  settle(): void {
    for (const lane of this.allLanes) lane.settle();
  }

  /** Takes over the old voice's echo and reverb where they would run the same: a tail rings on through a live edit. */
  takeTails(old: DeckVoice): void {
    if (!sameSends(this.plan, old.plan)) return;
    if (this.echo && old.echo) {
      this.echo = old.echo;
      this.echoStarted = old.echoStarted;
      old.echo = null;
    }
    if (this.reverb && old.reverb) {
      this.reverb = old.reverb;
      this.reverbStarted = old.reverbStarted;
      old.reverb = null;
    }
  }

  /** Settles a new voice's filters on the `n` samples before master sample `q`, without feeding its sends. */
  warm(q: number, n: number, left: Float64Array, right: Float64Array): void {
    if (!this.isolator && !this.filter) return;
    this.warming = true;
    for (let at = Math.max(q - n, 0); at < q; at += BLOCK) this.render(at, Math.min(BLOCK, q - at), left, right, left, right);
    this.warming = false;
  }

  /** Gives its stretcher back: this voice has played its last. */
  dispose(): void {
    if (this.stretch) giveBackStretcher(this.stretch.stretcher);
    this.stretch = null;
  }

  /** Whether changing the stretcher's profile would change anything. */
  get usesStretcher(): boolean {
    return this.stretch !== null;
  }

  /** The (fractional) source sample at a master sample: what the varispeed read and the stretcher follow. */
  private sourceAt(q: number): number {
    return interpExtended(this.map.knotBeats, this.map.knotSamples, this.clock.beat(q / SR));
  }

  private compileFx(): { events: FxEvent[]; stopAt: number } {
    const events: FxEvent[] = [];
    let stopAt = Infinity;
    for (const ev of this.plan.fx) {
      const ts = this.clock.time(ev.start_beat) * SR - this.qa;
      const te = this.clock.time(ev.end_beat) * SR - this.qa;
      const i0 = Math.min(this.size, Math.max(0, Math.ceil(ts)));
      const i1 = Math.min(this.size, Math.max(0, Math.ceil(te)));
      const div = ev.type === "stutter" ? ev.div : 0;
      events.push({ type: ev.type, startBeat: ev.start_beat, div, ts, te, i0, i1 });
      if (ev.type !== "stutter") {
        stopAt = i1; // the record has stopped: nothing after it plays
        break;
      }
    }
    return { events, stopAt };
  }

  /** Samples [j, j + n) of the spliced source into left/right from `offset`. */
  private readSpliced(stream: AudioStream, j: number, n: number, left: Float64Array, right: Float64Array, offset: number): void {
    const { pieces, seam, length } = this.map;
    const end = j + n;
    let lo = 0;
    let hi = pieces.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (pieces[mid].from <= j) lo = mid;
      else hi = mid - 1;
    }
    left.fill(0, offset, offset + n);
    right.fill(0, offset, offset + n);
    for (let p = lo; p < pieces.length; p++) {
      const { from, to, file } = pieces[p];
      if (from >= end) break;
      const a = Math.max(from, j, 0);
      const b = Math.min(to, end, length);
      if (a < b) stream.read(file + (a - from), b - a, left, right, offset + a - j);
    }
    const next = lo; // a seam ends where a piece starts: it can reach back into this read from a later piece
    for (let p = next; p < pieces.length; p++) {
      const piece = pieces[p];
      if (piece.from - seam >= end) break;
      if (!piece.seam || piece.from <= j) continue;
      const a = Math.max(piece.from - seam, j);
      const b = Math.min(piece.from, end);
      if (a >= b) continue;
      const count = b - a;
      const t0 = a - (piece.from - seam);
      stream.read(piece.file - seam + t0, count, this.seamL, this.seamR, 0);
      const base = offset + a - j;
      for (let i = 0; i < count; i++) {
        const fo = this.seamOut[t0 + i];
        const fi = this.seamIn[t0 + i];
        left[base + i] = left[base + i] * fo + this.seamL[i] * fi;
        right[base + i] = right[base + i] * fo + this.seamR[i] * fi;
      }
    }
  }

  /**
   * What goes into the stretcher: the sources mixed on the source's own time, each stem under its gain where the sample
   * will be heard, at samples [k0, k1] of the range. It's a stretcher per deck rather than per stem for the budget, as
   * four would cost four times this; the renderer has the time to run one per stem.
   */
  private readMixed(from: number, n: number, k0: number, k1: number, left: Float64Array, right: Float64Array): void {
    const { wideStemL, wideStemR } = this;
    left.fill(0, 0, n);
    right.fill(0, 0, n);
    for (const { stream, gain } of this.sources) {
      const g0 = gain ? gain.at(Math.max(k0, 0)) : 1;
      const g1 = gain ? gain.at(Math.max(k1, 0)) : 1;
      if (g0 === 0 && g1 === 0) continue;
      const slope = (g1 - g0) / n;
      for (let done = 0; done < n; done += WIDE) {
        const count = Math.min(WIDE, n - done);
        this.readSpliced(stream, from + done, count, wideStemL, wideStemR, 0);
        for (let t = 0; t < count; t++) {
          const g = g0 + slope * (done + t);
          left[done + t] += wideStemL[t] * g;
          right[done + t] += wideStemR[t] * g;
        }
      }
    }
  }

  /**
   * Varispeed: the sources mixed on the source's own time, then one resample. A stem's gain moves little in a block, so
   * it goes on before the resampler instead of running 16 taps per stem.
   */
  private readStretched(a: number, m: number, left: Float64Array, right: Float64Array, at: number): void {
    const { jq, wideL, wideR, wideStemL, wideStemR } = this;
    for (let i = 0; i < m; i++) jq[i] = this.sourceAt(this.qa + a + i);
    const half = SINC_TAPS / 2;
    const first = Math.floor(jq[0]) - half + 1;
    const span = Math.min(WIDE, Math.floor(jq[m - 1]) + half + 1 - first + 1);
    wideL.fill(0, 0, span);
    wideR.fill(0, 0, span);
    for (const { stream, gain } of this.sources) {
      const g0 = gain ? gain.at(a) : 1;
      const g1 = gain ? gain.at(a + m - 1) : 1;
      if (g0 === 0 && g1 === 0) continue;
      this.readSpliced(stream, first, span, wideStemL, wideStemR, 0);
      const slope = (g1 - g0) / span;
      for (let t = 0; t < span; t++) {
        const g = g0 + slope * t;
        wideL[t] += wideStemL[t] * g;
        wideR[t] += wideStemR[t] * g;
      }
    }
    const table = sinc();
    for (let i = 0; i < m; i++) {
      const whole = Math.floor(jq[i]);
      const phase = Math.min(SINC_PHASES - 1, Math.round((jq[i] - whole) * SINC_PHASES));
      const base = whole - half + 1 - first;
      const row = phase * SINC_TAPS;
      let l = 0;
      let r = 0;
      if (base >= 0 && base + SINC_TAPS <= span) {
        for (let t = 0; t < SINC_TAPS; t++) {
          const c = table[row + t];
          l += wideL[base + t] * c;
          r += wideR[base + t] * c;
        }
      }
      left[at + i] = l;
      right[at + i] = r;
    }
  }

  /** The deck's dry signal x (before its effects) at samples [k, k + n) of its range; silence outside it. */
  private computeX(k: number, n: number, left: Float64Array, right: Float64Array, offset: number): void {
    left.fill(0, offset, offset + n);
    right.fill(0, offset, offset + n);
    const a = Math.max(k, 0);
    const b = Math.min(k + n, this.size);
    if (a >= b) return;
    const m = Math.min(b - a, BLOCK);
    if (b - a > BLOCK) {
      this.computeX(a + BLOCK, b - a - BLOCK, left, right, offset + (a - k) + BLOCK);
    }
    const at = offset + (a - k);
    const { tmpL, tmpR, gainA } = this;
    if (this.stretch && !this.scrubRead) {
      if (this.fxReading) this.stretch.past(a, m, left, right, at);
      else this.stretch.read(a, m, left, right, at);
    } else if (this.stretched) {
      this.readStretched(a, m, left, right, at);
    } else {
      for (const { stream, gain } of this.sources) {
        this.readSpliced(stream, a + this.start, m, tmpL, tmpR, 0);
        if (gain) {
          const flat = gain.fill(a, m, gainA);
          if (flat && gainA[0] === 0) continue;
          for (let i = 0; i < m; i++) {
            left[at + i] += tmpL[i] * gainA[i];
            right[at + i] += tmpR[i] * gainA[i];
          }
        } else {
          for (let i = 0; i < m; i++) {
            left[at + i] += tmpL[i];
            right[at + i] += tmpR[i];
          }
        }
      }
    }
    // the gate: the deck sounds on [t0, t1], faded in and out on sin^2 where the window doesn't cut it anyway
    const { t0, t1, fadeIn, fadeOut, trim, qa } = this;
    const q0 = qa + a;
    const open = (fadeIn === 0 || q0 >= t0) && q0 + m <= t1 - Math.max(fadeOut, 1);
    if (open) {
      if (trim !== 1) {
        for (let i = 0; i < m; i++) {
          left[at + i] *= trim;
          right[at + i] *= trim;
        }
      }
      return;
    }
    const outFade = Math.max(fadeOut, 1);
    for (let i = 0; i < m; i++) {
      const q = q0 + i;
      let g = (t1 - q) / outFade;
      if (fadeIn) {
        const up = (q - (t0 - fadeIn)) / fadeIn;
        if (up < g) g = up;
      }
      g = g <= 0 ? 0 : g >= 1 ? 1 : g;
      const s = Math.sin(0.5 * Math.PI * g);
      const gain = s * s * trim;
      left[at + i] *= gain;
      right[at + i] *= gain;
    }
  }

  /** x read at the fractional positions `fxPos[from..to)` (`_read`: linear interpolation), times `fxGain`, into out. */
  private readAt(from: number, to: number, left: Float64Array, right: Float64Array): void {
    const { fxPos, fxGain, fxL, fxR } = this;
    let i = from;
    while (i < to) {
      let lo = Math.floor(fxPos[i]);
      let hi = lo;
      let e = i + 1;
      for (; e < to; e++) {
        const f = Math.floor(fxPos[e]);
        const nlo = Math.min(lo, f);
        const nhi = Math.max(hi, f);
        if (nhi - nlo >= SPREAD) break;
        lo = nlo;
        hi = nhi;
      }
      const width = hi - lo + 2;
      this.fxReading = true;
      this.computeX(lo, width, fxL, fxR, 0);
      this.fxReading = false;
      for (let s = i; s < e; s++) {
        const whole = Math.floor(fxPos[s]);
        const fr = fxPos[s] - whole;
        const o = whole - lo;
        left[s] = (fxL[o] * (1 - fr) + fxL[o + 1] * fr) * fxGain[s];
        right[s] = (fxR[o] * (1 - fr) + fxR[o + 1] * fr) * fxGain[s];
      }
      i = e;
    }
  }

  private applyFx(k: number, n: number, left: Float64Array, right: Float64Array): void {
    const { fxPos, fxGain, clock, qa } = this;
    const fade = FX_FADE_S * SR;
    for (const ev of this.events) {
      const a = Math.max(ev.i0, k);
      const b = Math.min(ev.i1, k + n);
      if (a < b) {
        const from = a - k;
        const to = b - k;
        const span = Math.max(ev.te - ev.ts, 1);
        if (ev.type === "spinback") {
          for (let i = from; i < to; i++) {
            const u = (k + i - ev.ts) / span;
            const left3 = 1 - u;
            fxPos[i] = ev.ts - ((SPINBACK_SPEED * (ev.te - ev.ts)) / 3) * (1 - left3 * left3 * left3);
            fxGain[i] = Math.pow(left3, 1.5);
          }
        } else if (ev.type === "brake") {
          for (let i = from; i < to; i++) {
            const u = (k + i - ev.ts) / span;
            fxPos[i] = ev.ts + (ev.te - ev.ts) * (u - (u * u) / 2);
            fxGain[i] = Math.sqrt(1 - u);
          }
        } else {
          for (let i = from; i < to; i++) {
            const at = k + i;
            const beat = clock.beat((qa + at) / SR);
            const r0 = ev.startBeat + Math.floor((beat - ev.startBeat) / ev.div + 1e-9) * ev.div;
            const tr = clock.time(r0) * SR - qa;
            const off = at - tr;
            const length = clock.time(r0 + ev.div) * SR - qa - tr;
            const up = off / fade;
            const down = (STUTTER_DUTY * length - off) / fade;
            fxPos[i] = ev.ts + off;
            fxGain[i] = (up <= 0 ? 0 : up >= 1 ? 1 : up) * (down <= 0 ? 0 : down >= 1 ? 1 : down);
          }
        }
        this.readAt(from, to, left, right);
      }
      if (ev.type === "stutter") {
        // back in where the record would have been, after a short fade
        const count = Math.min(this.size, ev.i1 + Math.trunc(fade)) - ev.i1;
        const c = Math.max(ev.i1, k);
        const d = Math.min(ev.i1 + count, k + n);
        for (let at = c; at < d; at++) {
          const g = count > 1 ? (at - ev.i1) / (count - 1) : 0;
          left[at - k] *= g;
          right[at - k] *= g;
        }
      }
    }
    if (this.stopAt < k + n) {
      const from = Math.max(this.stopAt - k, 0);
      left.fill(0, from, n);
      right.fill(0, from, n);
    }
  }

  /** Adds this deck's master samples [q, q + n) (n <= BLOCK) to the dry and the effects bus. */
  render(q: number, n: number, dryL: Float64Array, dryR: Float64Array, fxL: Float64Array, fxR: Float64Array): void {
    const { xL, xR, preL, preR, gainA, gainB, gainC } = this;
    const k = q - this.qa;
    const inside = k < this.size && k + n > 0;
    const a = Math.max(k, 0);
    const m = inside ? Math.min(k + n, this.size) - a : 0;
    const at = a - k;
    if (m > 0) {
      this.computeX(a, m, xL, xR, 0);
      if (this.events.length) this.applyFx(a, m, xL, xR);
      if (this.preTap) {
        preL.set(xL.subarray(0, m));
        preR.set(xR.subarray(0, m));
      }
      if (this.isolator) {
        this.eqGains(this.low, a, m, gainA);
        this.eqGains(this.mid, a, m, gainB);
        this.eqGains(this.high, a, m, gainC);
        this.isolator.run(xL, xR, m, gainA, gainB, gainC);
      }
      if (this.filter) this.filter.run(xL, xR, a, m);
      this.volume.fill(a, m, gainA);
      for (let i = 0; i < m; i++) {
        const v = gainA[i];
        xL[i] *= v;
        xR[i] *= v;
        dryL[at + i] += xL[i];
        dryR[at + i] += xR[i];
      }
    }
    if (q + n <= this.qa || this.warming) return;
    if (this.echo) this.runEcho(q, k, n, m, at, fxL, fxR);
    if (this.reverb) this.runReverb(k, n, m, at, fxL, fxR);
  }

  private eqGains(lane: ControlLane, k: number, m: number, out: Float64Array): void {
    if (lane.fill(k, m, out)) {
      out.fill(isoGain(out[0]), 0, m);
    } else {
      for (let i = 0; i < m; i++) out[i] = isoGain(out[i]);
    }
  }

  private fillSend(send: ControlLane, tapPre: boolean, k: number, m: number, at: number, n: number): boolean {
    const { sendL, sendR, gainA, xL, xR, preL, preR } = this;
    sendL.fill(0, 0, n);
    sendR.fill(0, 0, n);
    if (m <= 0) return false;
    send.fill(k, m, gainA);
    let peak = 0;
    const l = tapPre ? preL : xL;
    const r = tapPre ? preR : xR;
    for (let i = 0; i < m; i++) {
      const g = gainA[i];
      if (g > peak) peak = g;
      sendL[at + i] = l[i] * g;
      sendR[at + i] = r[i] * g;
    }
    return peak > 1e-4;
  }

  private runEcho(q: number, k: number, n: number, m: number, at: number, fxL: Float64Array, fxR: Float64Array): void {
    if (this.fillSend(this.echoSend, this.plan.sends.echo_tap === "pre_eq", Math.max(k, 0), m, at, n)) this.echoStarted = true;
    if (!this.echoStarted) return;
    // repeats keep the deck's tempo once it stops: a tempo jump on the cut would make the delay line jump
    this.echoFeedback.fill(Math.max(k, 0), n, this.feedback);
    this.echo!.run(this.sendL, this.sendR, this.feedback, this.echoDelay(q), this.echoDelay(q + n), n, fxL, fxR);
  }

  private echoDelay(q: number): number {
    const stop = this.plan.play_to_beat - 1 / 64;
    return (this.plan.sends.echo_beats * 60 * SR) / this.clock.bpm(Math.min(this.clock.beat(q / SR), stop));
  }

  private runReverb(k: number, n: number, m: number, at: number, fxL: Float64Array, fxR: Float64Array): void {
    if (this.fillSend(this.reverbSend, this.plan.sends.reverb_tap === "pre_eq", Math.max(k, 0), m, at, n)) this.reverbStarted = true;
    if (this.reverbStarted) this.reverb!.run(this.sendL, this.sendR, n, fxL, fxR);
  }

  /** The playhead jumped: the running sums and filter settle again. `clear` also silences the effect tails. */
  relocate(clear: boolean, scrubbing = false): void {
    this.stretch?.relocate();
    this.scrubRead = scrubbing;
    for (const lane of this.allLanes) lane.relocate();
    this.filter?.relocate();
    this.isolator?.reset();
    if (clear) {
      this.echo?.clear();
      this.reverb?.clear();
      this.echoStarted = this.reverbStarted = false;
    }
  }
}
