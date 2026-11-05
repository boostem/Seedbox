/**
 * Full-track 3-band waveform "deck": an overview strip (whole track, sections, viewport) above a zoomable,
 * scrollable main view with the bar/phrase grid, intro/outro brackets, cues and section bands. The playhead follows
 * the global player when it plays this track, or any `source`. Zoom, scroll, hover and the playhead redraw the canvases
 * (and the hover readout) from refs on the next animation frame; React only re-renders when "zoomed in?" flips. See
 * PERFORMANCE.md.
 *
 * With `deck`: a beat-phase ruler and the bars to the next phrase, and the follow mode's switch. Centred scrolls the
 * waveform under a fixed playhead; page moves the playhead and turns pages, for editing (or force it with `follow`).
 *
 * Mouse: click = seek (plays from there) · drag = scroll (a centred view goes back to the playhead 2.5 s later) ·
 * ⌘/ctrl + wheel or pinch = zoom · horizontal wheel = scroll. Keys (with `hotkeys`): = / − zoom, 0 fit.
 */
import { Minus, Plus, Scan } from "lucide-react";
import { type PointerEvent as ReactPointerEvent, type ReactNode, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { TrackAnalysis } from "@/api/types";
import { playerSource } from "@/audio/player";
import { centredStart, FrameSmoother, headTrace, type PlayheadSource } from "@/audio/playhead";
import { IconButton } from "@/components/ui";
import { cn } from "@/lib/cn";
import { barBeat, drawCues, drawEdges, drawGrid, drawPhraseBands, drawRegions, drawSections, drawWave, fmtPrecise, laneOf, setupCanvas, shade, type View, type WaveRegion, xOf } from "./draw";
import { useFrame, useWaveformKeys, useViewCtl, useViewFact, useWidthRef } from "./hooks";
import { alpha, sectionColor, wavePalette } from "./palette";
import { beatPhase, fmtBarsBeats } from "./phase";
import { useWaveLook, useWaveSettings, type WaveFollow } from "./settings";
import { WaveFollowTabs } from "./WaveSettings";
import type { WaveData } from "./prepare";
import { useWave } from "./waveData";
import { mayDraw } from "@/lib/frameCap";

export interface TrackWaveformProps {
  analysis: TrackAnalysis;
  /** Track id: the playhead shows while the global player plays this id. */
  trackId: string;
  /** Where the playhead comes from when it isn't the global player: the live engine (`engineSource`). */
  source?: PlayheadSource;
  /** The deck's controls: the beat-phase ruler, the bars to the next phrase and the follow switch. */
  deck?: boolean;
  /** Force the follow mode. Otherwise a deck uses the setting, and any other view pages (it's for editing). */
  follow?: WaveFollow;
  /** Click on the main view → seek to file time t (s). */
  onSeek?: (t: number) => void;
  /** The waveform when it doesn't come with `analysis`: the binary file (`useTrackWave`). */
  wave?: WaveData | null;
  /** Spans of the track to mark on the main view: where a transition happens. */
  regions?: WaveRegion[];
  /** Tint every other phrase. */
  phraseBands?: boolean;
  /** Seconds of track across the view, chosen from outside: two decks keep a beat the same width this way. */
  span?: number;
  /** No row of zoom buttons: the page has its own. */
  bare?: boolean;
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
/** After a manual scroll the view waits this long before it follows the playhead again. */
const MANUAL_MS = 2500;
/** What a centred view zooms to when it starts from the whole track, in bars. */
const CENTRED_BARS = 16;
const RULER_W = 112;
const RULER_H = 10;

/** Text that changes with the clock: only touches the DOM when it changes. */
function setText(el: HTMLElement, text: string): void {
  if (el.textContent !== text) el.textContent = text;
}

export function TrackWaveform({ analysis, trackId, source, deck, follow, onSeek, wave: waveProp, regions, phraseBands, span, bare, height = 132, overview = true, hotkeys, toolbar, className }: TrackWaveformProps) {
  const ownWave = useWave(analysis);
  const wave = waveProp ?? ownWave;
  const lane = useMemo(() => laneOf(analysis, wave), [analysis, wave]);
  const duration = analysis.duration_s || wave?.duration || 1;
  const minSpan = Math.max(4, (analysis.bar_s || 2) * 4);
  const ctl = useViewCtl(0, duration, minSpan);
  const zoomed = useViewFact(ctl, (v) => v.span < duration - 0.01);
  const atMax = useViewFact(ctl, (v) => v.span <= minSpan + 0.01);
  useWaveLook(); // a new mode re-renders, and the layout effect below repaints
  const followSetting = useWaveSettings((s) => s.follow);
  const mode: WaveFollow = follow ?? (deck ? followSetting : "page");
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const src = useMemo(() => source ?? playerSource(trackId), [source, trackId]);

  const boxRef = useRef<HTMLDivElement>(null);
  const baseRef = useRef<HTMLCanvasElement>(null);
  const waveRef = useRef<HTMLCanvasElement>(null);
  const overRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLCanvasElement>(null);
  const ovRef = useRef<HTMLCanvasElement>(null);
  const ovHeadRef = useRef<HTMLCanvasElement>(null);
  const barsRef = useRef<HTMLSpanElement>(null);
  const hoverLineRef = useRef<HTMLDivElement>(null);
  const hoverTipRef = useRef<HTMLDivElement>(null);
  const hoverX = useRef<number | null>(null);
  const rulerRef = useRef<HTMLCanvasElement>(null);
  const barLabelRef = useRef<HTMLSpanElement>(null);
  const phraseLabelRef = useRef<HTMLSpanElement>(null);
  /** the user scrolled the view, so it isn't following the playhead now; and when they last did */
  const manual = useRef(false);
  const lastManual = useRef(0);
  /** the playhead of this animation frame while playing, so every layer of a frame draws the same instant */
  const frameT = useRef<number | null>(null);
  const smoother = useRef(new FrameSmoother());
  /** the view's left edge at the last paint, and what the head layer and the ruler last drew: to skip repeat paints */
  const drawnStart = useRef(NaN);
  const headKey = useRef("");
  const rulerKey = useRef("");
  /** device x of the played/unplayed split in the main view and the overview at the last paint */
  const splitPx = useRef<[number, number]>([NaN, NaN]);

  const deviceRatio = () => Math.min(3, window.devicePixelRatio || 1);

  /** The playhead's file time, or null when the source doesn't have this track. */
  const playedAt = (): number | null => frameT.current ?? src.time();

  /** Whether the view is centred on the playhead now: the fixed column is then `Math.round(width / 2 * dpr)`. */
  const centredOn = (t: number | null): t is number => modeRef.current === "centred" && t != null && !manual.current;

  const viewOf = (w: number): View => {
    const v = ctl.get();
    const pxPerSec = w / v.span || 1;
    const t = playedAt();
    if (!centredOn(t)) return { start: v.start, pxPerSec, width: w };
    const dpr = deviceRatio();
    return { start: centredStart(t, Math.round((w / 2) * dpr), pxPerSec, dpr), pxPerSec, width: w };
  };

  /** Hand the view to the user: what a centred view shows now becomes the view they scroll from. */
  const goManual = () => {
    if (centredOn(playedAt())) ctl.set({ start: viewOf(widthRef.current).start, span: ctl.get().span });
    manual.current = true;
    lastManual.current = performance.now();
  };

  /** Where the played/unplayed split falls now, in device px of the main view and of the overview. */
  const splitNow = (t: number | null): [number, number] => {
    const width = widthRef.current;
    if (t == null || !width) return [NaN, NaN];
    const dpr = deviceRatio();
    return [Math.round(xOf(viewOf(width), t) * dpr), Math.round((t / duration) * width * dpr)];
  };

  // ---- drawing (from refs; called on the next animation frame, never from a render driven by the view). Layers, so
  // what moves often repaints alone: grid and edges under the waveform, the waveform, cues and sections over it, the
  // playhead on top; the overview's waveform is static, its viewport box and playhead sit on the canvas above it.
  const waveH = height - TOP - SECTIONS;

  /** The main view's waveform layer: on zoom/scroll, and while playing when the played split moves a pixel. */
  const paintWave = () => {
    const width = widthRef.current;
    const c = waveRef.current;
    if (!c || !width) return;
    const played = playedAt();
    splitPx.current[0] = splitNow(played)[0];
    const s = setupCanvas(c, width, height);
    if (s) drawWave(s.ctx, lane, viewOf(width), TOP + 2, waveH - 4, s.dpr, { played });
  };

  /** The overview's waveform and sections: on resize, a new track or mode, and when its played split moves. */
  const paintOverview = () => {
    const width = widthRef.current;
    const oc = ovRef.current;
    if (!oc || !width || !overview) return;
    const played = playedAt();
    splitPx.current[1] = splitNow(played)[1];
    const s = setupCanvas(oc, width, OVERVIEW_H);
    if (!s) return;
    const { ctx, dpr } = s;
    const p = wavePalette();
    const ovView: View = { start: 0, pxPerSec: width / duration || 1, width };
    ctx.fillStyle = p.bg;
    ctx.fillRect(0, 0, width, OVERVIEW_H);
    drawWave(ctx, lane, ovView, 1, OVERVIEW_H - 6, dpr, { played });
    for (const sec of analysis.sections ?? []) {
      const x0 = xOf(ovView, sec.start_s);
      const x1 = xOf(ovView, sec.end_s);
      ctx.fillStyle = alpha(sectionColor(sec.kind, p), 0.9);
      ctx.fillRect(x0 + 0.5, OVERVIEW_H - 3, Math.max(1, x1 - x0 - 1), 3);
    }
  };

  /** Everything that moves with zoom and scroll. */
  const paintBase = () => {
    const width = widthRef.current;
    const base = baseRef.current;
    const over = overRef.current;
    if (!base || !over || !width) return;
    const view = viewOf(width);
    drawnStart.current = view.start;
    const tv = ctl.get();
    const p = wavePalette();
    const b = setupCanvas(base, width, height);
    let edgeLabels: ReturnType<typeof drawEdges> = [];
    if (b) {
      b.ctx.fillStyle = p.bg;
      b.ctx.fillRect(0, 0, width, height);
      edgeLabels = drawEdges(b.ctx, lane, view, TOP, waveH, { labels: true });
      if (phraseBands) drawPhraseBands(b.ctx, lane, view, TOP, waveH);
      if (regions) drawRegions(b.ctx, regions, view, TOP, waveH);
      drawGrid(b.ctx, lane, view, TOP, waveH, { labels: true, labelY: 2 });
    }
    paintWave();
    const o = setupCanvas(over, width, height);
    if (o) {
      o.ctx.fillStyle = alpha(p.fg, 0.08);
      o.ctx.fillRect(0, TOP + waveH / 2, width, 1);
      drawCues(o.ctx, lane, view, TOP, waveH, { topOffset: 0, avoid: edgeLabels });
      drawSections(o.ctx, lane, view, height - SECTIONS, SECTIONS);
    }
    if (barsRef.current) setText(barsRef.current, tv.span < duration - 0.01 ? `${Math.max(1, Math.round(tv.span / (analysis.bar_s || 1)))} bars visible` : "Whole track");
    paintHover();
    paintHead();
  };

  const paintAll = () => {
    headKey.current = rulerKey.current = "";
    paintPhase();
    paintOverview();
    paintBase();
  };

  /** The beat-phase ruler and the counters: the bar's four beats with the current one filling up, and bars to the
   * next phrase. Paints when a cell's fill moves a pixel, and writes the text only when it changes. */
  const paintPhase = () => {
    const c = rulerRef.current;
    if (!deck || !c) return;
    const t = playedAt();
    const ph = beatPhase(analysis, t ?? 0);
    const on = t != null && ph != null;
    const cell = (RULER_W - 3 * 2) / 4;
    const key = on ? `${ph.beat} ${Math.round(ph.frac * cell)}` : "off";
    if (key !== rulerKey.current) {
      rulerKey.current = key;
      const s = setupCanvas(c, RULER_W, RULER_H);
      if (s) {
        const p = wavePalette();
        for (let i = 0; i < 4; i++) {
          const x = i * (cell + 2);
          s.ctx.fillStyle = alpha(p.fg, 0.1);
          s.ctx.fillRect(x, 0, cell, RULER_H);
          if (!on || i > ph.beat) continue;
          s.ctx.fillStyle = i === 0 ? p.phrase : alpha(p.fg, i === ph.beat ? 0.75 : 0.35);
          s.ctx.fillRect(x, 0, i === ph.beat ? cell * ph.frac : cell, RULER_H);
        }
      }
    }
    if (barLabelRef.current) setText(barLabelRef.current, on ? `Bar ${ph.bar + 1}.${ph.beat + 1}` : "Bar –");
    if (phraseLabelRef.current) {
      const to = on ? ph.beatsToPhrase : null;
      setText(phraseLabelRef.current, to == null ? "" : `${ph?.phraseLevel ? `${ph.phraseLevel}-bar phrase` : "Phrase"} in ${fmtBarsBeats(to)}`);
    }
  };

  /** The playheads, and the overview's viewport box: a few rects, repainted when one of them moves a pixel. */
  const paintHead = () => {
    const width = widthRef.current;
    const played = playedAt();
    const on = played != null;
    const pos = played ?? 0;
    const view = viewOf(width);
    const tv = ctl.get();
    const dpr = deviceRatio();
    // a centred playhead doesn't move, however the view's rounding falls
    const headX = centredOn(played) ? Math.round(Math.round((width / 2) * dpr) / dpr) + 0.5 : Math.round(xOf(view, pos)) + 0.5;
    const ovPx = (m: number) => Math.round((m / duration) * width * dpr);
    const key = `${width} ${height} ${on} ${headX} ${ovPx(pos)} ${ovPx(view.start)} ${ovPx(view.start + tv.span)}`;
    if (key === headKey.current) return;
    headKey.current = key;
    const p = wavePalette();
    const hc = headRef.current;
    if (hc && width) {
      const s = setupCanvas(hc, width, height);
      if (s && on) {
        const x = headX;
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
      if (!s) return;
      const { ctx } = s;
      const ovView: View = { start: 0, pxPerSec: width / duration || 1, width };
      // viewport (only when zoomed in)
      if (tv.span < duration - 0.01) {
        const x0 = xOf(ovView, view.start);
        const x1 = xOf(ovView, view.start + tv.span);
        shade(ctx, ovView, 0, Math.max(0, view.start), 0, OVERVIEW_H, 0.55);
        shade(ctx, ovView, Math.min(duration, view.start + tv.span), duration, 0, OVERVIEW_H, 0.55);
        ctx.strokeStyle = alpha(p.fg, 0.55);
        ctx.lineWidth = 1;
        ctx.strokeRect(Math.round(x0) + 0.5, 0.5, Math.max(2, Math.round(x1 - x0) - 1), OVERVIEW_H - 1);
      }
      if (on) {
        ctx.fillStyle = p.playhead;
        ctx.fillRect(Math.round((pos / duration) * width), 0, 1.5, OVERVIEW_H);
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
  const frameAll = useFrame(paintAll);
  const frameHover = useFrame(paintHover);
  const widthRef = useWidthRef(boxRef, frameAll);

  // props changed (another analysis, the worker delivered the waveform, a new height or mode): redraw now
  useLayoutEffect(() => paintAll());
  useEffect(() => ctl.subscribe(frameBase), [ctl, frameBase]);
  useLayoutEffect(() => ctl.setOverscroll(mode === "centred" ? 0.5 : 0), [ctl, mode]);
  useLayoutEffect(() => {
    if (span) ctl.set({ start: ctl.get().start, span });
  }, [ctl, span]);

  // ---- playhead: its own rAF loop while this track plays, reading the audio clock every frame. Centred, everything
  // scrolls: the view repaints when it reaches another device column. Paged, only the playhead moves, and the waveform
  // (brighter played part) repaints when the split reaches another device pixel.
  const tickFn = useRef(() => {});
  tickFn.current = () => {
    const t = playedAt();
    if (centredOn(t)) {
      if (viewOf(widthRef.current).start !== drawnStart.current) paintBase();
    } else {
      const m = splitNow(t)[0];
      if (m !== splitPx.current[0]) paintWave();
    }
    if (splitNow(t)[1] !== splitPx.current[1]) paintOverview();
    paintHead();
    paintPhase();
  };
  const paintAllNow = useRef(frameAll);
  paintAllNow.current = frameAll;
  useEffect(() => {
    let raf = 0;
    let wasCentred = false;
    let lastSeen: number | null = null;
    // to page mode: the view stays where the centred one was
    if (mode === "page" && Number.isFinite(drawnStart.current)) ctl.set({ start: drawnStart.current, span: ctl.get().span });
    const loop = (now: number) => {
      if (mayDraw()) {
        const clock = src.time();
        const head = clock == null ? null : smoother.current.at(clock, now, src.rate?.() ?? 1);
        frameT.current = head;
        if (headTrace.on && clock != null && head != null) {
          headTrace.frames.push(now);
          headTrace.clock.push(clock);
          headTrace.head.push(head);
        }
        if (manual.current && performance.now() - lastManual.current > MANUAL_MS) manual.current = false;
        // a paged view turns the page, unless the user just scrolled
        if (head != null && modeRef.current === "page" && !manual.current) ctl.reveal(head);
        tickFn.current();
      }
      raf = requestAnimationFrame(loop);
    };
    const sync = () => {
      const playing = src.playing();
      // from here on, the source's own time: this frame's has gone
      frameT.current = null;
      const t = src.time();
      const centred = modeRef.current === "centred" && t != null;
      if (centred && !wasCentred && ctl.get().span >= duration - 0.01 && analysis.bar_s > 0) {
        ctl.set({ start: ctl.get().start, span: analysis.bar_s * CENTRED_BARS }); // the whole track can't scroll
      }
      // nothing loaded any more: the view stays where it was instead of going back to where it was before it played
      if (wasCentred && t == null && !manual.current && Number.isFinite(drawnStart.current)) ctl.set({ start: drawnStart.current, span: ctl.get().span });
      // a seek while paused brings a centred view back to the playhead
      if (!playing && t != null && lastSeen != null && Math.abs(t - lastSeen) > 0.01) manual.current = false;
      wasCentred = centred;
      lastSeen = t;
      if (playing && !raf) raf = requestAnimationFrame(loop);
      if (!playing) {
        cancelAnimationFrame(raf);
        raf = 0;
        smoother.current.reset();
        paintAllNow.current();
      }
    };
    sync();
    const un = src.subscribe(sync);
    return () => {
      un();
      cancelAnimationFrame(raf);
      frameT.current = null;
      smoother.current.reset();
    };
  }, [ctl, src, mode, duration, analysis.bar_s]);

  // ---- interaction
  const drag = useRef<{ x: number; start: number; moved: boolean; id: number } | null>(null);
  const timeAt = (clientX: number) => {
    const r = baseRef.current?.getBoundingClientRect();
    if (!r) return 0;
    const view = viewOf(widthRef.current);
    return view.start + (clientX - r.left) / view.pxPerSec;
  };
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, start: viewOf(widthRef.current).start, moved: false, id: e.pointerId };
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
      goManual();
      const v = ctl.get();
      ctl.set({ start: d.start - dx / (widthRef.current / v.span || 1), span: v.span });
    }
  };
  const onUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved && onSeek) {
      manual.current = false;
      onSeek(Math.max(0, Math.min(duration, timeAt(e.clientX))));
    }
  };

  // wheel: native listener (passive: false) so ⌘/pinch zoom can preventDefault
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const r = el.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        // a centred view keeps its playhead in the centre; a paged one keeps the time under the mouse there
        if (!centredOnRef.current()) {
          goManualRef.current();
          ctl.zoom(Math.exp(-e.deltaY * 0.012), ctl.get().start + (e.clientX - r.left) / (r.width / ctl.get().span));
        } else {
          ctl.zoom(Math.exp(-e.deltaY * 0.012));
        }
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
        e.preventDefault();
        goManualRef.current();
        const v = ctl.get();
        const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        ctl.scroll((d / r.width) * v.span);
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [ctl]);

  const goManualRef = useRef(goManual);
  goManualRef.current = goManual;
  const centredOnRef = useRef(() => centredOn(playedAt()));
  centredOnRef.current = () => centredOn(playedAt());

  const ovDrag = useRef(false);
  const ovTo = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const t = ((e.clientX - r.left) / r.width) * duration;
    goManual();
    const span = ctl.get().span;
    ctl.set({ start: t - span / 2, span });
  };

  useWaveformKeys(!!hotkeys, (f) => ctl.zoom(f), () => ctl.fit());

  return (
    <div className={cn("flex flex-col", className)}>
      {!bare && (
        <div className="mb-1.5 flex h-6 items-center gap-2">
          <div className="flex min-w-0 flex-1 items-center gap-2">{toolbar}</div>
          {deck && !follow && <WaveFollowTabs />}
          <span ref={barsRef} className="mr-0.5 text-2xs text-fg-subtle num" />
          <IconButton icon={Minus} label="Zoom out (−)" size="sm" onClick={() => ctl.zoom(1 / 1.6)} disabled={!zoomed} />
          <IconButton icon={Plus} label="Zoom in (=)" size="sm" onClick={() => ctl.zoom(1.6)} disabled={atMax} />
          <IconButton icon={Scan} label="Fit whole track (0)" size="sm" onClick={ctl.fit} disabled={!zoomed} />
        </div>
      )}
      {deck && (
        <div className="mb-1.5 flex h-4 items-center gap-3 text-2xs text-fg-subtle num">
          <canvas ref={rulerRef} aria-hidden="true" style={{ width: RULER_W, height: RULER_H }} title="Beat phase: the four beats of the bar" />
          <span ref={barLabelRef} />
          <span ref={phraseLabelRef} title="Bars.beats to the next phrase" />
        </div>
      )}
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
          <canvas ref={baseRef} className="absolute inset-0 h-full w-full" />
          <canvas ref={waveRef} className="absolute inset-0 h-full w-full" />
          <canvas ref={overRef} className="absolute inset-0 h-full w-full" />
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
