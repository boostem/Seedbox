/**
 * Full-track 3-band waveform "deck": an overview strip (whole track, sections, viewport) above a zoomable,
 * scrollable main view with the bar/phrase grid, intro/outro brackets, cues and section bands. The playhead follows
 * the global player when it plays this track. Zoom, scroll, hover and the playhead redraw the canvases (and the hover
 * readout) from refs on the next animation frame; React only re-renders when "zoomed in?" flips. See PERFORMANCE.md.
 *
 * Mouse: click = seek (plays from there) · drag = scroll · ⌘/ctrl + wheel or pinch = zoom · horizontal wheel =
 * scroll. Keys (with `hotkeys`): = / − zoom, 0 fit.
 */
import { Minus, Plus, Scan } from "lucide-react";
import { type PointerEvent as ReactPointerEvent, type ReactNode, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { TrackAnalysis } from "@/api/types";
import { player, usePlayer } from "@/audio/player";
import { IconButton } from "@/components/ui";
import { cn } from "@/lib/cn";
import { barBeat, drawCues, drawEdges, drawGrid, drawSections, drawWave, fmtPrecise, laneOf, setupCanvas, shade, type View, xOf } from "./draw";
import { useFrame, useWaveformKeys, useViewCtl, useViewFact, useWidthRef } from "./hooks";
import { alpha, sectionColor, wavePalette } from "./palette";
import { useWave } from "./waveData";

export interface TrackWaveformProps {
  analysis: TrackAnalysis;
  /** Track id: the playhead shows while the global player plays this id. */
  trackId: string;
  /** Click on the main view → seek to file time t (s). */
  onSeek?: (t: number) => void;
  /** Main view height (px). */
  height?: number;
  overview?: boolean;
  /** =/−/0 zoom shortcuts. */
  hotkeys?: boolean;
  /** Left side of the toolbar row above the waveform (zoom controls sit on the right). */
  toolbar?: ReactNode;
  className?: string;
}

const OVERVIEW_H = 30;
const TOP = 14; // bar-number band
const SECTIONS = 15;

export function TrackWaveform({ analysis, trackId, onSeek, height = 132, overview = true, hotkeys, toolbar, className }: TrackWaveformProps) {
  const wave = useWave(analysis);
  const lane = useMemo(() => laneOf(analysis, wave), [analysis, wave]);
  const duration = analysis.duration_s || wave?.duration || 1;
  const minSpan = Math.max(4, (analysis.bar_s || 2) * 4);
  const ctl = useViewCtl(0, duration, minSpan);
  const zoomed = useViewFact(ctl, (v) => v.span < duration - 0.01);
  const atMax = useViewFact(ctl, (v) => v.span <= minSpan + 0.01);

  const boxRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLCanvasElement>(null);
  const ovRef = useRef<HTMLCanvasElement>(null);
  const ovHeadRef = useRef<HTMLCanvasElement>(null);
  const barsRef = useRef<HTMLSpanElement>(null);
  const hoverLineRef = useRef<HTMLDivElement>(null);
  const hoverTipRef = useRef<HTMLDivElement>(null);
  const hoverX = useRef<number | null>(null);
  const lastManual = useRef(0);

  const viewOf = (w: number): View => {
    const v = ctl.get();
    return { start: v.start, pxPerSec: w / v.span || 1, width: w };
  };

  // ---- drawing (from refs; called on the next animation frame, never from a render driven by the view)
  const paintBase = () => {
    const width = widthRef.current;
    const c = mainRef.current;
    if (!c || !width) return;
    const view = viewOf(width);
    const tv = ctl.get();
    const s = setupCanvas(c, width, height);
    if (s) {
      const { ctx, dpr } = s;
      const p = wavePalette();
      const waveH = height - TOP - SECTIONS;
      ctx.fillStyle = p.bg;
      ctx.fillRect(0, 0, width, height);
      const edgeLabels = drawEdges(ctx, lane, view, TOP, waveH, { labels: true });
      drawGrid(ctx, lane, view, TOP, waveH, { labels: true, labelY: 2 });
      drawWave(ctx, lane, view, TOP + 2, waveH - 4, dpr);
      ctx.fillStyle = alpha(p.fg, 0.08);
      ctx.fillRect(0, TOP + waveH / 2, width, 1);
      drawCues(ctx, lane, view, TOP, waveH, { topOffset: 0, avoid: edgeLabels });
      drawSections(ctx, lane, view, height - SECTIONS, SECTIONS);
    }
    const oc = ovRef.current;
    const ovS = oc && overview ? setupCanvas(oc, width, OVERVIEW_H) : null;
    if (ovS) {
      const { ctx, dpr } = ovS;
      const p = wavePalette();
      const ovView: View = { start: 0, pxPerSec: width / duration || 1, width };
      ctx.fillStyle = p.bg;
      ctx.fillRect(0, 0, width, OVERVIEW_H);
      drawWave(ctx, lane, ovView, 1, OVERVIEW_H - 6, dpr);
      for (const sec of analysis.sections ?? []) {
        const x0 = xOf(ovView, sec.start_s);
        const x1 = xOf(ovView, sec.end_s);
        ctx.fillStyle = alpha(sectionColor(sec.kind, p), 0.9);
        ctx.fillRect(x0 + 0.5, OVERVIEW_H - 3, Math.max(1, x1 - x0 - 1), 3);
      }
      // viewport (only when zoomed in)
      if (tv.span < duration - 0.01) {
        const x0 = xOf(ovView, tv.start);
        const x1 = xOf(ovView, tv.start + tv.span);
        shade(ctx, ovView, 0, tv.start, 0, OVERVIEW_H, 0.55);
        shade(ctx, ovView, tv.start + tv.span, duration, 0, OVERVIEW_H, 0.55);
        ctx.strokeStyle = alpha(p.fg, 0.55);
        ctx.lineWidth = 1;
        ctx.strokeRect(Math.round(x0) + 0.5, 0.5, Math.max(2, Math.round(x1 - x0) - 1), OVERVIEW_H - 1);
      }
    }
    if (barsRef.current) {
      barsRef.current.textContent = tv.span < duration - 0.01 ? `${Math.max(1, Math.round(tv.span / (analysis.bar_s || 1)))} bars visible` : "Whole track";
    }
    paintHover();
    paintHead();
  };

  const paintHead = () => {
    const width = widthRef.current;
    const st = usePlayer.getState();
    const on = st.currentId === trackId && st.status !== "idle" && st.status !== "error";
    const pos = on ? player.time() : 0;
    const p = wavePalette();
    const hc = headRef.current;
    if (hc && width) {
      const s = setupCanvas(hc, width, height);
      if (s && on) {
        const x = Math.round(xOf(viewOf(width), pos)) + 0.5;
        if (x >= 0 && x <= width) {
          s.ctx.fillStyle = alpha(p.bg, 0.35);
          s.ctx.fillRect(x - 1.5, 0, 3, height);
          s.ctx.fillStyle = p.playhead;
          s.ctx.fillRect(x - 0.5, 0, 1.5, height);
          s.ctx.beginPath();
          s.ctx.moveTo(x - 5, 0);
          s.ctx.lineTo(x + 5.5, 0);
          s.ctx.lineTo(x + 0.25, 6);
          s.ctx.closePath();
          s.ctx.fill();
        }
      }
    }
    const oc = ovHeadRef.current;
    if (oc && width && overview) {
      const s = setupCanvas(oc, width, OVERVIEW_H);
      if (s && on) {
        const x = (pos / duration) * width;
        s.ctx.fillStyle = alpha(p.bg, 0.5);
        s.ctx.fillRect(0, 0, x, OVERVIEW_H - 4);
        s.ctx.fillStyle = p.playhead;
        s.ctx.fillRect(Math.round(x), 0, 1.5, OVERVIEW_H);
      }
    }
  };

  /** The hover line and its time/bar readout: DOM writes through refs, no state. */
  const paintHover = () => {
    const line = hoverLineRef.current;
    const tip = hoverTipRef.current;
    if (!line || !tip) return;
    const hx = hoverX.current;
    const width = widthRef.current;
    line.hidden = tip.hidden = hx == null;
    if (hx == null) return;
    const t = viewOf(width).start + hx / viewOf(width).pxPerSec;
    const bb = barBeat(analysis, t);
    line.style.left = `${hx}px`;
    if (hx > width - 110) {
      tip.style.left = "";
      tip.style.right = `${width - hx + 6}px`;
    } else {
      tip.style.right = "";
      tip.style.left = `${hx + 6}px`;
    }
    (tip.firstChild as HTMLElement).textContent = fmtPrecise(t);
    (tip.lastChild as HTMLElement).textContent = bb ? `bar ${bb}` : "";
  };

  const frameBase = useFrame(paintBase);
  const frameHead = useFrame(paintHead);
  const frameHover = useFrame(paintHover);
  const widthRef = useWidthRef(boxRef, frameBase);

  // props changed (another analysis, the worker delivered the waveform, a new height): redraw now
  useLayoutEffect(() => paintBase());
  useEffect(() => ctl.subscribe(frameBase), [ctl, frameBase]);

  // ---- playhead: its own rAF loop while this track plays, reading the audio clock (player.time()) every frame
  const headFn = useRef(paintHead);
  headFn.current = paintHead;
  useEffect(() => {
    let raf = 0;
    const loop = () => {
      // follow the playhead while playing, unless the user just scrolled
      if (performance.now() - lastManual.current > 2500) ctl.reveal(player.time());
      headFn.current();
      raf = requestAnimationFrame(loop);
    };
    const sync = () => {
      const st = usePlayer.getState();
      const playing = st.currentId === trackId && st.status === "playing";
      if (playing && !raf) raf = requestAnimationFrame(loop);
      if (!playing) {
        cancelAnimationFrame(raf);
        raf = 0;
        frameHead();
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
  }, [ctl, trackId, frameHead]);

  // ---- interaction
  const drag = useRef<{ x: number; start: number; moved: boolean; id: number } | null>(null);
  const timeAt = (clientX: number) => {
    const r = mainRef.current?.getBoundingClientRect();
    if (!r) return 0;
    const view = viewOf(widthRef.current);
    return view.start + (clientX - r.left) / view.pxPerSec;
  };
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, start: ctl.get().start, moved: false, id: e.pointerId };
  };
  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    hoverX.current = e.clientX - r.left;
    frameHover();
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    if (!d.moved && Math.abs(dx) > 3) d.moved = true;
    if (d.moved) {
      lastManual.current = performance.now();
      const v = ctl.get();
      ctl.set({ start: d.start - dx / (widthRef.current / v.span || 1), span: v.span });
    }
  };
  const onUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved && onSeek) onSeek(Math.max(0, Math.min(duration, timeAt(e.clientX))));
  };

  // wheel: native listener (passive: false) so ⌘/pinch zoom can preventDefault
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const v = ctl.get();
      const r = el.getBoundingClientRect();
      const t = v.start + (e.clientX - r.left) / (r.width / v.span);
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        lastManual.current = performance.now();
        ctl.zoom(Math.exp(-e.deltaY * 0.012), t);
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
        e.preventDefault();
        lastManual.current = performance.now();
        const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        ctl.scroll((d / r.width) * v.span);
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [ctl]);

  const ovDrag = useRef(false);
  const ovTo = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const t = ((e.clientX - r.left) / r.width) * duration;
    lastManual.current = performance.now();
    const span = ctl.get().span;
    ctl.set({ start: t - span / 2, span });
  };

  useWaveformKeys(!!hotkeys, (f) => ctl.zoom(f), () => ctl.fit());

  return (
    <div className={cn("flex flex-col", className)}>
      <div className="mb-1.5 flex h-6 items-center gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">{toolbar}</div>
        <span ref={barsRef} className="mr-0.5 text-2xs text-fg-subtle num" />
        <IconButton icon={Minus} label="Zoom out (−)" size="sm" onClick={() => ctl.zoom(1 / 1.6)} disabled={!zoomed} />
        <IconButton icon={Plus} label="Zoom in (=)" size="sm" onClick={() => ctl.zoom(1.6)} disabled={atMax} />
        <IconButton icon={Scan} label="Fit whole track (0)" size="sm" onClick={ctl.fit} disabled={!zoomed} />
      </div>
      <div ref={boxRef} className="relative">
        {overview && (
          <div className="relative mb-1 overflow-hidden rounded-xs" style={{ height: OVERVIEW_H }}>
            <canvas
              ref={ovRef}
              className="absolute inset-0 h-full w-full cursor-pointer"
              onPointerDown={(e) => {
                e.currentTarget.setPointerCapture(e.pointerId);
                ovDrag.current = true;
                ovTo(e);
              }}
              onPointerMove={(e) => ovDrag.current && ovTo(e)}
              onPointerUp={() => (ovDrag.current = false)}
              aria-label="Track overview — click to move the zoomed view"
            />
            <canvas ref={ovHeadRef} className="pointer-events-none absolute inset-0 h-full w-full" />
          </div>
        )}
        <div
          className="relative cursor-crosshair overflow-hidden rounded-xs"
          style={{ height }}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerLeave={() => {
            hoverX.current = null;
            frameHover();
          }}
        >
          <canvas ref={mainRef} className="absolute inset-0 h-full w-full" />
          <canvas ref={headRef} className="pointer-events-none absolute inset-0 h-full w-full" />
          <div ref={hoverLineRef} hidden className="pointer-events-none absolute top-0 bottom-0 w-px bg-white/35" />
          <div
            ref={hoverTipRef}
            hidden
            className="pointer-events-none absolute bottom-4.5 rounded-xs bg-overlay/95 px-1.5 py-0.5 text-2xs whitespace-nowrap text-fg shadow-pop num"
          >
            <span />
            <span className="ml-1.5 text-fg-subtle empty:hidden" />
          </div>
        </div>
      </div>
    </div>
  );
}
