/**
 * The live engine: plays mix plans in the browser, sample-accurately, without a server render.
 *
 *   const engine = new LiveEngine();
 *   await engine.load("a", { hash, trimDb });    // a deck's track; `trimDb` is its `track_gain_db`, as the renderer's
 *   await engine.load("b", { hash: other });
 *   engine.schedule(plan);                       // a plan v3 for those tracks; stem lanes fold into EQ without stems
 *   await engine.play();                         // or play(when), in AudioContext seconds, on the sample
 *   engine.seek(beat); engine.pause(); engine.setLoop(startBeat, endBeat); engine.setLoop(null);
 *   engine.schedule(edited);                     // while playing too: heard in 5 ms, with no click and no restart
 *   engine.scrub(beat); engine.endScrub();       // the pointer's on the waveform, then lets go
 *   engine.beat(); engine.time();                // now, for per-frame drawing: read these, don't re-render per frame
 *   engine.subscribe(listener);                  // coarse state changes: status, what the decks play
 *   engine.schedule(next, { from: "b", to: "a" }); // the next transition while playing: b's track goes on as a, on its sample
 *
 * It runs at 44.1 kHz whatever the device's rate, as the renderer does: the context resamples on the way out. All DSP is
 * in the AudioWorklet (`processor.ts` around `core.ts`), one node for both decks, so they share a clock to the sample.
 * Off its own tempo a deck goes through the stretcher under key lock, and plays varispeed without it; `key_shift` is the stretcher's too.
 */
import { apiUrl } from "@/api/client";
import type { MixPlan, StemLanes } from "../../api/mixplan.generated";
import { Clock } from "./clock";
import { isLowPower, onLowPower } from "./power";
import { pyRound, SR } from "./constants";
import type { DeckAudio, DeckCarry, DeckId } from "./core";
import { foldStemLanes } from "./fold";
import { ChunkGone, ChunkLoader, neededChunks } from "./loader";
import type { PlannedDeck } from "./loader";
import type { EngineMessage, EngineReport } from "./messages";
import { beatAtFile, deckTime } from "./position";
import workletUrl from "./processor?worker&url";

export { MAX_DECODED_BYTES } from "./loader";
export type { DeckId } from "./core";

export const STEM_NAMES = ["drums", "bass", "other", "vocals"] as const;
const PREROLL_S = 1.5;
const PUMP_MS = 500;

export interface TrackRequest {
  /** The library's content hash. */
  hash: string;
  /** Gain that brings the track's body to DECK_LUFS: `track_gain_db` of `GET /api/analysis/{hash}/loudness`, the renderer's trim. */
  trimDb?: number;
}

interface TrackInfo {
  frames: number;
  chunk_frames: number;
  sources: string[];
}

interface LoadedDeck {
  hash: string;
  trimDb: number;
  info: TrackInfo;
  /** The stems were there at `load` and haven't gone since. */
  stems: boolean;
}

export type EngineStatus = "idle" | "buffering" | "playing" | "paused" | "ended" | "error";

export interface EngineState {
  status: EngineStatus;
  /** What each deck is playing: its stems or the full track with EQ; null without a plan. */
  decks: Record<DeckId, "stems" | "full" | null>;
  error: string | null;
}

export interface EngineStats {
  decodedBytes: number;
  peakDecodedBytes: number;
  /** Samples that played as silence because their chunk hadn't arrived. */
  missingSamples: number;
}

function streamKey(hash: string, source: string): string {
  return `${hash}/${source}`;
}

/** A chunk off the server: a FLAC of exactly its samples, decoded in the browser and copied out of its AudioBuffer. */
async function fetchChunk(
  ctx: BaseAudioContext,
  hash: string,
  source: string,
  index: number,
): Promise<{ left: Float32Array; right: Float32Array }> {
  const r = await fetch(apiUrl(`/api/tracks/${hash}/audio/${source}/${index}`));
  if (r.status === 404) throw new ChunkGone(`${hash}/${source}/${index}`);
  if (!r.ok) throw new Error(`chunk ${index} of ${source}: HTTP ${r.status}`);
  const audio = await ctx.decodeAudioData(await r.arrayBuffer());
  if (audio.sampleRate !== SR) throw new Error(`decoded at ${audio.sampleRate} Hz`);
  const left = audio.getChannelData(0).slice();
  const right = (audio.numberOfChannels > 1 ? audio.getChannelData(1) : audio.getChannelData(0)).slice();
  return { left, right };
}

