/**
 * Zoom/scroll state and canvas scheduling for the waveforms, without React renders: the view lives in a ref-like
 * controller that canvases subscribe to and redraw from on the next animation frame. React only hears about coarse
 * facts derived from it (`useViewFact`: "zoomed in?"), which change a few times per gesture, not per frame.
 * See PERFORMANCE.md.
 */
import { Maximize2, ZoomIn, ZoomOut } from "lucide-react";
import { type RefObject, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { registerCommands } from "@/lib/commands";
import { mayDraw } from "@/lib/frameCap";

export interface TimeView {
  start: number; // s at the left edge
  span: number; // s visible
}

export interface ViewCtl {
  get: () => TimeView;
  set: (v: TimeView) => void;
  /** Zoom by `factor` (>1 = in) keeping time `anchor` under the same pixel. */
  zoom: (factor: number, anchor?: number) => void;
  /** Scroll by `ds` seconds. */
  scroll: (ds: number) => void;
  /** Make `t` visible (page so it sits at 15 % from the left) if it isn't. */
  reveal: (t: number) => void;
  fit: () => void;
  /** Called after every change, synchronously (schedule the redraw with `useFrame`). */
  subscribe: (cb: () => void) => () => void;
  bounds: { min: number; max: number; minSpan: number };
  /** Let the view scroll past each end by this share of its span: 0.5 for a centred view, whose playhead reaches both. */
  setOverscroll: (share: number) => void;
}

export function clampView(v: TimeView, min: number, max: number, minSpan: number, overscroll = 0): TimeView {
  const full = Math.max(minSpan, max - min);
  const span = Math.max(Math.min(minSpan, full), Math.min(full, v.span));
  const over = span < full ? span * overscroll : 0; // the whole track always fits exactly
  const start = Math.max(min - over, Math.min(max - span + over, v.start));
  return { start: Number.isFinite(start) ? start : min, span };
}

/** A zoom/scroll controller over [min, max] (no React state). Spans are clamped to [minSpan, max - min]; the view
 * never scrolls past either end. A new range (another track) resets the view to `initial`, or the whole range. */
export function createViewCtl(min: number, max: number, minSpan: number, initial?: TimeView): ViewCtl {
  const subs = new Set<() => void>();
  let overscroll = 0;
  const clamp = (v: TimeView) => clampView(v, min, max, minSpan, overscroll);
  let view = clamp(initial ?? { start: min, span: max - min });
  const ctl: ViewCtl = {
    get: () => view,
    set(v) {
      const next = clamp(v);
      if (next.start === view.start && next.span === view.span) return;
      view = next;
      for (const cb of subs) cb();
    },
    zoom(factor, anchor) {
      const a = anchor ?? view.start + view.span / 2;
      const span = view.span / factor;
      const frac = (a - view.start) / view.span;
      ctl.set({ start: a - frac * span, span });
    },
    scroll: (ds) => ctl.set({ ...view, start: view.start + ds }),
    reveal(t) {
      if (t >= view.start && t <= view.start + view.span) return;
      ctl.set({ ...view, start: t - view.span * 0.15 });
    },
    fit: () => ctl.set({ start: min, span: max - min }),
    subscribe(cb) {
      subs.add(cb);
      return () => subs.delete(cb);
    },
    bounds: { min, max, minSpan },
    setOverscroll(share) {
      overscroll = share;
      ctl.set(view);
    },
  };
  return ctl;
}

/** `createViewCtl`, kept for the component's lifetime and recreated when the range changes. */
export function useViewCtl(min: number, max: number, minSpan: number, initial?: TimeView): ViewCtl {
  const init = useRef(initial);
  init.current = initial;
  return useMemo(() => createViewCtl(min, max, minSpan, init.current), [min, max, minSpan]);
}

/** A coarse fact about the view for React (a boolean or a rounded number): re-renders only when it changes. */
export function useViewFact<T extends string | number | boolean>(ctl: ViewCtl, fact: (v: TimeView) => T): T {
  const f = useRef(fact);
  f.current = fact;
  const get = useCallback(() => f.current(ctl.get()), [ctl]);
  return useSyncExternalStore(ctl.subscribe, get, get);
}

/** A stable function that runs `fn` once on the next animation frame it may draw in (`frameCap`), however often it's
 * called before that. */
export function useFrame(fn: () => void): () => void {
  const f = useRef(fn);
  f.current = fn;
  const raf = useRef(0);
  useEffect(() => () => cancelAnimationFrame(raf.current), []);
  return useCallback(() => {
    if (raf.current) return;
    const run = () => {
      if (!mayDraw()) {
        raf.current = requestAnimationFrame(run);
        return;
      }
      raf.current = 0;
      f.current();
    };
    raf.current = requestAnimationFrame(run);
  }, []);
}

/** Width of an element in a ref (ResizeObserver), 0 until measured. Calls `onResize` instead of re-rendering. */
export function useWidthRef(ref: RefObject<HTMLElement | null>, onResize: () => void): RefObject<number> {
  const width = useRef(0);
  const cb = useRef(onResize);
  cb.current = onResize;
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      if (el.clientWidth === width.current) return;
      width.current = el.clientWidth;
      cb.current();
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

/**
 * The waveform's keys (= / − zoom, 0 fit) as commands in the "View" group, so the palette and the shortcuts sheet
 * list them while a waveform with `hotkeys` is on screen.
 */
export function useWaveformKeys(enabled: boolean, zoom: (factor: number) => void, fit: () => void): void {
  const fns = useRef({ zoom, fit });
  fns.current = { zoom, fit };
  useEffect(() => {
    if (!enabled) return;
    return registerCommands([
      { id: "wave.zoomIn", title: "Zoom in", group: "View", icon: ZoomIn, combo: "=", altCombos: ["Shift+Equal"], keywords: "waveform", run: () => fns.current.zoom(1.6) },
      { id: "wave.zoomOut", title: "Zoom out", group: "View", icon: ZoomOut, combo: "-", keywords: "waveform", run: () => fns.current.zoom(1 / 1.6) },
      { id: "wave.fit", title: "Fit the waveform", group: "View", icon: Maximize2, combo: "0", keywords: "zoom reset whole", run: () => fns.current.fit() },
    ]);
  }, [enabled]);
}
