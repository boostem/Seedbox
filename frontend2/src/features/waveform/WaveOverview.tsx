/**
 * A whole-track waveform strip for library rows and decks, on the same renderer as the track view (`drawWave`: the
 * shared WebGL2 context, so a screenful of rows costs one GL context, not one each). The part played so far is
 * brighter; the canvas repaints only when the playhead moves to another device pixel, from its own rAF loop while
 * this track plays. Click to seek.
 */
import { type PointerEvent as ReactPointerEvent, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { TrackAnalysis } from "@/api/types";
import { player, usePlayer } from "@/audio/player";
import { cn } from "@/lib/cn";
import { drawWave, laneOf, setupCanvas, type View } from "./draw";
import { useWidthRef } from "./hooks";
import { alpha, sectionColor, wavePalette } from "./palette";
import type { WaveData } from "./prepare";
import { useWaveLook } from "./settings";
import { useWave } from "./waveData";

export interface WaveOverviewProps {
  /** the track's analysis (its waveform is prepared in the worker), or `wave` directly (e.g. from `loadWave`) */
  analysis?: TrackAnalysis | null;
  wave?: WaveData | null;
  /** the playhead and the brighter played part show while the global player plays this id */
  trackId?: string;
  height?: number;
  /** a 2 px section strip along the bottom */
  sections?: boolean;
  /** click → seek to file time t (s) */
  onSeek?: (t: number) => void;
  className?: string;
}

export function WaveOverview({ analysis, wave: waveProp, trackId, height = 28, sections, onSeek, className }: WaveOverviewProps) {
  const prepared = useWave(waveProp ? null : analysis);
  const wave = waveProp ?? prepared;
  const lane = useMemo(() => laneOf(analysis ?? null, wave), [analysis, wave]);
  const duration = wave?.duration || analysis?.duration_s || 1;
  useWaveLook();

  const boxRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const lastPx = useRef(NaN);

  const playedAt = (): number | null => {
    if (!trackId) return null;
    const st = usePlayer.getState();
    return st.currentId === trackId && st.status !== "idle" && st.status !== "error" ? player.time() : null;
  };
  const pxOf = (t: number | null) => (t == null ? NaN : Math.round((t / duration) * widthRef.current * Math.min(3, window.devicePixelRatio || 1)));

  const paint = () => {
    const c = canvasRef.current;
    const width = widthRef.current;
    if (!c || !width) return;
    const s = setupCanvas(c, width, height);
    if (!s) return;
    const { ctx, dpr } = s;
    const p = wavePalette();
    const played = playedAt();
    lastPx.current = pxOf(played);
    const view: View = { start: 0, pxPerSec: width / duration, width };
    const strip = sections && analysis?.sections?.length ? 3 : 0;
    drawWave(ctx, lane, view, 0, height - strip, dpr, { played });
    if (strip) {
      for (const sec of analysis!.sections) {
        const x0 = sec.start_s * view.pxPerSec;
        const x1 = sec.end_s * view.pxPerSec;
        ctx.fillStyle = alpha(sectionColor(sec.kind, p), 0.9);
        ctx.fillRect(x0 + 0.5, height - 2, Math.max(1, x1 - x0 - 1), 2);
      }
    }
    if (played != null) {
      ctx.fillStyle = p.playhead;
      ctx.fillRect(Math.round(played * view.pxPerSec), 0, 1.5, height);
    }
  };

  const paintFn = useRef(paint);
  paintFn.current = paint;
  const tickFn = useRef(() => {});
  tickFn.current = () => {
    if (pxOf(playedAt()) !== lastPx.current) paint();
  };
  const widthRef = useWidthRef(boxRef, () => paintFn.current());
  useLayoutEffect(() => paint());

  // while this track plays: repaint when the playhead reaches another device pixel
  useEffect(() => {
    if (!trackId) return;
    let raf = 0;
    const loop = () => {
      tickFn.current();
      raf = requestAnimationFrame(loop);
    };
    const sync = () => {
      const st = usePlayer.getState();
      const playing = st.currentId === trackId && st.status === "playing";
      if (playing && !raf) raf = requestAnimationFrame(loop);
      if (!playing) {
        cancelAnimationFrame(raf);
        raf = 0;
        paintFn.current();
      }
    };
    sync();
    const un = usePlayer.subscribe((s, prev) => {
      if (s.currentId !== trackId && prev.currentId !== trackId) return;
      if (s.status !== prev.status || s.currentId !== prev.currentId || (!raf && s.position !== prev.position)) sync();
    });
    return () => {
      un();
      cancelAnimationFrame(raf);
    };
  }, [trackId]);

  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!onSeek || e.button !== 0) return;
    const r = e.currentTarget.getBoundingClientRect();
    onSeek(Math.max(0, Math.min(duration, ((e.clientX - r.left) / r.width) * duration)));
  };

  return (
    <div ref={boxRef} className={cn("relative w-full", onSeek && "cursor-pointer", className)} style={{ height }} onPointerDown={onDown}>
      <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" />
    </div>
  );
}
