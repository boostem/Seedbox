/**
 * The engine itself: two decks and a master on one sample clock. Plain TypeScript with no browser in it: the AudioWorklet
 * (`processor.ts`) wraps it, and the parity test and benches run the same code outside a browser.
 *
 * Position is `q`, master samples from the window's start. `q` is what the graph computes now; what's coming out of the
 * limiter is LATENCY samples behind it (`position`). Everything is worked out per sample from `q`, so a keyframe, a loop
 * or an effect lands on the sample the plan says, whatever the block boundaries are.
 *
 * No allocation in `process` after a plan is set: the garbage collector would be the audio thread's worst enemy.
 */
import type { MixPlan, StemLanes } from "../../api/mixplan.generated";
import { Clock } from "./clock";
import { BLOCK, pyRound, SR } from "./constants";
import { DeckVoice } from "./deck";
import type { DeckSource } from "./deck";
import { LATENCY, Leveller, Limiter, MAKEUP } from "./master";
import { AudioStream } from "./stream";

export type DeckId = "a" | "b";

/** A deck's sources for a plan: "mix", or the four stems. `key` names a stream added with `addStream`. */
export interface DeckAudio {
  sources: { key: string; stem: keyof StemLanes | null }[];
  trimDb: number;
}

const DECLICK = 220; // samples: the window's fade-in, and any play, pause or seek
const WINDOW_FADE_OUT = Math.trunc(0.03 * SR);
const LOOP_FADE = 128;

type Transport = "stopped" | "playing" | "draining";

export class EngineCore {
  private readonly streams = new Map<string, AudioStream>();
  private clock: Clock | null = null;
  private decks: DeckVoice[] = [];
  private total = 0;
  private readonly dryL = new Float64Array(BLOCK);
  private readonly dryR = new Float64Array(BLOCK);
  private readonly fxL = new Float64Array(BLOCK);
  private readonly fxR = new Float64Array(BLOCK);
  private readonly levelGain = new Float64Array(BLOCK);
  private readonly leveller = new Leveller();
  private readonly limiter = new Limiter();

  private transport: Transport = "stopped";
  private q = 0;
  private startFrame = 0; // the context frame that plays start at: not before it
  private env = 0; // the declick's gain
  private envStep = 0;
  private pendingSeek: number | null = null;
  private pendingPause = false;
  private drain = 0;
  private loopFrom = -1;
  private loopTo = -1;
  private loopedOnce = false;
  private frame = 0;
  private endedFlag = false;

  /** A source's audio: the same stream stays when a new plan uses the same track. */
  addStream(key: string, frames: number, chunkFrames: number): void {
    if (!this.streams.has(key)) this.streams.set(key, new AudioStream(frames, chunkFrames));
  }

  setChunk(key: string, index: number, left: Float32Array, right: Float32Array): void {
    this.streams.get(key)?.setChunk(index, left, right);
  }

  dropChunk(key: string, index: number): void {
    this.streams.get(key)?.dropChunk(index);
  }

  /** Samples asked for that weren't loaded yet, over all sources. */
  get missing(): number {
    let n = 0;
    for (const s of this.streams.values()) n += s.missing;
    return n;
  }

  /** Runs the plan from now on. A transport position carries over, in beats. */
  setPlan(plan: MixPlan, audio: Record<DeckId, DeckAudio>): void {
    const beat = this.clock ? this.beatAt(this.q) : plan.window.start_beat;
    const w0 = plan.window.start_beat;
    const w1 = plan.window.end_beat;
    const lo = Math.min(w0, Math.max(plan.a.play_from_beat, w0) - 3, Math.max(plan.b.play_from_beat, w0) - 3);
    const clock = new Clock(plan.tempo, lo, w1 + 2, w0);
    const total = pyRound(clock.time(w1) * SR);
    const decks: DeckVoice[] = [];
    for (const id of ["a", "b"] as const) {
      const deck = plan[id];
      const sources: DeckSource[] = [];
      for (const { key, stem } of audio[id].sources) {
        const stream = this.streams.get(key);
        if (!stream) throw new Error(`no stream ${key}`);
        sources.push({ stream, gain: stem && deck.stem_lanes ? deck.stem_lanes[stem] : null });
      }
      const voice = DeckVoice.create(deck, sources, clock, w0, w1, total, audio[id].trimDb);
      if (voice) decks.push(voice);
    }
    this.clock = clock;
    this.decks = decks;
    this.total = total;
    this.q = Math.max(0, Math.min(total, pyRound(clock.time(Math.max(beat, w0)) * SR)));
    this.loopFrom = this.loopTo = -1;
  }

  private beatAt(q: number): number {
    return this.clock ? this.clock.beat(q / SR) : 0;
  }

  /** The master sample coming out now. */
  get position(): number {
    return Math.max(0, this.q - (this.transport === "stopped" ? 0 : LATENCY));
  }

  /** The master beat coming out now. */
  get beat(): number {
    return this.beatAt(this.position);
  }

  get playing(): boolean {
    return this.transport === "playing" && !this.pendingPause;
  }

  get windowFrames(): number {
    return this.total;
  }

  /** True once after the window has played to its end. */
  takeEnded(): boolean {
    const e = this.endedFlag;
    this.endedFlag = false;
    return e;
  }

  /** Starts at context frame `atFrame` (default: right away), sample-accurately. */
  play(atFrame = 0): void {
    if (!this.clock) return;
    if (this.q >= this.total) this.jump(this.loopFrom >= 0 ? this.loopFrom : 0, true);
    this.startFrame = atFrame;
    this.pendingPause = false;
    this.transport = "playing";
    this.endedFlag = false;
    this.env = this.q === 0 ? 1 : 0; // the window's own fade-in covers sample 0
    this.envStep = this.q === 0 ? 0 : 1 / DECLICK;
  }

