/**
 * A track's stems as one stacked waveform or four thin lanes (the Settings choice), for a whole track (overview) or a
 * `start` / `span` window of it (detail). While the clock runs it repaints when the playhead reaches another pixel or a
 * gain moves, through a uniform and without a React render, so a stem visibly dims as the plan plays it down.
 */
import { type PointerEvent as ReactPointerEvent, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { player, usePlayer } from "@/audio/player";
import { cn } from "@/lib/cn";
import { setupCanvas, laneOf, type View } from "./draw";
import { useWidthRef } from "./hooks";
import { wavePalette } from "./palette";
import { useWaveLook, useWaveSettings } from "./settings";
import { drawStems } from "./stemDraw";
import { STEMS, type StemName, type StemWave } from "./stems";
import { useStemWave } from "./waveData";
import { WavePlaceholder } from "./WavePlaceholder";

/** Where a stem view's playhead is, in file seconds. */
export interface StemClock {
  get: () => number | null;
  /** Whether the position is moving now; while it does, the view reads `get()` every frame. */
  running: () => boolean;
  /** Called when the clock starts, stops or jumps. */
  subscribe: (cb: () => void) => () => void;
}

/** The global player as a clock for `trackId`. */
export function playerStemClock(trackId: string): StemClock {
  const mine = () => {
    const s = usePlayer.getState();
    return s.currentId === trackId && s.status !== "idle" && s.status !== "error";
  };
  return {
    get: () => (mine() ? player.time() : null),
    running: () => mine() && usePlayer.getState().status === "playing",
    subscribe: (cb) =>
      usePlayer.subscribe((s, prev) => {
        if (s.currentId !== trackId && prev.currentId !== trackId) return;
        if (s.status !== prev.status || s.currentId !== prev.currentId || s.status !== "playing") cb();
      }),
  };
}

export interface StemWaveformProps {
  /** The track: its stems are fetched, and the player is its clock unless `clock` is given. */
  hash?: string;
  /** Or the stems directly. */
  stems?: StemWave | null;
  height?: number;
  /** The window to draw, file seconds; the whole track by default. */
  start?: number;
  span?: number;
  clock?: StemClock;
  /** The linear gain of each stem (drums, bass, vocals, other) at file time `t`, e.g. `planStemGains`. Read per frame. */
  gainsAt?: (t: number) => ArrayLike<number>;
  /** Stem names on the view. */
  legend?: boolean;
  onSeek?: (t: number) => void;
  className?: string;
}

const STEM_TEXT: Record<StemName, string> = { drums: "text-stem-drums", bass: "text-stem-bass", vocals: "text-stem-vocals", other: "text-stem-other" };
const STEM_DOT: Record<StemName, string> = { drums: "bg-stem-drums", bass: "bg-stem-bass", vocals: "bg-stem-vocals", other: "bg-stem-other" };

export function StemWaveform({ hash, stems: stemsProp, height = 112, ...rest }: StemWaveformProps) {
  const fetched = useStemWave(stemsProp === undefined ? hash : undefined);
  const stems = stemsProp ?? fetched.wave;
  if (stems) return <StemCanvas stems={stems} hash={hash} height={height} {...rest} />;
  const style = { height };
  if (fetched.state === "failed") return <WavePlaceholder state="failed" style={style} className={rest.className} message="Couldn't load this track's stem waveform" onRetry={fetched.retry} />;
  if (fetched.state === "none") return <WavePlaceholder state="queued" style={style} className={rest.className} message="Waiting for this track's stems to be separated" />;
  return <WavePlaceholder state={fetched.state === "analysing" ? "analysing" : "loading"} style={style} className={rest.className} message={fetched.state === "analysing" ? "Measuring the stems" : "Loading the stems"} />;
}

function StemCanvas({ stems, hash, height = 112, start = 0, span, clock: clockProp, gainsAt, legend = true, onSeek, className }: Omit<StemWaveformProps, "stems"> & { stems: StemWave }) {
  const mode = useWaveSettings((s) => s.stemView);
  useWaveLook();
  const clock = useMemo(() => clockProp ?? (hash ? playerStemClock(hash) : null), [clockProp, hash]);
  const lane = useMemo(() => laneOf(null, null), []);
  const visible = span ?? stems.duration;

  const boxRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const lastKey = useRef("");

  const paint = () => {
    const c = canvasRef.current;
    const width = widthRef.current;
    if (!c || !width) return;
    const s = setupCanvas(c, width, height);
    if (!s) return;
    const view: View = { start, pxPerSec: width / visible || 1, width };
    const played = clock?.get() ?? null;
    const gains = gainsAt && played != null ? gainsAt(played) : null;
    drawStems(s.ctx, stems, lane, view, 0, height, s.dpr, { view: mode, gains, played });
    if (played != null) {
      const x = (played - start) * view.pxPerSec;
      if (x >= 0 && x <= width) {
        s.ctx.fillStyle = wavePalette().playhead;
        s.ctx.fillRect(Math.round(x), 0, 1.5, height);
      }
    }
    lastKey.current = keyOf(played, gains, view.pxPerSec);
  };
  const keyOf = (played: number | null, gains: ArrayLike<number> | null, pxPerSec: number) =>
    `${played == null ? "" : Math.round((played - start) * pxPerSec * Math.min(3, window.devicePixelRatio || 1))}:${gains ? Array.from(gains, (g) => Math.round(g * 64)).join() : ""}`;

  const paintFn = useRef(paint);
  paintFn.current = paint;
  const tickFn = useRef(() => {});
  tickFn.current = () => {
    const played = clock?.get() ?? null;
    const key = keyOf(played, gainsAt && played != null ? gainsAt(played) : null, (widthRef.current || 1) / visible);
    if (key !== lastKey.current) paint();
  };
  const widthRef = useWidthRef(boxRef, () => paintFn.current());
  useLayoutEffect(() => paint());

  useEffect(() => {
    if (!clock) return;
    let raf = 0;
    const loop = () => {
      tickFn.current();
      raf = clock.running() ? requestAnimationFrame(loop) : 0;
    };
    const kick = () => {
      if (!raf) raf = requestAnimationFrame(loop);
    };
    const un = clock.subscribe(kick);
    kick();
    return () => {
      un();
      cancelAnimationFrame(raf);
    };
  }, [clock]);

  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!onSeek || e.button !== 0) return;
    const r = e.currentTarget.getBoundingClientRect();
    onSeek(Math.max(0, Math.min(stems.duration, start + ((e.clientX - r.left) / r.width) * visible)));
  };

  return (
    <div className={className}>
      <div ref={boxRef} className={cn("relative w-full", onSeek && "cursor-pointer")} style={{ height }} onPointerDown={onDown}>
        <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" />
        {legend && mode === "lanes" && (
          <div className="pointer-events-none absolute inset-0 grid grid-rows-4">
            {STEMS.map((name) => (
              <span key={name} className={cn("mt-0.5 ml-0.5 self-start justify-self-start rounded-xs bg-bg/80 px-1 text-2xs font-medium capitalize", STEM_TEXT[name])}>
                {name}
              </span>
            ))}
          </div>
        )}
      </div>
      {legend && mode === "stacked" && <StemLegend className="mt-1" />}
    </div>
  );
}

/** Which colour is which stem. */
export function StemLegend({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-3 text-2xs text-fg-subtle", className)}>
      {STEMS.map((name) => (
        <span key={name} className="flex items-center gap-1 capitalize">
          <span className={cn("size-2 rounded-xs", STEM_DOT[name])} />
          {name}
        </span>
      ))}
    </div>
  );
}

