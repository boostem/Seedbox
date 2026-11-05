/**
 * The whole set on one timeline (#75). One canvas, one view: a wheel, a key or a click on a transition goes from the
 * whole set to a few bars and back without changing renderer or page.
 *
 * - Press on a track and drag: reorder it. Pins keep their places.
 * - Click a transition: open it in the editor. Click anywhere else, or on the ruler: put the playhead there.
 * - ⌘/ctrl + wheel or pinch: zoom. Horizontal wheel, or dragging the empty part: scroll. = / − / 0 too.
 * - In draw mode, drag on the lanes: draw the arc.
 *
 * Nothing that moves renders React (PERFORMANCE.md): the view is a controller, gestures live in refs and the playhead is
 * an element moved from its own rAF loop while the set plays.
 */
import { Maximize2, Minus, Pause, Play, Plus } from "lucide-react";
import { type PointerEvent as ReactPointerEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { Button, IconButton } from "@/components/ui";
import { setupCanvas } from "@/features/waveform/draw";
import { type TimeView, useFrame, useViewCtl, useWaveformKeys, useWidthRef } from "@/features/waveform/hooks";
import { wavePalette } from "@/features/waveform/palette";
import type { WaveData } from "@/features/waveform/prepare";
import { useWaveLook } from "@/features/waveform/settings";
import { fmtTime } from "@/lib/format";
import { mayDraw } from "@/lib/frameCap";
import { ARC_GRID, type ArcPoint, paint as paintStroke, toGrid, toPoints } from "../arc";
import { arcTimeAt, dropPosition, moveTrack, type PlacedTrack, type PlacedTransition, type TimelineLayout } from "./layout";
import { ARC_PAD, CAMELOT_H, HEIGHT, LANE_GAP, LANE_H, laneTop, LANES_TOP, paintTimeline, RULER_H, type Scene } from "./paint";
import type { SetPlayback } from "./playback";
import { SetWaves } from "./waves";

const MIN_SPAN_S = 6;
const DRAG_PX = 4;
const EDGE_PX = 28;
const ZOOM_MS = 320;
const MIN_ENERGY_RANGE = 0.2;
const FIT_PAD = 1.4; // a transition takes this share less than the whole view: some of each track shows around it

export interface SetTimelineProps {
  layout: TimelineLayout;
  /** The target arc, as [arc time, level]. */
  arc: [number, number][];
  onArc: (points: [number, number][]) => void;
  drawing: boolean;
  /** Off while another order is only proposed. */
  reorderable: boolean;
  pinned: ReadonlySet<number>;
  onReorder: (order: PlacedTrack["order"][]) => void;
  /** The transition open in the editor, by position. */
  open: number | null;
  onOpen: (transition: PlacedTransition) => void;
  playback: SetPlayback;
  /** Waveforms from here instead of the server: the benchmark's. */
  waves?: (hash: string) => WaveData | null;
}

type Gesture =
  | { kind: "press"; x0: number; time: number; track: PlacedTrack | null; transition: PlacedTransition | null; ruler: boolean }
  | { kind: "reorder"; track: PlacedTrack; time: number; dt: number; to: number }
  | { kind: "pan"; x0: number; start: number }
  | { kind: "arc"; levels: number[]; last: ArcPoint };

/**
 * The levels an energy curve is drawn between: the set's quietest and most intense tracks, as the arc's 0 and 1 are. Tracks
 * this alike have no such range to speak of, so their curves get room.
 */
export function energyRange(layout: TimelineLayout): [number, number] {
  const energies = layout.tracks.map((t) => t.order.energy).filter((e): e is number => e !== null);
  if (!energies.length) return [0, 1];
  const lo = Math.min(...energies);
  const hi = Math.max(...energies);
  return hi - lo >= MIN_ENERGY_RANGE ? [lo, hi] : [Math.max(0, lo - MIN_ENERGY_RANGE), Math.min(1, hi + MIN_ENERGY_RANGE)];
}

const reducedMotion = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const ease = (u: number) => 1 - (1 - u) ** 3;

export function SetTimeline({ layout, arc, onArc, drawing, reorderable, pinned, onReorder, open, onOpen, playback, waves }: SetTimelineProps) {
  useWaveLook();
  const boxRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const clockRef = useRef<HTMLSpanElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const hover = useRef<{ transition: number | null }>({ transition: null });
  const animation = useRef(0);
  const wavesRef = useRef<SetWaves | null>(null);

  // another order or a plan's arrival changes the length: the view stays where it was, unless it was the whole set
  const lastView = useRef<{ view: TimeView; whole: boolean } | null>(null);
  const ctl = useViewCtl(0, Math.max(layout.total, MIN_SPAN_S), MIN_SPAN_S, lastView.current && !lastView.current.whole ? lastView.current.view : undefined);

  const latest = useRef({ layout, arc, drawing, open, reorderable, pinned, onReorder, onOpen, onArc, waves });
  latest.current = { layout, arc, drawing, open, reorderable, pinned, onReorder, onOpen, onArc, waves };

  const draw = () => {
    const c = canvasRef.current;
    const width = widthRef.current;
    if (!c || !width) return;
    const s = setupCanvas(c, width, HEIGHT);
    if (!s) return;
    const view = ctl.get();
    const now = latest.current;
    const g = gesture.current;
    const scene: Scene = {
      layout: now.layout,
      wave: now.waves ?? ((hash) => wavesRef.current?.get(hash) ?? null),
      arc: g?.kind === "arc" ? toPoints(g.levels) : now.arc,
      energyRange: energyRange(now.layout),
      drag: g?.kind === "reorder" ? { position: g.track.position, dt: g.dt, to: g.to } : null,
      open: now.open,
      drawing: now.drawing,
      hover: hover.current,
    };
    paintTimeline(s.ctx, wavePalette(), scene, { start: view.start, pxPerSec: width / view.span, width }, s.dpr);
    placeHead();
  };

  /** The playhead and the clock where the playback says. */
  const placeHead = () => {
    const t = playback.time();
    const width = widthRef.current;
    const view = ctl.get();
    const head = headRef.current;
    if (head) {
      const x = ((t - view.start) / view.span) * width;
      head.style.display = x < -1 || x > width + 1 ? "none" : "";
      head.style.transform = `translateX(${Math.round(x)}px)`;
    }
    const clock = clockRef.current;
    if (clock) clock.textContent = `${fmtTime(t)} / ${fmtTime(latest.current.layout.total)}`;
  };

  const frame = useFrame(draw);
  const widthRef = useWidthRef(boxRef, draw);
  useLayoutEffect(() => draw());
  useEffect(() => {
    const un = ctl.subscribe(() => {
      lastView.current = { view: ctl.get(), whole: ctl.get().span >= ctl.bounds.max - ctl.bounds.min - 0.001 };
      frame();
    });
    return () => {
      un();
    };
  }, [ctl, frame]);

  // the waveforms come as they come
  useEffect(() => {
    if (waves) return;
    const store = new SetWaves(() => frame());
    wavesRef.current = store;
    return () => {
      store.close();
      wavesRef.current = null;
    };
  }, [waves, frame]);
  const hashes = useMemo(() => layout.tracks.map((t) => t.order.hash).join(" "), [layout]);
  useEffect(() => {
    wavesRef.current?.want(hashes ? hashes.split(" ") : []);
  }, [hashes, waves]);

  /** Glides to a view: the span in ratios, so going from the whole set to a few bars feels even all the way. */
  const glide = useCallback(
    (to: TimeView) => {
      cancelAnimationFrame(animation.current);
      if (reducedMotion()) {
        ctl.set(to);
        return;
      }
      const from = ctl.get();
      const fromCentre = from.start + from.span / 2;
      const toCentre = to.start + to.span / 2;
      const began = performance.now();
      const step = (now: number) => {
        const u = Math.min(1, (now - began) / ZOOM_MS);
        const k = ease(u);
        const span = from.span * (to.span / from.span) ** k;
        // the centre goes with how far the zoom has got, so what's being zoomed to stays in view on the way
        const share = from.span === to.span ? k : (from.span - span) / (from.span - to.span);
        ctl.set({ start: fromCentre + (toCentre - fromCentre) * share - span / 2, span });
        if (u < 1) animation.current = requestAnimationFrame(step);
      };
      animation.current = requestAnimationFrame(step);
    },
    [ctl],
  );
  useEffect(() => () => cancelAnimationFrame(animation.current), []);

  const fit = useCallback(() => glide({ start: 0, span: ctl.bounds.max }), [ctl, glide]);
  const zoomBy = useCallback(
    (factor: number) => {
      cancelAnimationFrame(animation.current);
      ctl.zoom(factor);
    },
    [ctl],
  );
  useWaveformKeys(true, zoomBy, fit);

  // opening a transition zooms to it; closing it goes back to the whole set
  const openTransition = open === null ? null : (layout.transitions[open] ?? null);
  const openRange = openTransition && `${openTransition.t0} ${openTransition.t1}`;
  const wasOpen = useRef(false);
  useEffect(() => {
    if (openRange) {
      const [t0, t1] = openRange.split(" ").map(Number);
      const span = Math.max(MIN_SPAN_S, (t1 - t0) * FIT_PAD);
      glide({ start: (t0 + t1) / 2 - span / 2, span });
      wasOpen.current = true;
    } else if (wasOpen.current) {
      wasOpen.current = false;
      fit();
    }
  }, [openRange, glide, fit]);

  // wheel: a native listener (passive: false) so ⌘/pinch zoom can preventDefault
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const r = el.getBoundingClientRect();
      const width = r.width || widthRef.current || 1;
      const view = ctl.get();
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        cancelAnimationFrame(animation.current);
        ctl.zoom(Math.exp(-e.deltaY * 0.012), view.start + ((e.clientX - r.left) / width) * view.span);
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
        e.preventDefault();
        cancelAnimationFrame(animation.current);
        ctl.scroll(((e.shiftKey ? e.deltaY : e.deltaX) / width) * view.span);
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [ctl, widthRef]);

  // the playhead follows the music, a page at a time, while it plays
  const subscribe = useCallback((cb: () => void) => playback.subscribe(cb), [playback]);
  const status = useSyncExternalStore(subscribe, () => playback.status);
  const playing = status === "playing" || status === "loading" || status === "waiting";
  useEffect(() => {
    placeHead();
    if (status !== "playing") return;
    let raf = 0;
    const loop = () => {
      if (mayDraw()) {
        if (!gesture.current) ctl.reveal(playback.time());
        placeHead();
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
    // placeHead reads only refs
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, playback, ctl]);

  const pointer = (e: ReactPointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const width = r.width || widthRef.current || 1;
    const view = ctl.get();
    const x = e.clientX - r.left;
    return { x, y: e.clientY - r.top, width, time: view.start + (x / width) * view.span };
  };

  const trackAt = (time: number, y: number): PlacedTrack | null => {
    const lane = y < laneTop(1) ? 0 : 1;
    if (y < LANES_TOP || y >= laneTop(1) + LANE_H) return null;
    return layout.tracks.find((t) => t.lane === lane && time >= t.start && time <= t.end) ?? null;
  };
  const transitionAt = (time: number, y: number): PlacedTransition | null => {
    if (y < LANES_TOP || y >= laneTop(1) + LANE_H) return null;
    return layout.transitions.find((t) => time >= t.t0 && time <= t.t1) ?? null;
  };
  const arcPointAt = (time: number, y: number): ArcPoint => {
    const h = 2 * LANE_H + LANE_GAP - 2 * ARC_PAD;
    return { t: arcTimeAt(layout, time), v: 1 - (y - LANES_TOP - ARC_PAD) / h };
  };

  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    cancelAnimationFrame(animation.current);
    const { x, y, time } = pointer(e);
    if (drawing && y >= LANES_TOP) {
      const last = arcPointAt(time, y);
      gesture.current = { kind: "arc", levels: paintStroke(toGrid(arc, ARC_GRID), null, last), last };
      frame();
      return;
    }
    gesture.current = { kind: "press", x0: x, time, track: trackAt(time, y), transition: transitionAt(time, y), ruler: y < CAMELOT_H + RULER_H };
  };

  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const { x, y, width, time } = pointer(e);
    let g = gesture.current;
    if (!g) {
      const over = transitionAt(time, y)?.position ?? null;
      if (over !== hover.current.transition) {
        hover.current = { transition: over };
        frame();
      }
      const box = e.currentTarget;
      const movable = reorderable && !drawing && !!trackAt(time, y);
      box.style.cursor = drawing ? "crosshair" : over !== null ? "pointer" : movable ? "grab" : "";
      return;
    }
    if (g.kind === "press") {
      if (Math.abs(x - g.x0) < DRAG_PX) return;
      g = g.track && reorderable && !pinned.has(g.track.position) ? { kind: "reorder", track: g.track, time: g.time, dt: 0, to: g.track.position } : { kind: "pan", x0: g.x0, start: ctl.get().start };
      gesture.current = g;
    }
    if (g.kind === "reorder") {
      // near an edge of a zoomed view, the set comes to you
      const view = ctl.get();
      if (x < EDGE_PX) ctl.scroll(-view.span * 0.01);
      else if (x > width - EDGE_PX) ctl.scroll(view.span * 0.01);
      g.dt = time - g.time;
      g.to = dropPosition(layout, g.track.position, (g.track.start + g.track.end) / 2 + g.dt);
      frame();
    } else if (g.kind === "pan") {
      const view = ctl.get();
      ctl.set({ ...view, start: g.start - ((x - g.x0) / width) * view.span });
    } else if (g.kind === "arc") {
      const to = arcPointAt(time, y);
      g.levels = paintStroke(g.levels, g.last, to);
      g.last = to;
      frame();
    }
  };

  const onUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const g = gesture.current;
    gesture.current = null;
    if (!g) return;
    if (g.kind === "press") {
      if (g.ruler || !g.transition) {
        playback.seek(pointer(e).time);
        placeHead();
      } else {
        onOpen(g.transition);
      }
    } else if (g.kind === "reorder") {
      const next = moveTrack(layout.tracks.map((t) => t.order), g.track.position, g.to, pinned);
      if (next) onReorder(next);
    } else if (g.kind === "arc") {
      onArc(toPoints(g.levels));
    }
    frame();
  };

  const onCancel = () => {
    gesture.current = null;
    frame();
  };

  const onLeave = () => {
    if (hover.current.transition !== null) {
      hover.current = { transition: null };
      frame();
    }
  };

  const canPlay = layout.transitions.length > 0;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" icon={playing ? Pause : Play} loading={status === "loading"} disabled={!canPlay} onClick={() => playback.toggle()}>
          {playing ? "Pause" : "Play"}
        </Button>
        <span ref={clockRef} className="num text-xs text-fg-muted" data-testid="set-clock" />
        {status === "waiting" && <span className="text-xs text-warn">Waiting for the next transition's plan</span>}
        {status === "error" && <span className="text-xs text-danger">{playback.error}</span>}
        <div className="ml-auto flex items-center gap-3">
          <div className="flex items-center gap-3 text-xs text-fg-subtle" aria-hidden>
            <span className="flex items-center gap-1">
              <span className="h-0.5 w-3 bg-fg/85" /> Energy
            </span>
            <span className="flex items-center gap-1">
              <span className="w-3 border-t-2 border-dashed border-accent" /> Target
            </span>
            <span className="flex items-center gap-1">
              <span className="h-0.5 w-3 bg-info" /> BPM
            </span>
          </div>
          <div className="flex items-center gap-0.5">
            <IconButton size="sm" icon={Minus} label="Zoom out" onClick={() => zoomBy(1 / 1.6)} />
            <IconButton size="sm" icon={Plus} label="Zoom in" onClick={() => zoomBy(1.6)} />
            <IconButton size="sm" icon={Maximize2} label="Whole set" onClick={fit} />
          </div>
        </div>
      </div>
      <div
        ref={boxRef}
        role="img"
        aria-label={`Whole-set timeline: ${layout.tracks.length} tracks. Drag a track to reorder it, click a transition to edit it.`}
        data-testid="set-timeline"
        data-bench="set-timeline"
        className="relative w-full touch-none select-none overflow-hidden rounded-sm border border-line bg-bg"
        style={{ height: HEIGHT }}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onCancel}
        onPointerLeave={onLeave}
      >
        <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" />
        <div ref={headRef} className="pointer-events-none absolute inset-y-0 left-0 bg-playhead" style={{ width: 1.5 }} />
      </div>
    </div>
  );
}
