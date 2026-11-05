/**
 * A waveform strip for library rows, decks and preview clips (pass a clip's 3-band `wave`), on the same renderer as the
 * track view (`drawWave`: the shared WebGL2 context, so a screenful of rows costs one GL context, not one each). The
 * canvas is painted when the data, the size or the mode changes. The playhead and the dimmer unplayed part are an
 * overlay: while this track plays, a rAF loop moves an element and a CSS mask when the playhead reaches another device
 * pixel, with no repaint and no React render. Click or drag to seek.
 */
import { type PointerEvent as ReactPointerEvent, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { TrackAnalysis } from "@/api/types";
import { player, usePlayer } from "@/audio/player";
import { cn } from "@/lib/cn";
import { drawWave, laneOf, setupCanvas, UNPLAYED } from "./draw";
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
  /** click or drag → seek to file time t (s) */
  onSeek?: (t: number) => void;
  className?: string;
}

export function WaveOverview({ analysis, wave: waveProp, trackId, height = 28, sections, onSeek, className }: WaveOverviewProps) {
  const prepared = useWave(waveProp ? null : analysis);
  const wave = waveProp ?? prepared;
  const lane = useMemo(() => laneOf(analysis ?? null, wave), [analysis, wave]);
  const duration = wave?.duration || analysis?.duration_s || 1;
  useWaveLook();

  const strip = sections && analysis?.sections?.length ? 3 : 0;
  const boxRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stripRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const lastPx = useRef(NaN);

  /** The overlay at the playhead now; `force` after a repaint or a resize. Nothing is written within a device pixel. */
  const place = (force = false) => {
    let played: number | null = null;
    if (trackId) {
      const st = usePlayer.getState();
      if (st.currentId === trackId && st.status !== "idle" && st.status !== "error") played = Math.max(0, Math.min(duration, player.time()));
    }
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const px = played == null ? NaN : Math.round((played / duration) * widthRef.current * dpr);
    if (!force && Object.is(px, lastPx.current)) return;
    lastPx.current = px;
    const canvas = canvasRef.current;
    const head = headRef.current;
    if (!canvas || !head) return;
    const x = px / dpr;
    canvas.style.maskImage = played == null ? "" : `linear-gradient(90deg, black ${x}px, rgb(0 0 0 / ${UNPLAYED}) ${x}px)`;
    head.style.display = played == null ? "none" : "";
    if (played != null) head.style.transform = `translateX(${x}px)`;
  };

  const paint = () => {
    const c = canvasRef.current;
    const width = widthRef.current;
    if (!c || !width) return;
    const s = setupCanvas(c, width, height - strip);
    if (!s) return;
    drawWave(s.ctx, lane, { start: 0, pxPerSec: width / duration, width }, 0, height - strip, s.dpr);
    const sc = stripRef.current;
    const st = sc && setupCanvas(sc, width, strip);
    if (st) {
      const p = wavePalette();
      for (const sec of analysis?.sections ?? []) {
        const x0 = (sec.start_s / duration) * width;
        const x1 = (sec.end_s / duration) * width;
        st.ctx.fillStyle = alpha(sectionColor(sec.kind, p), 0.9);
        st.ctx.fillRect(x0 + 0.5, 1, Math.max(1, x1 - x0 - 1), 2);
      }
    }
    place(true);
  };

  const paintFn = useRef(paint);
  paintFn.current = paint;
  const placeFn = useRef(place);
  placeFn.current = place;
  const widthRef = useWidthRef(boxRef, () => paintFn.current());
  useLayoutEffect(() => paint());

  // while this track plays: move the overlay from a rAF loop; a seek while paused moves it once
  useEffect(() => {
    if (!trackId) return;
    let raf = 0;
    const loop = () => {
      placeFn.current();
      raf = requestAnimationFrame(loop);
    };
    const sync = () => {
      const st = usePlayer.getState();
      const playing = st.currentId === trackId && st.status === "playing";
      if (playing && !raf) raf = requestAnimationFrame(loop);
      if (!playing) {
        cancelAnimationFrame(raf);
        raf = 0;
        placeFn.current();
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

  const seek = (e: ReactPointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    onSeek?.(Math.max(0, Math.min(duration, ((e.clientX - r.left) / r.width) * duration)));
  };
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!onSeek || e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    seek(e);
  };
  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (onSeek && e.buttons === 1) seek(e);
  };

  return (
    <div ref={boxRef} className={cn("relative w-full overflow-hidden", onSeek && "cursor-pointer", className)} style={{ height }} onPointerDown={onDown} onPointerMove={onMove}>
      <canvas ref={canvasRef} className="absolute inset-x-0 top-0 w-full" style={{ height: height - strip }} />
      {strip > 0 && <canvas ref={stripRef} className="absolute inset-x-0 bottom-0 w-full" style={{ height: strip }} />}
      <div ref={headRef} className="pointer-events-none absolute inset-y-0 left-0 bg-playhead" style={{ display: "none", width: 1.5 }} />
    </div>
  );
}
