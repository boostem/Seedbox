/**
 * Full-track 3-band waveform "deck": an overview strip (whole track, sections, viewport) above a zoomable,
 * scrollable main view with the bar/phrase grid, intro/outro brackets, cues and section bands. The playhead follows
 * the global player when it plays this track (drawn on its own canvas from a store subscription — no re-renders).
 *
 * Mouse: click = seek (plays from there) · drag = scroll · ⌘/ctrl + wheel or pinch = zoom · horizontal wheel =
 * scroll. Keys (with `hotkeys`): = / − zoom, 0 fit.
 */
import { Minus, Plus, Scan } from "lucide-react";
import { type PointerEvent as ReactPointerEvent, type ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { TrackAnalysis } from "@/api/types";
import { usePlayer } from "@/audio/player";
import { IconButton } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useHotkey } from "@/lib/hotkeys";
import { barBeat, drawCues, drawEdges, drawGrid, drawSections, drawWave, fmtPrecise, laneOf, setupCanvas, shade, type View, xOf } from "./draw";
import { useTimeView, useWidth } from "./hooks";
import { alpha, sectionColor, wavePalette } from "./palette";
import { prepareWave } from "./prepare";

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
  const wave = useMemo(() => prepareWave(analysis), [analysis]);
  const lane = useMemo(() => laneOf(analysis, wave), [analysis, wave]);
  const duration = analysis.duration_s || wave?.duration || 1;
  const minSpan = Math.max(4, (analysis.bar_s || 2) * 4);
  const tv = useTimeView(0, duration, minSpan);
  const { view: tview } = tv;
  const tvRef = useRef(tv);
  tvRef.current = tv;

  const boxRef = useRef<HTMLDivElement>(null);
  const width = useWidth(boxRef);
  const mainRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLCanvasElement>(null);
  const ovRef = useRef<HTMLCanvasElement>(null);
  const ovHeadRef = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const lastManual = useRef(0);

  const view: View = useMemo(() => ({ start: tview.start, pxPerSec: width / tview.span || 1, width }), [tview, width]);
  const ovView: View = useMemo(() => ({ start: 0, pxPerSec: width / duration || 1, width }), [width, duration]);

  // ---- base layers
  useLayoutEffect(() => {
    const c = mainRef.current;
    if (!c || !width) return;
    const s = setupCanvas(c, width, height);
    if (!s) return;
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
  }, [lane, view, width, height]);

  useLayoutEffect(() => {
    const c = ovRef.current;
    if (!c || !width || !overview) return;
    const s = setupCanvas(c, width, OVERVIEW_H);
    if (!s) return;
    const { ctx, dpr } = s;
    const p = wavePalette();
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
    if (tview.span < duration - 0.01) {
      const x0 = xOf(ovView, tview.start);
      const x1 = xOf(ovView, tview.start + tview.span);
      shade(ctx, ovView, 0, tview.start, 0, OVERVIEW_H, 0.55);
      shade(ctx, ovView, tview.start + tview.span, duration, 0, OVERVIEW_H, 0.55);
      ctx.strokeStyle = alpha(p.fg, 0.55);
      ctx.lineWidth = 1;
      ctx.strokeRect(Math.round(x0) + 0.5, 0.5, Math.max(2, Math.round(x1 - x0) - 1), OVERVIEW_H - 1);
    }
  }, [lane, ovView, width, overview, analysis.sections, tview, duration]);

  // ---- playhead (store subscription, no React renders)
  const drawHead = useCallback(() => {
    const st = usePlayer.getState();
    const on = st.currentId === trackId && st.status !== "idle" && st.status !== "error";
    const pos = st.position;
    const p = wavePalette();
    const hc = headRef.current;
    if (hc && width) {
      const s = setupCanvas(hc, width, height);
      if (s && on) {
        const x = Math.round(xOf(view, pos)) + 0.5;
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
        const x = xOf(ovView, pos);
        s.ctx.fillStyle = alpha(p.bg, 0.5);
        s.ctx.fillRect(0, 0, x, OVERVIEW_H - 4);
        s.ctx.fillStyle = p.playhead;
        s.ctx.fillRect(Math.round(x), 0, 1.5, OVERVIEW_H);
      }
    }
  }, [trackId, view, ovView, width, height, overview]);

  useLayoutEffect(() => drawHead(), [drawHead]);
  useEffect(
    () =>
      usePlayer.subscribe((s, prev) => {
        if (s.currentId !== trackId && prev.currentId !== trackId) return;
        drawHead();
        // follow the playhead while playing, unless the user just scrolled
        if (s.currentId === trackId && s.status === "playing" && performance.now() - lastManual.current > 2500) tvRef.current.reveal(s.position);
      }),
    [drawHead, trackId],
  );

  // ---- interaction
  const drag = useRef<{ x: number; start: number; moved: boolean; id: number } | null>(null);
  const timeAt = (clientX: number) => {
    const r = mainRef.current?.getBoundingClientRect();
    if (!r) return 0;
    return view.start + (clientX - r.left) / view.pxPerSec;
  };
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, start: tview.start, moved: false, id: e.pointerId };
  };
  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    setHover(e.clientX - r.left);
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    if (!d.moved && Math.abs(dx) > 3) d.moved = true;
    if (d.moved) {
      lastManual.current = performance.now();
      tv.set({ start: d.start - dx / view.pxPerSec, span: tview.span });
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
      const tv = tvRef.current;
      const r = el.getBoundingClientRect();
      const t = tv.view.start + (e.clientX - r.left) / (r.width / tv.view.span);
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        lastManual.current = performance.now();
        tv.zoom(Math.exp(-e.deltaY * 0.012), t);
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
        e.preventDefault();
        lastManual.current = performance.now();
        const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        tv.scroll((d / r.width) * tv.view.span);
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const ovDrag = useRef(false);
  const ovTo = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const t = ((e.clientX - r.left) / r.width) * duration;
    lastManual.current = performance.now();
    tv.set({ start: t - tview.span / 2, span: tview.span });
  };

  useHotkey(["=", "Shift+Equal"], () => tv.zoom(1.6), { enabled: !!hotkeys });
  useHotkey("-", () => tv.zoom(1 / 1.6), { enabled: !!hotkeys });
  useHotkey("0", () => tv.fit(), { enabled: !!hotkeys });

  const zoomed = tview.span < duration - 0.01;
  const hoverT = hover == null ? null : view.start + hover / view.pxPerSec;
  const bb = hoverT == null ? null : barBeat(analysis, hoverT);

  return (
    <div className={cn("flex flex-col", className)}>
      <div className="mb-1.5 flex h-6 items-center gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">{toolbar}</div>
        <span className="mr-0.5 text-2xs text-fg-subtle num">{zoomed ? `${Math.max(1, Math.round(tview.span / (analysis.bar_s || 1)))} bars visible` : "Whole track"}</span>
        <IconButton icon={Minus} label="Zoom out (−)" size="sm" onClick={() => tv.zoom(1 / 1.6)} disabled={!zoomed} />
        <IconButton icon={Plus} label="Zoom in (=)" size="sm" onClick={() => tv.zoom(1.6)} disabled={tview.span <= minSpan + 0.01} />
        <IconButton icon={Scan} label="Fit whole track (0)" size="sm" onClick={tv.fit} disabled={!zoomed} />
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
          onPointerLeave={() => setHover(null)}
        >
          <canvas ref={mainRef} className="absolute inset-0 h-full w-full" />
          <canvas ref={headRef} className="pointer-events-none absolute inset-0 h-full w-full" />
          {hover != null && hoverT != null && (
            <>
              <div className="pointer-events-none absolute top-0 bottom-0 w-px bg-white/35" style={{ left: hover }} />
              <div
                className="pointer-events-none absolute bottom-4.5 rounded-xs bg-overlay/95 px-1.5 py-0.5 text-2xs whitespace-nowrap text-fg shadow-pop num"
                style={hover > width - 110 ? { right: width - hover + 6 } : { left: hover + 6 }}
              >
                {fmtPrecise(hoverT)}
                {bb && <span className="ml-1.5 text-fg-subtle">bar {bb}</span>}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
