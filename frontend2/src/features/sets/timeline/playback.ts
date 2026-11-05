/**
 * Plays the set on one live engine, from anywhere. The engine plays one plan on two decks, so each transition takes its
 * turn: a plan is stretched over its first track's whole part of the set (`extendPlan`, as now-playing's chain), and once
 * its outgoing track has gone and the tempo has settled, the next plan takes over with their shared track going on from
 * its sample (`engine.schedule(next, carry)`).
 *
 * The plan's beats are what the engine counts; the timeline counts seconds of the set. `beatAt` and `timeAt` go between
 * them, through the files outside a transition and the master clock inside it. A seek while nothing plays only moves the
 * playhead: the engine and its AudioContext wait for Play, which is the gesture a browser asks for.
 */
import type { MixPlan } from "@/api/mixplan.generated";
import type { EngineState, TrackRequest } from "@/audio/engine";
import type { DeckCarry } from "@/audio/engine/core";
import { DECK_LUFS } from "@/audio/engine/constants";
import { beatAtFile, deckTime } from "@/audio/engine/position";
import { transitionRange } from "@/features/mix/lanes";
import { extendPlan } from "@/features/nowplaying/session";
import { fileAt, type PlacedTrack, type PlacedTransition, setTimeOf, type TimelineLayout } from "./layout";

/** What this needs of `LiveEngine`. */
export interface SetEngine {
  state: EngineState;
  subscribe(listener: (s: EngineState) => void): () => void;
  load(deck: "a" | "b", track: TrackRequest): Promise<void>;
  schedule(plan: MixPlan, carry?: DeckCarry): void;
  play(): Promise<void>;
  pause(): void;
  seek(beat: number): void;
  beat(): number;
  close(): Promise<void>;
}

export type SetPlaybackStatus = "stopped" | "loading" | "playing" | "paused" | "waiting" | "ended" | "error";

const TICK_MS = 100;
const RETRY_MS = 2000;
const TAIL_BEATS = 16;

export interface Segment {
  index: number;
  plan: MixPlan;
  /** Both decks sound between these beats, and these seconds of the set. */
  beats: [number, number];
  times: [number, number];
  a: PlacedTrack;
  b: PlacedTrack;
  /** From this beat on, the next plan can take over: A has gone and the tempo has settled on B's. */
  handOverBeat: number;
}

/**
 * Transition `index` as the engine should play it: from where its first track's stretch of the set begins to where the
 * next transition takes over (or its last track's end). A plan's preview window is only the part around the blend.
 */
export function segmentOf(layout: TimelineLayout, index: number, plan: MixPlan): Segment {
  const t: PlacedTransition = layout.transitions[index];
  const a = layout.tracks[index];
  const b = layout.tracks[index + 1];
  const beats = transitionRange(plan);
  const nextIn = layout.transitions[index + 1]?.aIn;
  const firstFile = a.pieces[0]?.f0 ?? 0; // where the previous plan hands this track over is after this
  const bEnd = beatAtFile(plan.b, nextIn ?? b.pieces[b.pieces.length - 1]?.f1 ?? t.bOut);
  const settled = Math.max(beats[1], plan.tempo.length ? plan.tempo[plan.tempo.length - 1].beat : beats[1]);
  const handOverBeat = nextIn === undefined ? Infinity : Math.max(beats[1], Math.min(settled, bEnd));
  // the last track plays out; otherwise a little further than the next plan needs, so a late one has room
  const to = nextIn === undefined ? Math.max(beats[1], bEnd) : Math.max(bEnd, handOverBeat) + TAIL_BEATS;
  const stretched = extendPlan(plan, firstFile, null);
  const opened: MixPlan = {
    ...stretched,
    window: { start_beat: stretched.window.start_beat, end_beat: to },
    b: { ...stretched.b, play_to_beat: Math.max(stretched.b.play_to_beat, to) },
  };
  return { index, plan: opened, beats, times: [t.t0, t.t1], a, b, handOverBeat };
}

/** What the engine loads for a track: its gain to the level decks play at, as the renderer's and now-playing's. */
function trackRequest(t: PlacedTrack): TrackRequest {
  const lufs = t.view?.integrated_lufs;
  return { hash: t.order.hash, trimDb: lufs === null || lufs === undefined ? 0 : DECK_LUFS - lufs };
}

