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
import { EDIT_FADE } from "./control";
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
const WARM = 256; // samples a new graph runs unheard, to settle its filters, before a live edit fades to it
const SCRUB_GRAIN = Math.trunc(0.04 * SR); // a scrub is short bursts of the plan from where the pointer is
const SCRUB_FADE = Math.trunc(0.003 * SR);

/** What a plan's voices are built from: when only the lanes' keyframes differ, the running voices can take the edit. */
function structureOf(plan: MixPlan, audio: Record<DeckId, DeckAudio>): string {
  const on = (keyframes: readonly { value: number }[], off: number): boolean => keyframes.some((k) => k.value !== off);
  const up = (keyframes: readonly { value: number }[]): boolean => keyframes.some((k) => k.value > 0);
  const deck = (id: DeckId) => {
    const d = plan[id];
    const l = d.lanes;
    return [
      d.grid, d.key_shift, d.play_from_beat, d.play_to_beat, d.sends, d.loops, d.fx, audio[id],
      on(l.eq_low, 0) || on(l.eq_mid, 0) || on(l.eq_high, 0),
      on(l.filter, 0), up(l.filter_res), up(l.echo_send), up(l.reverb_send),
    ];
  };
  return JSON.stringify([plan.window, plan.tempo, deck("a"), deck("b")]);
}

type Transport = "stopped" | "playing" | "draining";

export class EngineCore {
  private readonly streams = new Map<string, AudioStream>();
  private clock: Clock | null = null;
  private decks: DeckVoice[] = [];
  private voices: Record<DeckId, DeckVoice | null> = { a: null, b: null };
  private structure = "";
  private total = 0;
  private readonly dryL = new Float64Array(BLOCK);
  private readonly dryR = new Float64Array(BLOCK);
  private readonly fxL = new Float64Array(BLOCK);
  private readonly fxR = new Float64Array(BLOCK);
  private readonly levelGain = new Float64Array(BLOCK);
  private readonly oldL = new Float64Array(BLOCK);
  private readonly oldR = new Float64Array(BLOCK);
  private readonly oldFxL = new Float64Array(BLOCK);
  private readonly oldFxR = new Float64Array(BLOCK);
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
  private loopBeats: [number, number] | null = null;
  private loopedOnce = false;
  private editLeft = 0; // samples left of a live edit's fade
  private oldDecks: DeckVoice[] = []; // the graph a structural edit fades from, at its own position
  private oldQ = 0;
  private queued: { plan: MixPlan; audio: Record<DeckId, DeckAudio> } | null = null;
  private scrubbing = false;
  private scrubQ = 0;
  private scrubFresh = false;
  private scrubResume = false;
  private scrubEnding = false;
  private scrubResting = false;
  private grainLeft = 0;
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

  /**
   * Runs the plan from now on, and it can come while playing: drag a keyframe and it's heard on the next buffer, without
   * a click. An edit that only moves lanes' keyframes goes to the running voices; any other builds a new graph and fades
   * to it. Either way it takes EDIT_FADE samples; a plan that comes during that waits, and only the newest is kept. The
   * position carries over in beats, and so does a loop region.
   */
  setPlan(plan: MixPlan, audio: Record<DeckId, DeckAudio>): void {
    if (this.editLeft > 0) {
      this.queued = { plan, audio };
      return;
    }
    const structure = structureOf(plan, audio);
    const live = this.transport === "playing" && this.clock !== null && !this.scrubResting;
    if (live && structure === this.structure) {
      this.retarget(plan, audio);
      return;
    }
    this.install(plan, audio, structure, live);
  }

  private sourcesOf(plan: MixPlan, audio: Record<DeckId, DeckAudio>, id: DeckId): DeckSource[] {
    const deck = plan[id];
    return audio[id].sources.map(({ key, stem }) => {
      const stream = this.streams.get(key);
      if (!stream) throw new Error(`no stream ${key}`);
      return { stream, gain: stem && deck.stem_lanes ? deck.stem_lanes[stem] : null };
    });
  }

  private retarget(plan: MixPlan, audio: Record<DeckId, DeckAudio>): void {
    for (const id of ["a", "b"] as const) this.voices[id]?.retarget(plan[id], this.sourcesOf(plan, audio, id), this.q);
    this.editLeft = EDIT_FADE;
  }

