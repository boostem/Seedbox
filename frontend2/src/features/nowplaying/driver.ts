/**
 * Plays a set on the live engine, a transition at a time: `planChain` stretches each plan over its tracks, and shortly before
 * a window ends the next plan takes over, with the surviving track going on from its sample (`engine.schedule(next, carry)`).
 * The view reads the driver, so `demo` can stand in for it. This is how the app plays a set: the export (#70) is a file.
 */
import type { MixPlan } from "@/api/mixplan.generated";
import type { SessionTrack, SetSession, TrackAnalysis } from "@/api/types";
import { LiveEngine, type EngineStatus } from "@/audio/engine";
import { DECK_LUFS } from "@/audio/engine/constants";
import { player } from "@/audio/player";
import type { StemWave } from "@/features/waveform/stems";
import { HANDOFF_BEATS, planChain, playableRun } from "./session";

export interface NowPlayingDriver {
  readonly session: SetSession;
  /** The stretched plans of the set: what `plan` plays, by index. */
  readonly chain: (MixPlan | null)[];
  status(): EngineStatus;
  /** A reason nothing plays. */
  error(): string | null;
  /** The transition playing: tracks[index] into tracks[index + 1]. */
  index(): number;
  /** chain[index] as it plays: a deck without stems has its stem lanes folded into its EQ. */
  plan(): MixPlan | null;
  /** The master beat now, as heard. */
  beat(): number;
  play(): void;
  pause(): void;
  seek(beat: number): void;
  /** Coarse changes: status, another transition. Never per frame. */
  subscribe(listener: () => void): () => void;
  /** What a demo has on hand instead of the server's files. */
  assets?(hash: string): { analysis: TrackAnalysis; stems: StemWave } | null;
  close(): void;
}

const TICK_MS = 200;

function trackRequest(t: SessionTrack) {
  return { hash: t.hash, trimDb: t.track_gain_db ?? (t.integrated_lufs === null ? 0 : DECK_LUFS - t.integrated_lufs) };
}

export class SetDriver implements NowPlayingDriver {
  readonly chain: (MixPlan | null)[];
  private readonly engine = new LiveEngine();
  private readonly listeners = new Set<() => void>();
  private readonly off: () => void;
  private readonly first: number;
  private at: number;
  private failed: string | null = null;
  private prepared: Promise<void> | null = null;
  private handing = false;
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(readonly session: SetSession) {
    this.chain = planChain(session);
    this.first = playableRun(session)?.first ?? -1;
    this.at = this.first;
    this.off = this.engine.subscribe(() => this.emit());
    if (this.first >= 0) this.prepared = this.prepare();
  }

  private emit(): void {
    for (const l of this.listeners) l();
  }

  private async load(index: number): Promise<MixPlan> {
    await this.engine.load("a", trackRequest(this.session.tracks[index]));
    await this.engine.load("b", trackRequest(this.session.tracks[index + 1]));
    return this.chain[index]!;
  }

  private async prepare(): Promise<void> {
    try {
      this.engine.schedule(await this.load(this.at));
      this.emit();
    } catch (e) {
      this.fail(e);
    }
  }

  private fail(e: unknown): void {
    this.failed = e instanceof Error ? e.message : String(e);
    this.emit();
  }

  /** Near the end of a window, the next plan takes over if there is one. */
  private tick(): void {
    const plan = this.chain[this.at];
    const next = this.chain[this.at + 1];
    if (!plan || !next || this.handing || this.engine.state.status !== "playing") return;
    if (this.engine.beat() < plan.window.end_beat - HANDOFF_BEATS) return;
    this.handing = true;
    void this.load(this.at + 1).then(
      (following) => {
        this.engine.schedule(following, { from: "b", to: "a" });
        this.at += 1;
        this.handing = false;
        this.emit();
      },
      (e) => {
        this.handing = false;
        this.fail(e);
      },
    );
  }

  status(): EngineStatus {
    return this.engine.state.status;
  }

  error(): string | null {
    return this.failed ?? this.engine.state.error;
  }

  index(): number {
    return this.at;
  }

  plan(): MixPlan | null {
    return this.engine.playedPlan ?? this.chain[this.at] ?? null;
  }

  beat(): number {
    return this.engine.playedPlan ? this.engine.beat() : (this.chain[this.at]?.window.start_beat ?? 0);
  }

  play(): void {
    void (async () => {
      try {
        player.stop(); // one thing plays at a time: a preview from the library stops here
        if (this.engine.state.status === "ended") {
          this.at = this.first;
          this.prepared = this.prepare();
          await this.prepared;
          this.engine.seek(this.chain[this.at]!.window.start_beat);
        }
        await this.prepared;
        await this.engine.play();
        this.timer ??= setInterval(() => this.tick(), TICK_MS);
      } catch (e) {
        this.fail(e);
      }
    })();
  }

  pause(): void {
    this.engine.pause();
  }

  seek(beat: number): void {
    const plan = this.chain[this.at];
    if (plan) this.engine.seek(Math.max(plan.window.start_beat, Math.min(plan.window.end_beat - 1, beat)));
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  close(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    this.off();
    void this.engine.close();
  }
}