/** The set's seconds at a beat of this segment. */
export function timeAt(s: Segment, beat: number): number {
  if (beat <= s.beats[0]) return setTimeOf(s.a, deckTime(s.plan.a, beat));
  if (beat >= s.beats[1]) return setTimeOf(s.b, deckTime(s.plan.b, beat));
  return s.times[0] + ((beat - s.beats[0]) / (s.beats[1] - s.beats[0])) * (s.times[1] - s.times[0]);
}

/** This segment's beat at the set's seconds `t`. */
export function beatAt(s: Segment, t: number): number {
  if (t <= s.times[0]) return beatAtFile(s.plan.a, fileAt(s.a, t));
  if (t >= s.times[1]) return beatAtFile(s.plan.b, fileAt(s.b, t));
  return s.beats[0] + ((t - s.times[0]) / (s.times[1] - s.times[0])) * (s.beats[1] - s.beats[0]);
}

/** The transition that plays at `t`: the one it's in, or the first one after it. */
export function segmentIndexAt(layout: TimelineLayout, t: number): number {
  const { transitions } = layout;
  for (const tr of transitions) if (t < tr.t1) return tr.position;
  return Math.max(0, transitions.length - 1);
}

export interface SetPlaybackDeps {
  engine: () => Promise<SetEngine>;
  /** The pair's plan; rejects while it has none. */
  plan: (from: string, to: string) => Promise<MixPlan>;
  /** Called before the engine sounds: whatever else plays stops. */
  beforePlay?: () => void;
}

export class SetPlayback {
  status: SetPlaybackStatus = "stopped";
  error: string | null = null;
  private layout: TimelineLayout = { tracks: [], transitions: [], total: 0 };
  private engine: SetEngine | null = null;
  private current: Segment | null = null;
  private next: Segment | null = null;
  private handing = false;
  private fetchingNext = false;
  private retryAt = 0;
  private head = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private generation = 0;
  private unsubscribe: (() => void) | null = null;
  private readonly listeners = new Set<() => void>();

  constructor(private readonly deps: SetPlaybackDeps) {}

  /** Coarse changes: the status. The playhead is `time()`, read per frame. */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private set(status: SetPlaybackStatus, error: string | null = null): void {
    if (status === this.status && error === this.error) return;
    this.status = status;
    this.error = error;
    for (const l of this.listeners) l();
  }

  /** The order changed. What played isn't where it was, so the music stops and the playhead stays within the set. */
  setLayout(layout: TimelineLayout, sameOrder = false): void {
    const was = this.layout;
    this.layout = layout;
    if (sameOrder && was.tracks.length === layout.tracks.length) {
      // a plan arrived: the same music, but what follows may sit elsewhere on the set
      const c = this.current;
      const t = c && layout.transitions[c.index];
      if (c && t) this.current = { ...c, a: layout.tracks[c.index], b: layout.tracks[c.index + 1], times: [t.t0, t.t1] };
      this.next = null;
      return;
    }
    this.halt();
    this.head = Math.min(this.head, layout.total);
  }

  private halt(): void {
    this.generation++;
    this.stopTimer();
    if (this.engine && this.current) this.engine.pause();
    this.current = null;
    this.next = null;
    this.fetchingNext = false;
    this.handing = false;
    this.set("stopped");
  }

  /** Where the playhead is, in the set's seconds. */
  time(): number {
    const { engine, current } = this;
    if (!engine || !current || this.status === "stopped" || this.status === "loading") return this.head;
    return timeAt(current, engine.beat());
  }

  get playing(): boolean {
    return this.status === "playing" || this.status === "loading" || this.status === "waiting";
  }

  /** Moves the playhead; what's playing carries on from there. */
  seek(t: number): void {
    const to = Math.max(0, Math.min(this.layout.total, t));
    this.head = to;
    if (this.status === "playing" || this.status === "waiting") {
      void this.play(to);
    } else if (this.current) {
      // paused: it opens where the playhead is now when it plays again
      this.halt();
    } else if (this.status === "ended") {
      this.set("stopped");
    }
  }