  private install(plan: MixPlan, audio: Record<DeckId, DeckAudio>, structure: string, fade: boolean): void {
    const beat = this.clock ? this.beatAt(this.q) : plan.window.start_beat;
    const seekBeat = this.pendingSeek !== null ? this.beatAt(this.pendingSeek) : null;
    const scrubBeat = this.scrubbing ? this.beatAt(this.scrubQ) : null;
    const w0 = plan.window.start_beat;
    const w1 = plan.window.end_beat;
    const lo = Math.min(w0, Math.max(plan.a.play_from_beat, w0) - 3, Math.max(plan.b.play_from_beat, w0) - 3);
    const clock = new Clock(plan.tempo, lo, w1 + 2, w0);
    const total = pyRound(clock.time(w1) * SR);
    const decks: DeckVoice[] = [];
    const voices: Record<DeckId, DeckVoice | null> = { a: null, b: null };
    const clamp = (b: number): number => Math.max(0, Math.min(total, pyRound(clock.time(Math.max(b, w0)) * SR)));
    const q = clamp(beat);
    for (const id of ["a", "b"] as const) {
      const voice = DeckVoice.create(plan[id], this.sourcesOf(plan, audio, id), clock, w0, w1, total, audio[id].trimDb);
      voices[id] = voice;
      if (voice) decks.push(voice);
    }
    if (fade) {
      for (const id of ["a", "b"] as const) {
        const voice = voices[id];
        const old = this.voices[id];
        if (!voice) continue;
        voice.warm(q, WARM, this.oldL, this.oldR);
        if (old) voice.takeTails(old);
      }
      this.oldDecks = this.decks;
      this.oldQ = this.q;
      this.editLeft = EDIT_FADE;
    }
    this.clock = clock;
    this.decks = decks;
    this.voices = voices;
    this.structure = structure;
    this.total = total;
    this.q = q;
    if (seekBeat !== null) this.pendingSeek = clamp(seekBeat);
    if (scrubBeat !== null) this.scrubQ = clamp(scrubBeat);
    this.placeLoop();
  }

  /** The loop region, in samples of the plan now running. */
  private placeLoop(): void {
    if (!this.clock || !this.loopBeats) {
      this.loopFrom = this.loopTo = -1;
      return;
    }
    this.loopFrom = Math.max(0, pyRound(this.clock.time(this.loopBeats[0]) * SR));
    this.loopTo = Math.min(this.total, pyRound(this.clock.time(this.loopBeats[1]) * SR));
  }

  /** A live edit has played out, or the playhead jumped (under a closed fade): what comes next can start. */
  private endEdit(): void {
    this.editLeft = 0;
    this.oldDecks = [];
    for (const d of this.decks) d.settle();
    const next = this.queued;
    if (next) {
      this.queued = null;
      this.setPlan(next.plan, next.audio);
    }
  }

  private beatAt(q: number): number {
    return this.clock ? this.clock.beat(q / SR) : 0;
  }

  /** The master sample coming out now; while scrubbing, where the pointer is. */
  get position(): number {
    if (this.scrubbing) return this.scrubQ;
    return Math.max(0, this.q - (this.transport === "stopped" ? 0 : LATENCY));
  }

  /** The master beat coming out now. */
  get beat(): number {
    return this.beatAt(this.position);
  }

  get playing(): boolean {
    return this.transport === "playing" && !this.pendingPause && !this.scrubbing;
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
    if (this.scrubbing) {
      this.scrubResume = true;
      return;
    }
    if (this.q >= this.total) this.jump(this.loopFrom >= 0 ? this.loopFrom : 0, true);
    this.startFrame = atFrame;
    this.pendingPause = false;
    this.transport = "playing";
    this.endedFlag = false;
    this.env = this.q === 0 ? 1 : 0; // the window's own fade-in covers sample 0
    this.envStep = this.q === 0 ? 0 : 1 / DECLICK;
  }

  pause(): void {
    if (this.scrubbing) {
      this.scrubResume = false;
      return;
    }
    if (this.transport !== "playing") return;
    this.pendingPause = true;
    this.envStep = -1 / DECLICK;
  }

  /** To a master beat: fades out, jumps, fades back in if it was playing. */
  seek(beat: number): void {
    if (!this.clock) return;
    const q = Math.max(0, Math.min(this.total, pyRound(this.clock.time(beat) * SR)));
    if (this.scrubbing) {
      this.scrubQ = Math.min(q, Math.max(0, this.total - SCRUB_GRAIN));
    } else if (this.transport === "playing") {
      this.pendingSeek = q;
      this.envStep = -1 / DECLICK;
    } else {
      this.jump(q, true);
      this.transport = "stopped";
    }
  }

  /**
   * The pointer is at this master beat: bursts of the plan from there, with short fades, until `endScrub`. A burst plays
   * forward whichever way the pointer goes, and when the pointer rests the sound stops after the burst it's on.
   */
  scrub(beat: number): void {
    if (!this.clock) return;
    const q = Math.max(0, Math.min(Math.max(0, this.total - SCRUB_GRAIN), pyRound(this.clock.time(beat) * SR)));
    if (!this.scrubbing) {
      this.scrubResume = this.playing;
      this.scrubbing = true;
      this.scrubEnding = false;
      this.pendingPause = false;
      this.pendingSeek = null;
      this.startFrame = 0;
      if (this.transport === "playing") {
        this.grainLeft = 0; // what's playing fades out first
      } else {
        // from silence: the first burst starts at once
        this.transport = "playing";
        this.env = 0;
        this.envStep = 0;
        this.scrubResting = true;
      }
    }
    this.scrubQ = q;
    this.scrubFresh = true;
  }

