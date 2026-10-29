/**
 * MIX VIEW: deck A above deck B on one time axis, aligned so B's cue (in_start_s) sits under A's out_start_s and
 * B is time-scaled by its pitch change. The overlap is a highlighted column through both decks, with a bar ruler
 * between them (bars counted from where B comes in), vocal-clash bars, the bass-swap bar and B-in / A-out flags.
 * Unheard parts (A after it's out, B before its cue) are dimmed.
 *
 * Playheads: the global player when it plays A or B, plus an optional external clock (the audition's mix time).
 */
import { Minus, Plus, Scan } from "lucide-react";
import { type PointerEvent as ReactPointerEvent, type ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { TrackAnalysis, Transition } from "@/api/types";
import { usePlayer } from "@/audio/player";
import { IconButton } from "@/components/ui";
import { cn } from "@/lib/cn";
import { barBeat, drawCues, drawEdges, drawGrid, drawSections, drawTimeRuler, drawWave, fmtPrecise, setupCanvas, shade, toFile, toMix, type View, xOf } from "./draw";
import { useTimeView, useWaveformKeys, useWidth } from "./hooks";
import { type MixGeometry, mixGeometry } from "./mix";
import { alpha, wavePalette } from "./palette";

/** External playhead source (e.g. the audition): current mix time or null. */
export interface MixClock {
  get: () => number | null;
  subscribe: (cb: () => void) => () => void;
}

export interface MixWaveformProps {
  a: TrackAnalysis | null;
  b: TrackAnalysis | null;
  aId: string;
  bId: string;
  aBpm: number;
  transition: Transition;
  clock?: MixClock;
  /** Click → mix time (e.g. audition from there). */
  onSeek?: (mixTime: number) => void;
  laneHeight?: number;
  toolbar?: ReactNode;
  /** Deck labels drawn over the top-left of each lane. */
  labelA?: ReactNode;
  labelB?: ReactNode;
  hotkeys?: boolean;
  className?: string;
}

const RULE_T = 15; // time ruler (A) on top
const SEC = 13; // section strips
const MID = 32; // mix ruler between decks
const RULE_B = 15; // time ruler (B) at the bottom

export function MixWaveform({ a, b, aId, bId, aBpm, transition, clock, onSeek, laneHeight = 92, toolbar, labelA, labelB, hotkeys, className }: MixWaveformProps) {
  const g = useMemo(() => mixGeometry(transition, a, b, aBpm), [transition, a, b, aBpm]);
  const LH = laneHeight;
  const H = RULE_T + SEC + LH + MID + LH + SEC + RULE_B;
  const yA = RULE_T + SEC; // A wave band top
  const yMid = yA + LH;
  const yB = yMid + MID;

  const barA = g?.barA ?? 2;
  const min = g?.min ?? 0;
  const max = g?.max ?? 1;
  const initial = g ? { start: g.out - 12 * barA, span: g.overlapS + 28 * barA } : undefined;
  const tv = useTimeView(min, max, 4 * barA, initial);
  const tvRef = useRef(tv);
  tvRef.current = tv;
  const tview = tv.view;

  const boxRef = useRef<HTMLDivElement>(null);
  const width = useWidth(boxRef);
  const baseRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const view: View = useMemo(() => ({ start: tview.start, pxPerSec: width / tview.span || 1, width }), [tview, width]);

  useLayoutEffect(() => {
    const c = baseRef.current;
    if (!c || !width || !g) return;
    const s = setupCanvas(c, width, H);
    if (!s) return;
    paint(s.ctx, s.dpr, g, view, transition, { H, LH, yA, yMid, yB });
  }, [g, view, width, H, LH, yA, yMid, yB, transition]);

  // ---- playheads
  const drawHeads = useCallback(() => {
    const c = headRef.current;
    if (!c || !width || !g) return;
    const s = setupCanvas(c, width, H);
    if (!s) return;
    const { ctx } = s;
    const p = wavePalette();
    const line = (m: number, y0: number, y1: number, color: string) => {
      const x = Math.round(xOf(view, m)) + 0.5;
      if (x < 0 || x > width) return;
      ctx.fillStyle = alpha(p.bg, 0.35);
      ctx.fillRect(x - 1.5, y0, 3, y1 - y0);
      ctx.fillStyle = color;
      ctx.fillRect(x - 0.5, y0, 1.5, y1 - y0);
      ctx.beginPath();
      ctx.moveTo(x - 5, y0);
      ctx.lineTo(x + 5.5, y0);
      ctx.lineTo(x + 0.25, y0 + 6);
      ctx.closePath();
      ctx.fill();
    };
    const m = clock?.get();
    if (m != null) line(m, 0, H, p.playhead);
    const st = usePlayer.getState();
    if (st.status !== "idle" && st.status !== "error") {
      if (st.currentId === aId) line(st.position, RULE_T, yMid, p.playhead);
      else if (st.currentId === bId) line(toMix(g.laneB, st.position), yMid, H - RULE_B, p.playhead);
    }
  }, [g, view, width, H, yMid, clock, aId, bId]);

  useLayoutEffect(() => drawHeads(), [drawHeads]);
  useEffect(() => {
    const un1 = usePlayer.subscribe((s, prev) => {
      if (s.currentId === aId || s.currentId === bId || prev.currentId === aId || prev.currentId === bId) drawHeads();
    });
    const un2 = clock?.subscribe(drawHeads);
    return () => {
      un1();
      un2?.();
    };
  }, [drawHeads, clock, aId, bId]);

  // ---- interaction
  const drag = useRef<{ x: number; start: number; moved: boolean } | null>(null);
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, start: tview.start, moved: false };
  };
  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    setHover(e.clientX - r.left);
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    if (!d.moved && Math.abs(dx) > 3) d.moved = true;
    if (d.moved) tv.set({ start: d.start - dx / view.pxPerSec, span: tview.span });
  };
  const onUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved && onSeek) {
      const r = e.currentTarget.getBoundingClientRect();
      onSeek(view.start + (e.clientX - r.left) / view.pxPerSec);
    }
  };
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const tv = tvRef.current;
      const r = el.getBoundingClientRect();
      const t = tv.view.start + (e.clientX - r.left) / (r.width / tv.view.span);
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        tv.zoom(Math.exp(-e.deltaY * 0.012), t);
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
        e.preventDefault();
        const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        tv.scroll((d / r.width) * tv.view.span);
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);
  const reset = () => initial && tv.set(initial);
  useWaveformKeys(!!hotkeys, (f) => tv.zoom(f), reset);

  if (!g) return null;
  const hm = hover == null ? null : view.start + hover / view.pxPerSec;
  const tb = hm == null ? null : toFile(g.laneB, hm);
  const visibleBars = Math.round(tview.span / barA);

  return (
    <div className={cn("flex flex-col", className)}>
      <div className="mb-1.5 flex h-6 items-center gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">{toolbar}</div>
        <span className="mr-0.5 text-2xs text-fg-subtle num">{visibleBars} bars visible</span>
        <IconButton icon={Minus} label="Zoom out (−)" size="sm" onClick={() => tv.zoom(1 / 1.6)} />
        <IconButton icon={Plus} label="Zoom in (=)" size="sm" onClick={() => tv.zoom(1.6)} />
        <IconButton icon={Scan} label="Back to the transition (0)" size="sm" onClick={reset} />
      </div>
      <div
        ref={boxRef}
        className="relative cursor-crosshair overflow-hidden rounded-sm border border-line bg-bg"
        style={{ height: H }}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerLeave={() => setHover(null)}
      >
        <canvas ref={baseRef} className="absolute inset-0 h-full w-full" />
        <canvas ref={headRef} className="pointer-events-none absolute inset-0 h-full w-full" />
        {labelA && <div className="pointer-events-none absolute left-2 flex items-center gap-1.5" style={{ top: yA + 4 }}>{labelA}</div>}
        {labelB && <div className="pointer-events-none absolute left-2 flex items-center gap-1.5" style={{ top: yB + LH - 22 }}>{labelB}</div>}
        {hover != null && hm != null && (
          <>
            <div className="pointer-events-none absolute top-0 bottom-0 w-px bg-white/35" style={{ left: hover }} />
            <div
              className="pointer-events-none absolute rounded-xs bg-overlay/95 px-1.5 py-1 text-2xs whitespace-nowrap text-fg shadow-pop num"
              style={{ top: yMid + 2, ...(hover > width - 150 ? { right: width - hover + 6 } : { left: hover + 6 }) }}
            >
              <div>
                <span className="font-semibold text-fg-subtle">A</span> {fmtPrecise(hm)}
                {barBeat(a, hm) && <span className="ml-1 text-fg-subtle">bar {barBeat(a, hm)}</span>}
              </div>
              {tb != null && tb >= 0 && (
                <div>
                  <span className="font-semibold text-fg-subtle">B</span> {fmtPrecise(tb)}
                  {barBeat(b, tb) && <span className="ml-1 text-fg-subtle">bar {barBeat(b, tb)}</span>}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

interface Layout {
  H: number;
  LH: number;
  yA: number;
  yMid: number;
  yB: number;
}

function paint(ctx: CanvasRenderingContext2D, dpr: number, g: MixGeometry, view: View, t: Transition, L: Layout): void {
  const p = wavePalette();
  const { H, LH, yA, yMid, yB } = L;
  const { laneA, laneB } = g;
  ctx.fillStyle = p.bg;
  ctx.fillRect(0, 0, view.width, H);

  // rulers / strips backgrounds
  ctx.fillStyle = p.panel;
  ctx.fillRect(0, 0, view.width, RULE_T);
  ctx.fillRect(0, H - RULE_B, view.width, RULE_B);
  ctx.fillRect(0, yMid, view.width, MID);
  ctx.fillStyle = p.line;
  ctx.fillRect(0, RULE_T - 1, view.width, 1);
  ctx.fillRect(0, yMid, view.width, 1);
  ctx.fillRect(0, yMid + MID - 1, view.width, 1);
  ctx.fillRect(0, H - RULE_B, view.width, 1);

  // overlap column (behind the waves)
  const xo0 = xOf(view, g.out);
  const xo1 = xOf(view, g.outEnd);
  if (g.bars > 0) {
    ctx.fillStyle = alpha(p.accent, 0.1);
    ctx.fillRect(xo0, RULE_T, xo1 - xo0, H - RULE_T - RULE_B);
  }

  // deck A
  drawSections(ctx, laneA, view, RULE_T, SEC);
  drawEdges(ctx, laneA, view, yA, LH, { labels: true });
  drawGrid(ctx, laneA, view, yA, LH);
  drawWave(ctx, laneA, view, yA + 3, LH - 6, dpr);
  drawCues(ctx, laneA, view, yA, LH, { labels: false });
  shade(ctx, view, g.outEnd, g.max + 1, RULE_T, LH + SEC, 0.66);

  // deck B
  drawEdges(ctx, laneB, view, yB, LH, { labels: true });
  drawGrid(ctx, laneB, view, yB, LH);
  drawWave(ctx, laneB, view, yB + 3, LH - 6, dpr);
  drawCues(ctx, laneB, view, yB, LH, { labels: false });
  drawSections(ctx, laneB, view, yB + LH, SEC);
  shade(ctx, view, g.min - 1, g.out, yB, LH + SEC, 0.66);

  // time rulers: A's time on top, B's file time at the bottom
  drawTimeRuler(ctx, view, 0, RULE_T, (m) => fmtShort(m));
  drawTimeRuler(ctx, view, H - RULE_B, RULE_B, (m) => fmtShort(toFile(laneB, m)), { from: laneB.origin });

  // ---- mix ruler: A's bars counted from where B comes in
  const barPx = g.barA * view.pxPerSec;
  const j0 = Math.floor((view.start - g.out) / g.barA) - 1;
  const j1 = Math.ceil((view.start + view.width / view.pxPerSec - g.out) / g.barA) + 1;
  const every = [1, 2, 4, 8, 16, 32].find((n) => barPx * n >= 26) ?? 64;
  ctx.font = `600 10px ${p.font}`;
  ctx.textBaseline = "middle";
  // overlap band in the ruler
  if (g.bars > 0) {
    ctx.fillStyle = alpha(p.accent, 0.28);
    ctx.fillRect(xo0, yMid + 1, xo1 - xo0, MID - 2);
  }
  // vocal clash bars
  for (const j of g.clash) {
    const x0 = xOf(view, g.out + j * g.barA);
    const x1 = xOf(view, g.out + (j + 1) * g.barA);
    ctx.fillStyle = alpha(p.danger, 0.45);
    ctx.fillRect(x0 + 0.5, yMid + MID - 9, Math.max(1, x1 - x0 - 1), 8);
    // tint both decks on that bar
    ctx.fillStyle = alpha(p.danger, 0.1);
    ctx.fillRect(x0, yA, x1 - x0, LH);
    ctx.fillRect(x0, yB, x1 - x0, LH);
  }
  for (let j = j0; j <= j1; j++) {
    const x = Math.round(xOf(view, g.out + j * g.barA)) + 0.5;
    if (x < -2 || x > view.width + 2) continue;
    const major = j % 8 === 0;
    ctx.fillStyle = alpha(p.fg, major ? 0.45 : 0.2);
    if (barPx >= 4 || major) ctx.fillRect(x - 0.5, yMid + 1, 1, major ? 7 : 4);
    if (j % every === 0) {
      const label = j >= 0 ? String(j + 1) : `−${-j}`;
      const inOverlap = j >= 0 && j < g.bars;
      ctx.fillStyle = inOverlap ? p.fg : alpha(p.fg, 0.5);
      ctx.fillText(label, x + 3, yMid + 12);
    }
  }
  // flags: B in / A out / swap
  const flag = (x: number, text: string, color: string, align: "left" | "right") => {
    if (x < -40 || x > view.width + 40) return;
    ctx.font = `700 10px ${p.font}`;
    const tw = ctx.measureText(text).width + 8;
    const fx = align === "left" ? x + 1 : x - tw - 1;
    ctx.fillStyle = color;
    ctx.fillRect(fx, yMid + MID - 13, tw, 12);
    ctx.fillStyle = p.bg;
    ctx.fillText(text, fx + 4, yMid + MID - 6.5);
  };
  const vline = (x: number, color: string) => {
    if (x < -1 || x > view.width + 1) return;
    ctx.fillStyle = color;
    ctx.fillRect(Math.round(x), RULE_T, 1.5, H - RULE_T - RULE_B);
  };
  if (g.bars > 0) {
    vline(xo0, alpha(p.accent, 0.95));
    vline(xo1, alpha(p.accent, 0.6));
    const echo = t.technique === "echo out"; // a blend that ends with A echoing out, not an N-bar echo
    const inOverlapLabel = echo ? `${g.bars}-BAR BLEND → ECHO OUT` : `${g.bars}-BAR ${(t.technique ?? "blend").toUpperCase()}`;
    ctx.font = `700 10px ${p.font}`;
    const tw = ctx.measureText(inOverlapLabel).width;
    if (xo1 - xo0 > tw + 70) {
      ctx.fillStyle = p.fg;
      ctx.fillText(inOverlapLabel, (xo0 + xo1) / 2 - tw / 2, yMid + MID - 7);
    }
    flag(xo0, "B IN", p.accent, "left");
    flag(xo1, echo ? "ECHO A OUT" : "A OUT", alpha(p.fg, 0.8), "right");
    if (g.swapBar != null) {
      const xs = xOf(view, g.out + g.swapBar * g.barA);
      ctx.fillStyle = p.low;
      ctx.fillRect(Math.round(xs), yA, 1.5, LH);
      ctx.fillRect(Math.round(xs), yB, 1.5, LH);
      ctx.beginPath();
      ctx.moveTo(xs, yMid + 2);
      ctx.lineTo(xs + 4, yMid + 6);
      ctx.lineTo(xs, yMid + 10);
      ctx.lineTo(xs - 4, yMid + 6);
      ctx.closePath();
      ctx.fill();
      ctx.font = `700 10px ${p.font}`;
      const label = "BASS SWAP";
      const lw = ctx.measureText(label).width;
      ctx.fillStyle = alpha(p.bg, 0.8);
      ctx.fillRect(xs + 5, yA + LH - 15, lw + 8, 12);
      ctx.fillStyle = p.low;
      ctx.fillText(label, xs + 9, yA + LH - 8.5);
    }
  } else {
    vline(xo0, p.accent);
    flag(xo0, t.technique === "echo out" ? "ECHO OUT · B IN" : t.technique === "drop swap" ? "DROP SWAP · B IN" : "CUT · B IN", p.accent, "left");
  }
}

function fmtShort(s: number): string {
  if (!Number.isFinite(s)) return "";
  const neg = s < -0.05;
  const a = Math.abs(s);
  const m = Math.floor(a / 60);
  const sec = Math.floor(a - m * 60);
  return `${neg ? "−" : ""}${m}:${String(sec).padStart(2, "0")}`;
}