  /** Plays from `from`, by default where the playhead is. */
  async play(from: number = this.time()): Promise<void> {
    if (this.layout.transitions.length === 0) return;
    const generation = ++this.generation;
    const start = this.status === "ended" && from >= this.layout.total - 0.01 ? 0 : from;
    this.head = start;
    this.deps.beforePlay?.();
    this.stopTimer();
    this.engine?.pause();
    this.next = null;
    this.fetchingNext = false;
    this.set("loading");
    try {
      const index = segmentIndexAt(this.layout, start);
      const pair = this.layout.transitions[index];
      const plan = await this.deps.plan(pair.from, pair.to);
      const engine = await this.ensureEngine();
      if (generation !== this.generation) return;
      const segment = segmentOf(this.layout, index, plan);
      await this.loadDecks(engine, segment);
      if (generation !== this.generation) return;
      engine.schedule(segment.plan);
      engine.seek(beatAt(segment, start));
      this.current = segment;
      await engine.play();
      if (generation !== this.generation) return;
      this.set("playing");
      this.startTimer();
    } catch (e) {
      if (generation !== this.generation) return;
      this.current = null;
      this.set("error", e instanceof Error ? e.message : String(e));
    }
  }

  pause(): void {
    this.generation++;
    this.head = this.time();
    this.stopTimer();
    this.engine?.pause();
    this.next = null;
    this.fetchingNext = false;
    if (this.current) this.set("paused");
    else this.set("stopped");
  }

  toggle(): void {
    if (this.playing) this.pause();
    else void this.play();
  }

  private async ensureEngine(): Promise<SetEngine> {
    if (!this.engine) {
      const engine = await this.deps.engine();
      this.engine = engine;
      this.unsubscribe = engine.subscribe((s) => {
        if (s.status === "ended" && this.status === "playing") {
          this.head = this.layout.total;
          this.stopTimer();
          this.current = null;
          this.set("ended");
        } else if (s.status === "error" && this.status !== "error") {
          this.set("error", s.error);
        }
      });
    }
    return this.engine;
  }

  private startTimer(): void {
    this.stopTimer();
    this.timer = setInterval(() => this.tick(), TICK_MS);
  }

  private stopTimer(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
  }

  /** Public for tests: what a tick of the clock does. */
  tick(now: number = Date.now()): void {
    const { engine, current } = this;
    if (!engine || !current) return;
    const nextIndex = current.index + 1;
    if (nextIndex >= this.layout.transitions.length) return;
    if (!this.next && !this.fetchingNext && now >= this.retryAt) void this.fetchNext(nextIndex, now);
    if (this.handing) return;
    const beat = engine.beat();
    if (beat < current.handOverBeat) return;
    if (this.next) {
      void this.handOver(engine, current, this.next, beat);
    } else if (this.status === "playing" && beat >= current.plan.window.end_beat - 1) {
      // the next plan hasn't come: hold on the last of this one rather than run out of music
      engine.pause();
      this.set("waiting");
    }
  }

  private async loadDecks(engine: SetEngine, s: Segment): Promise<void> {
    await engine.load("a", trackRequest(s.a));
    await engine.load("b", trackRequest(s.b));
  }

  /** The next plan takes over: `current`'s B goes on as its A, on its sample. */
  private async handOver(engine: SetEngine, current: Segment, next: Segment, beat: number): Promise<void> {
    const generation = this.generation;
    const waited = this.status === "waiting";
    this.handing = true;
    this.next = null;
    try {
      await this.loadDecks(engine, next);
      if (generation !== this.generation) return;
      this.current = next;
      if (waited) {
        // nothing's sounding to carry on from: it picks up where the music was held
        engine.schedule(next.plan);
        engine.seek(beatAtFile(next.plan.a, deckTime(current.plan.b, beat)));
        await engine.play();
        if (generation !== this.generation) return;
        this.set("playing");
      } else {
        engine.schedule(next.plan, { from: "b", to: "a" });
      }
    } catch (e) {
      if (generation === this.generation) this.set("error", e instanceof Error ? e.message : String(e));
    } finally {
      if (generation === this.generation) this.handing = false;
    }
  }

  private async fetchNext(index: number, now: number): Promise<void> {
    const generation = this.generation;
    if (!this.engine) return;
    this.fetchingNext = true;
    try {
      const pair = this.layout.transitions[index];
      const plan = await this.deps.plan(pair.from, pair.to);
      if (generation !== this.generation) return;
      this.next = segmentOf(this.layout, index, plan);
    } catch {
      this.retryAt = now + RETRY_MS; // its plan isn't there yet; the job's on it
    } finally {
      if (generation === this.generation) this.fetchingNext = false;
    }
  }

  async close(): Promise<void> {
    this.generation++;
    this.stopTimer();
    this.unsubscribe?.();
    this.unsubscribe = null;
    const engine = this.engine;
    this.engine = null;
    this.current = null;
    await engine?.close();
  }
}