  /** The pointer let go: playing goes on from there, if it was; otherwise it stays there, stopped. */
  endScrub(): void {
    if (this.scrubbing) this.scrubEnding = true;
  }

  /** Between bursts, with the output faded out: the next burst, the end of the scrub, or a rest. */
  private nextGrain(): void {
    if (this.scrubEnding) {
      const q = this.scrubQ;
      this.scrubbing = this.scrubEnding = this.scrubResting = this.scrubFresh = false;
      if (this.scrubResume) {
        this.jump(q, false);
        this.envStep = 1 / DECLICK;
      } else {
        // what's still in the limiter drains first, and pause's way out of that takes LATENCY back off
        this.jump(q + LATENCY, false);
        this.pendingPause = true;
        this.transport = "draining";
        this.drain = LATENCY;
      }
    } else if (this.scrubFresh) {
      this.scrubFresh = this.scrubResting = false;
      this.jump(this.scrubQ, false);
      this.grainLeft = SCRUB_GRAIN;
      this.envStep = 1 / SCRUB_FADE;
    } else {
      this.scrubResting = true;
    }
  }

  /** Keep playing [startBeat, endBeat) of the master timeline; null plays on. The jump is on the sample. */
  setLoop(startBeat: number | null, endBeat: number | null): void {
    if (!this.clock || startBeat === null || endBeat === null || endBeat <= startBeat) {
      this.loopBeats = null;
      this.loopFrom = this.loopTo = -1;
      return;
    }
    this.loopBeats = [startBeat, endBeat];
    this.placeLoop();
    this.loopedOnce = false;
  }

  /** Moves the graph, under a closed fade. The limiter keeps its lookahead: that's the fade, on its way out. */
  private jump(q: number, clearTails: boolean): void {
    this.q = q;
    for (const d of this.decks) d.relocate(clearTails);
    if (this.editLeft > 0) this.endEdit();
  }

  /** The window's fades and the loop region's, on the graph's own position. */
  private edgeGain(q: number): number {
    let g = 1;
    if (q < DECLICK) g = q / (DECLICK - 1);
    const left = this.total - 1 - q;
    if (left < WINDOW_FADE_OUT - 1) g = Math.min(g, left <= 0 ? 0 : left / (WINDOW_FADE_OUT - 1));
    if (this.loopTo >= 0 && !this.scrubbing) {
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
      if (this.scrubbing && this.scrubResting) {
        if (this.scrubFresh || this.scrubEnding) {
          this.nextGrain();
          continue;
        }
        this.dryL.fill(0, 0, m);
        this.dryR.fill(0, 0, m);
        this.limiter.run(this.dryL, this.dryR, m, outL, outR, o);
        o += m;
        continue;
      }
      if (!this.scrubbing && this.q === this.loopTo) {
        this.jump(this.loopFrom, false);
        this.loopedOnce = true;
        continue;
      }
      const target = !this.scrubbing && this.loopTo >= 0 && this.q < this.loopTo ? this.loopTo : this.total;
      m = Math.min(m, target - this.q);
      if (m <= 0 && this.scrubbing) {
        this.env = 0;
        this.envStep = 0;
        this.nextGrain();
        continue;
      }
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
    if (this.oldDecks.length) this.mixOldGraph(m);
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
    if (this.editLeft > 0) {
      this.editLeft -= m;
      if (this.editLeft <= 0) this.endEdit();
    }
    if (this.scrubbing) {
      this.grainLeft -= m;
      if (closed) this.nextGrain();
      else if (this.grainLeft <= 0 && this.envStep === 0) this.envStep = -1 / SCRUB_FADE;
      return;
    }
    if (!closed) return;
    if (this.pendingSeek !== null) {
      const to = this.pendingSeek;
      this.pendingSeek = null;
      this.jump(to, true);
      this.envStep = 1 / DECLICK;
    } else if (this.pendingPause) {
      this.transport = "draining";
      this.drain = LATENCY;
      if (this.editLeft > 0) this.endEdit();
    }
  }

  /** A structural edit's fade: the new graph comes up over the old one. Its sends start empty or with the old tails. */
  private mixOldGraph(m: number): void {
    const { dryL, dryR, fxL, fxR, oldL, oldR, oldFxL, oldFxR } = this;
    oldL.fill(0, 0, m);
    oldR.fill(0, 0, m);
    oldFxL.fill(0, 0, m);
    oldFxR.fill(0, 0, m);
    for (const d of this.oldDecks) if (this.oldQ + m > d.qa) d.render(this.oldQ, m, oldL, oldR, oldFxL, oldFxR);
    this.oldQ += m;
    for (let i = 0; i < m; i++) {
      const w = Math.min(1, (EDIT_FADE - this.editLeft + i + 1) / EDIT_FADE);
      dryL[i] = dryL[i] * w + oldL[i] * (1 - w);
      dryR[i] = dryR[i] * w + oldR[i] * (1 - w);
      fxL[i] += oldFxL[i] * (1 - w);
      fxR[i] += oldFxR[i] * (1 - w);
    }
  }
}
