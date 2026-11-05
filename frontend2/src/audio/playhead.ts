/**
 * The audio clock for playheads, read every animation frame: no store, no React (PERFORMANCE.md).
 *
 * `ClockAnchor` ties a media position to `AudioContext.currentTime`: an HTMLAudioElement's `currentTime` is noisy and
 * coarse, the context's is sample-accurate. `FrameSmoother` then takes out what's left: `currentTime` itself moves in
 * steps of the output buffer (3-40 ms, depending on the system), which against a 60 Hz display shows as a scroll that
 * stutters by several pixels. It predicts from the display's frame times and follows the clock slowly, so the
 * playhead advances evenly and stays within a fraction of a millisecond of the mean of the clock.
 */

/** Further than this from the media position, the anchor starts again (a stall, a seek it wasn't told about). */
const ANCHOR_RESET_S = 0.08;
/** The share of the anchor's error that is corrected per read: clock drift goes, the media position's noise doesn't come. */
const ANCHOR_SLEW = 0.01;

/** A media position (s) carried forward by a context's clock, and slowly pulled to the media position. */
export class ClockAnchor {
  private media = 0;
  private context = NaN;

  /** The position now, from the media position and the context's `currentTime` as read this moment. */
  read(media: number, context: number): number {
    const estimate = this.media + (context - this.context);
    const error = media - estimate;
    if (!(Math.abs(error) < ANCHOR_RESET_S)) {
      this.media = media;
      this.context = context;
      return media;
    }
    this.media += error * ANCHOR_SLEW;
    return estimate + error * ANCHOR_SLEW;
  }

  /** Forget the position: after a seek, a pause or another track. */
  reset(): void {
    this.context = NaN;
  }
}

/** Further than this from the clock, the smoother jumps to it: a seek, or a hidden tab that lost its frames. */
const SMOOTH_SNAP_S = 0.1;
/** The share of the error corrected per frame: a time constant of about 16 frames, so longer than any buffer step. */
const SMOOTH_GAIN = 0.06;

/** Even playhead motion from a clock that moves in steps. `at` takes the clock and the frame's timestamp (ms). */
export class FrameSmoother {
  private value = 0;
  private frame = NaN;

  at(clock: number, frameMs: number, rate = 1): number {
    const predicted = this.value + (rate * (frameMs - this.frame)) / 1000;
    const error = clock - predicted;
    this.value = Math.abs(error) < SMOOTH_SNAP_S ? predicted + error * SMOOTH_GAIN : clock;
    this.frame = frameMs;
    return this.value;
  }

  reset(): void {
    this.frame = NaN;
  }
}

/** Where a playhead's position comes from: the global player, or a deck of the live engine. */
export interface PlayheadSource {
  /** The position now, s of the track; null when this source has nothing loaded. Cheap: read it every frame. */
  time(): number | null;
  playing(): boolean;
  /** Seconds of track per second, when it isn't 1: an off-tempo deck, or 0 for one waiting at its cue. */
  rate?(): number;
  /** Coarse changes only: status, another track, a seek while paused. Never per frame. */
  subscribe(listener: () => void): () => void;
}

/** The live engine as a playhead source; `toTrack` maps its master seconds to the track's (see `mixGeometry`). */
export function engineSource(
  engine: { seconds(): number; state: { status: string }; subscribe(listener: () => void): () => void },
  toTrack: (master: number) => number = (s) => s,
): PlayheadSource {
  return {
    time: () => (engine.state.status === "idle" || engine.state.status === "error" ? null : toTrack(engine.seconds())),
    playing: () => engine.state.status === "playing",
    subscribe: (listener) => engine.subscribe(listener),
  };
}

/** The time at the left edge of a view with `t` on device column `head`. It falls on a whole device column, so a column
 * keeps the same points as it scrolls and the waveform doesn't shimmer. */
export function centredStart(t: number, head: number, pxPerSec: number, dpr: number): number {
  const perColumn = pxPerSec * dpr;
  return perColumn > 0 ? (Math.round(t * perColumn) - head) / perColumn : 0;
}

/** Per-frame samples for #/bench: when `on`, the view that draws records its frame time, clock and playhead. */
export const headTrace = { on: false, frames: [] as number[], clock: [] as number[], head: [] as number[] };

function jitterMs(times: number[], frames: number[]): number {
  // the spread of position against the display's own time, so an even playhead scores 0 at any frame rate
  const d = times.map((t, i) => (t - frames[i] / 1000) * 1000);
  if (d.length < 2) return 0;
  const steps = d.slice(1).map((v, i) => v - d[i]);
  const mean = steps.reduce((a, b) => a + b, 0) / steps.length;
  return Math.sqrt(steps.reduce((a, b) => a + (b - mean) ** 2, 0) / steps.length);
}

/** What the trace shows, in ms: frame-to-frame unevenness of the raw clock and of the playhead drawn from it. */
export function traceJitter(trace: Pick<typeof headTrace, "frames" | "clock" | "head">): { frames: number; clock_jitter_ms: number; head_jitter_ms: number } {
  return { frames: trace.frames.length, clock_jitter_ms: jitterMs(trace.clock, trace.frames), head_jitter_ms: jitterMs(trace.head, trace.frames) };
}