export class LiveEngine {
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private ready: Promise<void> | null = null;
  private readonly tracks: Partial<Record<DeckId, LoadedDeck>> = {};
  private readonly streamsPosted = new Set<string>();
  private plan: MixPlan | null = null;
  private played: MixPlan | null = null;
  private clock: Clock | null = null;
  private timeline = "";
  private planned: PlannedDeck[] = [];
  private total = 0;
  private loop: { from: number; to: number } | null = null;
  private loopBeats: [number, number] | null = null;
  private reportFrame = 0;
  private reportQ = 0;
  private reportMissing = 0;
  private scrubbing = false;
  private scrubPumpedAt = -Infinity;
  private playing = false;
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly listeners = new Set<(s: EngineState) => void>();
  private loader: ChunkLoader | null = null;
  private offLowPower: (() => void) | null = null;
  state: EngineState = { status: "idle", decks: { a: null, b: null }, error: null };

  subscribe(listener: (s: EngineState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private setState(patch: Partial<EngineState>): void {
    this.state = { ...this.state, ...patch };
    for (const l of this.listeners) l(this.state);
  }

  private post(m: EngineMessage, transfer: Transferable[] = []): void {
    this.node?.port.postMessage(m, transfer);
  }

  /** The context and the worklet, on first use. */
  private boot(): Promise<void> {
    if (!this.ready) {
      this.ready = (async () => {
        const ctx = new AudioContext({ sampleRate: SR, latencyHint: "interactive" });
        await ctx.audioWorklet.addModule(workletUrl);
        const node = new AudioWorkletNode(ctx, "seedbox-engine", {
          numberOfInputs: 0,
          numberOfOutputs: 1,
          outputChannelCount: [2],
        });
        node.connect(ctx.destination);
        node.onprocessorerror = () => this.setState({ status: "error", error: "The audio engine stopped with an error." });
        node.port.onmessage = (e: MessageEvent<EngineReport>) => this.report(e.data);
        this.ctx = ctx;
        this.node = node;
        this.post({ type: "lowPower", on: isLowPower() });
        this.offLowPower = onLowPower((on) => this.post({ type: "lowPower", on }));
        this.loader = new ChunkLoader({
          fetchChunk: (key, index) => {
            const cut = key.lastIndexOf("/");
            return fetchChunk(ctx, key.slice(0, cut), key.slice(cut + 1), index);
          },
          post: (key, index, left, right) =>
            this.post({ type: "chunk", key, index, left, right }, [left.buffer, right.buffer]),
          drop: (key, index) => this.post({ type: "drop", key, index }),
          chunkBytes: (key) => {
            for (const d of this.planned) if (d.keys.includes(key)) return d.chunkFrames * 8;
            return 882000 * 8;
          },
          onGone: (key) => this.onSourceGone(key),
        });
      })();
    }
    return this.ready;
  }

  private report(m: EngineReport): void {
    if (m.type !== "position") return;
    this.reportFrame = m.frame;
    this.reportQ = m.q;
    this.reportMissing = m.missing;
    this.playing = m.playing && !this.scrubbing;
    if (m.ended) {
      this.stopPump();
      this.setState({ status: "ended" });
    }
  }

  /** Where the engine's sound comes out: for a meter to tap. Null before the first `load` or `play`. */
  get output(): AudioNode | null {
    return this.node;
  }

  /** Puts a track on a deck. It's playable after the next `schedule`. */
  async load(deck: DeckId, track: TrackRequest): Promise<void> {
    await this.boot();
    const r = await fetch(apiUrl(`/api/tracks/${track.hash}/audio`));
    if (!r.ok) {
      const error = `Couldn't load track ${track.hash}: HTTP ${r.status}`;
      this.setState({ status: "error", error });
      throw new Error(error);
    }
    const info = (await r.json()) as TrackInfo;
    const previous = this.tracks[deck];
    const other = this.tracks[deck === "a" ? "b" : "a"];
    if (previous && previous.hash !== track.hash && other?.hash !== previous.hash) {
      for (const source of previous.info.sources) this.loader?.forget(streamKey(previous.hash, source));
    }
    this.tracks[deck] = {
      hash: track.hash,
      trimDb: track.trimDb ?? 0,
      info,
      stems: STEM_NAMES.every((s) => info.sources.includes(s)),
    };
    for (const source of info.sources) {
      const key = streamKey(track.hash, source);
      if (!this.streamsPosted.has(key)) {
        this.streamsPosted.add(key);
        this.post({ type: "stream", key, frames: info.frames, chunkFrames: info.chunk_frames });
      }
    }
  }

  /** Whether this deck's track has its stems here, so it can play stem lanes. */
  hasStems(deck: DeckId): boolean {
    return !!this.tracks[deck]?.stems;
  }

  /**
   * Runs this plan from now on, with what the decks have now: without stems, its stem lanes fold into EQ. While playing
   * this is a live edit: the position (in beats) and the loop region carry over, and it's heard in 5 ms without a click.
   * With `carry`, the deck `from` of the plan playing goes on as the deck `to` of this one, without a gap: load the same
   * track there first, and keep the new plan's window around where it picks up.
   */
  schedule(plan: MixPlan, carry?: DeckCarry): void {
    for (const id of ["a", "b"] as const) {
      const t = this.tracks[id];
      if (!t || t.hash !== plan[id].track_id) throw new Error(`deck ${id} hasn't loaded track ${plan[id].track_id}`);
    }
    // the new clock's position, until the next report comes from the audio thread
    const old = this.played;
    const at = carry && old && this.clock ? deckTime(old[carry.from], this.clock.beat(this.seconds())) : null;
    this.plan = plan;
    this.apply(carry);
    if (at !== null && this.clock) {
      this.reportQ = pyRound(this.clock.time(beatAtFile(plan[carry!.to], at)) * SR);
      this.reportFrame = this.ctx ? pyRound(this.ctx.currentTime * SR) : this.reportFrame;
    }
  }

  private apply(carry?: DeckCarry): void {
    const plan = this.plan;
    if (!plan) return;
    const hasStems = (trackId: string): boolean =>
      (["a", "b"] as const).some((id) => this.tracks[id]?.hash === trackId && this.tracks[id]?.stems);
    const played = foldStemLanes(plan, hasStems);
    const audio = {} as Record<DeckId, DeckAudio>;
    const planned: PlannedDeck[] = [];
    const decks: EngineState["decks"] = { a: null, b: null };
    for (const id of ["a", "b"] as const) {
      const deck = played[id];
      const t = this.tracks[id]!;
      const onStems = deck.stem_lanes !== null;
      const sources = onStems
        ? STEM_NAMES.map((s) => ({ key: streamKey(t.hash, s), stem: `gain_${s}` as keyof StemLanes }))
        : [{ key: streamKey(t.hash, "mix"), stem: null }];
      audio[id] = { sources, trimDb: t.trimDb };
      decks[id] = onStems ? "stems" : "full";
      planned.push({ plan: deck, keys: sources.map((s) => s.key), frames: t.info.frames, chunkFrames: t.info.chunk_frames });
    }
    const w0 = played.window.start_beat;
    const w1 = played.window.end_beat;
    const lo = Math.min(w0, Math.max(played.a.play_from_beat, w0) - 3, Math.max(played.b.play_from_beat, w0) - 3);
    const timeline = JSON.stringify([played.tempo, lo, w0, w1]);
    if (timeline !== this.timeline) {
      // a drag on a lane comes here 60 times a second and leaves the clock as it is
      const heard = this.clock ? this.beat() : null;
      this.timeline = timeline;
      this.clock = new Clock(played.tempo, lo, w1 + 2, w0);
      this.total = pyRound(this.clock.time(w1) * SR);
      if (heard !== null && this.ctx) {
        // the same beat on the new clock: the playhead shouldn't jump until the next report
        this.reportQ = pyRound(this.clock.time(Math.max(heard, w0)) * SR);
        this.reportFrame = pyRound((this.ctx.currentTime - (this.ctx.outputLatency || 0) - this.ctx.baseLatency) * SR);
      }
    }
    const clock = this.clock!;
    this.played = played;
    this.planned = planned;
    this.post({ type: "plan", plan: played, audio, carry });
    if (this.loopBeats) this.loop = { from: clock.time(this.loopBeats[0]), to: clock.time(this.loopBeats[1]) };
    if (decks.a !== this.state.decks.a || decks.b !== this.state.decks.b) this.setState({ decks });
    this.pump();
  }

  private onSourceGone(key: string): void {
    for (const id of ["a", "b"] as const) {
      const t = this.tracks[id];
      if (t?.stems && key.startsWith(`${t.hash}/`) && !key.endsWith("/mix")) {
        t.stems = false;
        t.info.sources = t.info.sources.filter((s) => s === "mix");
        this.apply();
      }
    }
  }

  private needed() {
    return this.clock ? neededChunks(this.planned, this.clock, this.seconds(), this.total / SR, this.loop) : [];
  }

  private pump(): void {
    this.loader?.update(this.needed());
  }

  private startPump(): void {
    this.stopPump();
    this.timer = setInterval(() => this.pump(), PUMP_MS);
  }

  private stopPump(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
  }

  /** Starts playing; `when` is in AudioContext seconds and lands on its sample. Waits for the first audio. */
  async play(when?: number): Promise<void> {
    await this.boot();
    if (!this.played || !this.ctx) throw new Error("schedule a plan before playing");
    this.setState({ status: "buffering" });
    await this.ctx.resume();
    this.pump();
    const soon = this.needed().filter((n) => n.at <= PREROLL_S);
    await this.loader?.whenLoaded(soon);
    this.post({ type: "play", atFrame: when === undefined ? undefined : pyRound(when * SR) });
    this.playing = true;
    this.reportFrame = pyRound(this.ctx.currentTime * SR);
    this.setState({ status: "playing" });
    this.startPump();
  }

  pause(): void {
    this.post({ type: "pause" });
    this.playing = false;
    this.reportQ = pyRound(this.seconds() * SR);
    this.stopPump();
    if (this.state.status === "playing" || this.state.status === "buffering") this.setState({ status: "paused" });
  }

  /** To a master beat: a fade, the jump, a fade back if it's playing. */
  seek(beat: number): void {
    if (!this.clock) return;
    this.post({ type: "seek", beat });
    this.reportQ = pyRound(this.clock.time(beat) * SR);
    this.pump();
    if (this.state.status === "ended") this.setState({ status: "paused" });
  }

  /**
   * The pointer is at this beat, and you hear it: call it as the pointer moves, then `endScrub`. Playing carries on from
   * where it lets go, if it was playing. Call the first one inside the pointer's event, so the browser allows the sound.
   */
  scrub(beat: number): void {
    if (!this.clock || !this.ctx) return;
    if (!this.scrubbing) {
      this.scrubbing = true;
      void this.ctx.resume();
    }
    this.post({ type: "scrub", beat });
    this.reportQ = pyRound(this.clock.time(beat) * SR);
    this.playing = false;
    // the chunks around the pointer; not on every pixel of a drag
    const at = this.reportQ / SR;
    if (Math.abs(at - this.scrubPumpedAt) > 1) {
      this.scrubPumpedAt = at;
      this.pump();
    }
    if (this.state.status === "ended") this.setState({ status: "paused" });
  }

  endScrub(): void {
    if (!this.scrubbing) return;
    this.scrubbing = false;
    this.scrubPumpedAt = -Infinity;
    this.post({ type: "endScrub" });
    this.pump();
    if (this.state.status === "playing" && this.ctx) {
      this.playing = true;
      this.reportFrame = pyRound(this.ctx.currentTime * SR);
    }
  }

  /** Loop [startBeat, endBeat) of the master timeline; `setLoop(null)` plays on. */
  setLoop(startBeat: number | null, endBeat: number | null = null): void {
    this.post({ type: "loop", startBeat, endBeat });
    this.loopBeats = startBeat !== null && endBeat !== null ? [startBeat, endBeat] : null;
    this.loop =
      this.loopBeats && this.clock ? { from: this.clock.time(this.loopBeats[0]), to: this.clock.time(this.loopBeats[1]) } : null;
    this.pump();
  }

  /** The master chain's loudness target in LUFS (`GET /api/mixes/master`). `makeupDb`: what a render measured, if there is one. */
  setMaster(targetLufs: number, makeupDb?: number | null): void {
    this.post({ type: "master", targetLufs, makeupDb });
  }

  /** The plan as it plays: stem lanes folded into EQ on a deck without stems. */
  get playedPlan(): MixPlan | null {
    return this.played;
  }

  /** Master seconds now, as heard; for playheads in a requestAnimationFrame loop. */
  seconds(): number {
    const ctx = this.ctx;
    if (!ctx || !this.playing) return this.reportQ / SR;
    const since = ctx.currentTime - this.reportFrame / SR - (ctx.outputLatency || 0) - ctx.baseLatency;
    return Math.max(0, Math.min(this.total / SR, this.reportQ / SR + Math.max(0, since)));
  }

  /** The master beat now, as heard. */
  beat(): number {
    return this.clock ? this.clock.beat(this.seconds()) : 0;
  }

  stats(): EngineStats {
    return {
      decodedBytes: this.loader?.usedBytes ?? 0,
      peakDecodedBytes: this.loader?.peakBytes ?? 0,
      missingSamples: this.reportMissing,
    };
  }

  /** Closes the context: the decoded audio goes with it. */
  async close(): Promise<void> {
    this.offLowPower?.();
    this.stopPump();
    await this.ctx?.close();
    this.ctx = null;
    this.node = null;
    this.ready = null;
    this.loader = null;
    this.clock = null;
    this.timeline = "";
    this.streamsPosted.clear();
    for (const id of ["a", "b"] as const) delete this.tracks[id];
  }
}