  pause(): void {
    if (this.transport !== "playing") return;
    this.pendingPause = true;
    this.envStep = -1 / DECLICK;
  }

  /** To a master beat: fades out, jumps, fades back in if it was playing. */
  seek(beat: number): void {
    if (!this.clock) return;
    const q = Math.max(0, Math.min(this.total, pyRound(this.clock.time(beat) * SR)));
    if (this.transport === "playing") {
      this.pendingSeek = q;
      this.envStep = -1 / DECLICK;
    } else {
      this.jump(q, true);
      this.transport = "stopped";
    }
  }

  /** Keep playing [startBeat, endBeat) of the master timeline; null plays on. The jump is on the sample. */
  setLoop(startBeat: number | null, endBeat: number | null): void {
    if (!this.clock || startBeat === null || endBeat === null || endBeat <= startBeat) {
      this.loopFrom = this.loopTo = -1;
      return;
    }
    this.loopFrom = Math.max(0, pyRound(this.clock.time(startBeat) * SR));
    this.loopTo = Math.min(this.total, pyRound(this.clock.time(endBeat) * SR));
    this.loopedOnce = false;
  }

  private jump(q: number, clearTails: boolean): void {
    this.q = q;
    for (const d of this.decks) d.relocate(clearTails);
    if (clearTails) this.limiter.clear();
  }

  /** The window's fades and the loop region's, on the graph's own position. */
  private edgeGain(q: number): number {
    let g = 1;
    if (q < DECLICK) g = q / (DECLICK - 1);
    const left = this.total - 1 - q;
    if (left < WINDOW_FADE_OUT - 1) g = Math.min(g, left <= 0 ? 0 : left / (WINDOW_FADE_OUT - 1));
    if (this.loopTo >= 0) {
      const untilEnd = this.loopTo - q;
      if (untilEnd > 0 && untilEnd <= LOOP_FADE) g = Math.min(g, untilEnd / LOOP_FADE);
      const sinceStart = q - this.loopFrom;
      if (this.loopedOnce && sinceStart >= 0 && sinceStart < LOOP_FADE) g = Math.min(g, sinceStart / LOOP_FADE);
    }
    return g;
  }

  /** The next n samples of output, for context frames [frame, frame + n). */
  process(outL: Float32Array, outR: Float32Array, n: number, frame = this.frame): void {
    this.frame = frame + n;
    let o = 0;
    while (o < n) {
      const here = frame + o;
      if (this.transport === "stopped" || here < this.startFrame) {
        const until = this.transport === "stopped" ? n : Math.min(n, o + (this.startFrame - here));
        outL.fill(0, o, until);
        outR.fill(0, o, until);
        o = until;
        continue;
      }
      let m = Math.min(n - o, BLOCK);
      if (this.transport === "draining") {
        m = Math.min(m, this.drain);
        this.dryL.fill(0, 0, m);
        this.dryR.fill(0, 0, m);
        this.limiter.run(this.dryL, this.dryR, m, outL, outR, o);
        o += m;
        this.drain -= m;
        if (this.drain === 0) {
          this.transport = "stopped";
          if (this.pendingPause) {
            this.q = Math.max(0, this.q - LATENCY);
            this.pendingPause = false;
            for (const d of this.decks) d.relocate(false);
          }
        }
        continue;
      }
      if (this.q === this.loopTo) {
        this.jump(this.loopFrom, false);
        this.loopedOnce = true;
        continue;
      }
      const target = this.loopTo >= 0 && this.q < this.loopTo ? this.loopTo : this.total;
      m = Math.min(m, target - this.q);
      if (m <= 0) {
        this.endedFlag = true;
        this.transport = "draining";
        this.drain = LATENCY;
        continue;
      }
      this.renderBlock(m, outL, outR, o);
      o += m;
    }
  }

  private renderBlock(m: number, outL: Float32Array, outR: Float32Array, offset: number): void {
    const { dryL, dryR, fxL, fxR, levelGain } = this;
    dryL.fill(0, 0, m);
    dryR.fill(0, 0, m);
    fxL.fill(0, 0, m);
    fxR.fill(0, 0, m);
    const q = this.q;
    for (const d of this.decks) if (q + m > d.qa) d.render(q, m, dryL, dryR, fxL, fxR);
    this.leveller.run(dryL, dryR, m, levelGain);
    let env = this.env;
    let step = this.envStep;
    let closed = false;
    for (let i = 0; i < m; i++) {
      const g = env * this.edgeGain(q + i);
      dryL[i] = (dryL[i] * levelGain[i] + fxL[i] * MAKEUP) * g;
      dryR[i] = (dryR[i] * levelGain[i] + fxR[i] * MAKEUP) * g;
      if (step !== 0) {
        env += step;
        if (env >= 1) {
          env = 1;
          step = 0;
        } else if (env <= 0) {
          env = 0;
          step = 0;
          closed = true;
        }
      }
    }
    this.env = env;
    this.envStep = step;
    this.q = q + m;
    this.limiter.run(dryL, dryR, m, outL, outR, offset);
    if (!closed) return;
    if (this.pendingSeek !== null) {
      this.jump(this.pendingSeek, true);
      this.pendingSeek = null;
      this.envStep = 1 / DECLICK;
    } else if (this.pendingPause) {
      this.transport = "draining";
      this.drain = LATENCY;
    }
  }
}
