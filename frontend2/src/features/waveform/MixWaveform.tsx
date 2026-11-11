/**
 * MIX VIEW: deck A above deck B on one time axis, aligned so B's cue (in_start_s) sits under A's out_start_s and
 * B is time-scaled by its pitch change. The overlap is a highlighted column through both decks, with a bar ruler
 * between them (bars counted from where B comes in), vocal-clash bars, the bass-swap bar and B-in / A-out flags.
 * Unheard parts (A after it's out, B before its cue) are dimmed.
 *
 * Playheads: the global player when it plays A or B, plus an optional external clock (the audition's mix time).
 * Zoom, scroll, hover and the playheads redraw from refs on the next animation frame, without React renders
 * (PERFORMANCE.md).
 */
import { Minus, Plus, Scan } from "lucide-react";
import { type PointerEvent as ReactPointerEvent, type ReactNode, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { TrackAnalysis, Transition } from "@/api/types";
import { player, usePlayer } from "@/audio/player";
import { IconButton } from "@/components/ui";
import { cn } from "@/lib/cn";
import { barBeat, drawCues, drawEdges, drawGrid, drawSections, drawTimeRuler, drawWave, fmtPrecise, setupCanvas, shade, toFile, toMix, type View, xOf } from "./draw";
import { useFrame, useWaveformKeys, useViewCtl, useViewFact, useWidthRef } from "./hooks";
import { type MixGeometry, mixGeometry } from "./mix";
import { alpha, styleColor, wavePalette } from "./palette";
import { useWaveLook } from "./settings";
import { useWave } from "./waveData";
import { mayDraw } from "@/lib/frameCap";

/** External playhead source (e.g. the audition): current mix time or null. */
export interface MixClock {
  get: () => number | null;
  /** Called when the clock starts, stops or jumps; while it runs, the playhead reads `get()` every frame. */
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
  const waveA = useWave(a);
  const waveB = useWave(b);
  const g = useMemo(() => mixGeometry(transition, a, b, aBpm, { a: waveA, b: waveB }), [transition, a, b, aBpm, waveA, waveB]);
  const LH = laneHeight;
  const H = RULE_T + SEC + LH + MID + LH + SEC + RULE_B;
  const yA = RULE_T + SEC; // A wave band top
  const yMid = yA + LH;
  const yB = yMid + MID;

  const barA = g?.barA ?? 2;
  const min = g?.min ?? 0;
  const max = g?.max ?? 1;
  const initial = g ? { start: g.out - 12 * barA, span: g.overlapS + 28 * barA } : undefined;
  const ctl = useViewCtl(min, max, 4 * barA, initial);
  const visibleBars = useViewFact(ctl, (v) => Math.round(v.span / barA));
  useWaveLook(); // a new mode re-renders, and the layout effect below repaints

  const boxRef = useRef<HTMLDivElement>(null);
  const baseRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLCanvasElement>(null);
  const hoverLineRef = useRef<HTMLDivElement>(null);
  const hoverTipRef = useRef<HTMLDivElement>(null);
  const hoverX = useRef<number | null>(null);

  const viewOf = (w: number): View => {
    const v = ctl.get();
    return { start: v.start, pxPerSec: w / v.span || 1, width: w };
  };

  /** Where each deck has played to (file time), from the external clock or the player; null where nothing plays. */
  const playedNow = (): Played => {
    if (!g) return { a: null, b: null };
    const m = clock?.get();
    if (m != null) return { a: toFile(g.laneA, m), b: toFile(g.laneB, m) };
    const st = usePlayer.getState();
    if (st.status === "idle" || st.status === "error") return { a: null, b: null };
    if (st.currentId === aId) return { a: player.time(), b: null };
    if (st.currentId === bId) return { a: null, b: player.time() };
    return { a: null, b: null };
  };
  /** The played/unplayed splits in device px, to repaint the waves only when one moves a pixel. */
  const splitKey = (pl: Played): string => {
    if (!g || (pl.a == null && pl.b == null)) return "";
    const view = viewOf(widthRef.current);
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const px = (lane: MixGeometry["laneA"], t: number | null) => (t == null ? "" : Math.round(xOf(view, toMix(lane, t)) * dpr));
    return `${px(g.laneA, pl.a)}|${px(g.laneB, pl.b)}`;
  };
  const lastSplit = useRef("");

  const paintBase = () => {
    const c = baseRef.current;
    const width = widthRef.current;
    if (!c || !width || !g) return;
    const s = setupCanvas(c, width, H);
    const played = playedNow();
    lastSplit.current = splitKey(played);
    if (s) paint(s.ctx, s.dpr, g, viewOf(width), transition, { H, LH, yA, yMid, yB }, played);
    paintHover();
    paintHeads();
  };

  // ---- playheads
  const paintHeads = (): boolean => {
    const c = headRef.current;
    const width = widthRef.current;
    if (!c || !width || !g) return false;
    const s = setupCanvas(c, width, H);
    if (!s) return false;
    const { ctx } = s;
    const p = wavePalette();
    const view = viewOf(width);
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
      if (st.currentId === aId) line(player.time(), RULE_T, yMid, p.playhead);
      else if (st.currentId === bId) line(toMix(g.laneB, player.time()), yMid, H - RULE_B, p.playhead);
    }
    // keep animating while something moves
    return m != null || (st.status === "playing" && (st.currentId === aId || st.currentId === bId));
  };

  /** The hover line and the A/B time readout: DOM writes through refs, no state. */
  const paintHover = () => {
    const line = hoverLineRef.current;
    const tip = hoverTipRef.current;
    if (!line || !tip || !g) return;
    const hx = hoverX.current;
    const width = widthRef.current;
    line.hidden = tip.hidden = hx == null;
    if (hx == null) return;
    const view = viewOf(width);
    const hm = view.start + hx / view.pxPerSec;
    const tb = toFile(g.laneB, hm);
    line.style.left = `${hx}px`;
    if (hx > width - 150) {
      tip.style.left = "";
      tip.style.right = `${width - hx + 6}px`;
    } else {
      tip.style.right = "";
      tip.style.left = `${hx + 6}px`;
    }
    const [rowA, rowB] = [tip.children[0] as HTMLElement, tip.children[1] as HTMLElement];
    const bbA = barBeat(a, hm);
    rowA.children[1].textContent = fmtPrecise(hm);
    rowA.children[2].textContent = bbA ? `bar ${bbA}` : "";
    rowB.hidden = !(tb >= 0);
    const bbB = barBeat(b, tb);
    rowB.children[1].textContent = fmtPrecise(tb);
    rowB.children[2].textContent = bbB ? `bar ${bbB}` : "";
  };

  const frameBase = useFrame(paintBase);
  const frameHover = useFrame(paintHover);
  const widthRef = useWidthRef(boxRef, frameBase);
  useLayoutEffect(() => paintBase());
  useEffect(() => ctl.subscribe(frameBase), [ctl, frameBase]);

  // a rAF loop while the player plays A or B or the external clock runs; idle otherwise
  // the waves (brighter played part) repaint only when a split moves to another device pixel
  const headsFn = useRef(paintHeads);
  headsFn.current = () => {
    if (splitKey(playedNow()) !== lastSplit.current) paintBase();
    return paintHeads();
  };
  useEffect(() => {
    let raf = 0;
    const loop = () => {
      raf = !mayDraw() || headsFn.current() ? requestAnimationFrame(loop) : 0;
    };
    const kick = () => {
      if (!raf) raf = requestAnimationFrame(loop);
    };
    const un1 = usePlayer.subscribe((s, prev) => {
      if (s.currentId === aId || s.currentId === bId || prev.currentId === aId || prev.currentId === bId) kick();
    });
    const un2 = clock?.subscribe(kick);
    kick();
    return () => {
      un1();
      un2?.();
      cancelAnimationFrame(raf);
    };
  }, [clock, aId, bId]);

  // ---- interaction
  const drag = useRef<{ x: number; start: number; moved: boolean } | null>(null);
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, start: ctl.get().start, moved: false };
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
      const v = ctl.get();
      ctl.set({ start: d.start - dx / (widthRef.current / v.span || 1), span: v.span });
    }
  };
  const onUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved && onSeek) {
      const r = e.currentTarget.getBoundingClientRect();
      const view = viewOf(widthRef.current);
      onSeek(view.start + (e.clientX - r.left) / view.pxPerSec);
    }
  };
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const v = ctl.get();
      const r = el.getBoundingClientRect();
      const t = v.start + (e.clientX - r.left) / (r.width / v.span);
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        ctl.zoom(Math.exp(-e.deltaY * 0.012), t);
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
        e.preventDefault();
        const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        ctl.scroll((d / r.width) * v.span);
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [ctl]);
  const reset = () => initial && ctl.set(initial);
  useWaveformKeys(!!hotkeys, (f) => ctl.zoom(f), reset);

  if (!g) return null;

  return (
    <div className={cn("flex flex-col", className)}>
      <div className="mb-1.5 flex h-6 items-center gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">{toolbar}</div>
        <span className="mr-0.5 text-2xs text-fg-subtle num">{visibleBars} bars visible</span>
        <IconButton icon={Minus} label="Zoom out (−)" size="sm" onClick={() => ctl.zoom(1 / 1.6)} />
        <IconButton icon={Plus} label="Zoom in (=)" size="sm" onClick={() => ctl.zoom(1.6)} />
        <IconButton icon={Scan} label="Back to the transition (0)" size="sm" onClick={reset} />
      </div>
      <div
        ref={boxRef}
        className="relative cursor-crosshair overflow-hidden rounded-sm border border-line bg-bg"
        style={{ height: H }}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerLeave={() => {
          hoverX.current = null;
          frameHover();
        }}
      >
        <canvas ref={baseRef} className="absolute inset-0 h-full w-full" />
        <canvas ref={headRef} className="pointer-events-none absolute inset-0 h-full w-full" />
        {labelA && <div className="pointer-events-none absolute left-2 flex items-center gap-1.5" style={{ top: yA + 4 }}>{labelA}</div>}
        {labelB && <div className="pointer-events-none absolute left-2 flex items-center gap-1.5" style={{ top: yB + LH - 22 }}>{labelB}</div>}
        <div ref={hoverLineRef} hidden className="pointer-events-none absolute top-0 bottom-0 w-px bg-white/35" />
        <div
          ref={hoverTipRef}
          hidden
          className="pointer-events-none absolute rounded-xs bg-overlay/95 px-1.5 py-1 text-2xs whitespace-nowrap text-fg shadow-pop num"
          style={{ top: yMid + 2 }}
        >
          <div>
            <span className="font-semibold text-fg-subtle">A</span> <span />
            <span className="ml-1 text-fg-subtle empty:hidden" />
          </div>
          <div>
            <span className="font-semibold text-fg-subtle">B</span> <span />
            <span className="ml-1 text-fg-subtle empty:hidden" />
          </div>
        </div>
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

interface Played {
  a: number | null;
  b: number | null;
}

function paint(ctx: CanvasRenderingContext2D, dpr: number, g: MixGeometry, view: View, t: Transition, L: Layout, played: Played): void {
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
  const styleCol = styleColor(t.plan_technique ?? t.technique, p);
  if (g.bars > 0) {
    ctx.fillStyle = alpha(styleCol, 0.12);
    ctx.fillRect(xo0, RULE_T, xo1 - xo0, H - RULE_T - RULE_B);
  }

  // deck A
  drawSections(ctx, laneA, view, RULE_T, SEC);
  drawEdges(ctx, laneA, view, yA, LH, { labels: true });
  drawGrid(ctx, laneA, view, yA, LH);
  drawWave(ctx, laneA, view, yA + 3, LH - 6, dpr, { played: played.a });
  drawCues(ctx, laneA, view, yA, LH, { labels: false });
  shade(ctx, view, g.outEnd, g.max + 1, RULE_T, LH + SEC, 0.66);

  // deck B
  drawEdges(ctx, laneB, view, yB, LH, { labels: true });
  drawGrid(ctx, laneB, view, yB, LH);
  drawWave(ctx, laneB, view, yB + 3, LH - 6, dpr, { played: played.b });
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
    ctx.fillStyle = alpha(styleCol, 0.32);
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
