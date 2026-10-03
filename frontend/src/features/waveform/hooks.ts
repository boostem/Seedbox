import { type RefObject, useCallback, useLayoutEffect, useRef, useState } from "react";

/** Width of an element (ResizeObserver), 0 until measured. */
export function useWidth(ref: RefObject<HTMLElement | null>): number {
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

export interface TimeView {
  start: number; // s at the left edge
  span: number; // s visible
}

export interface TimeViewApi {
  view: TimeView;
  set: (v: TimeView) => void;
  /** Zoom by `factor` (>1 = in) keeping time `anchor` under the same pixel. */
  zoom: (factor: number, anchor?: number) => void;
  /** Scroll by `ds` seconds. */
  scroll: (ds: number) => void;
  /** Make `t` visible (page so it sits at 15 % from the left) if it isn't. */
  reveal: (t: number) => void;
  fit: () => void;
  bounds: { min: number; max: number; minSpan: number };
}

/**
 * Zoom/scroll state over a time range [min, max]. Spans are clamped to [minSpan, max - min]; the view never
 * scrolls past either end.
 */
export function useTimeView(min: number, max: number, minSpan: number, initial?: TimeView): TimeViewApi {
  const clamp = useCallback(
    (v: TimeView): TimeView => {
      const full = Math.max(minSpan, max - min);
      const span = Math.max(Math.min(minSpan, full), Math.min(full, v.span));
      const start = Math.max(min, Math.min(max - span, v.start));
      return { start: Number.isFinite(start) ? start : min, span };
    },
    [min, max, minSpan],
  );
  const [view, setView] = useState<TimeView>(() => clamp(initial ?? { start: min, span: max - min }));
  const ref = useRef(view);
  ref.current = view;
  // re-clamp when the range changes (another track/analysis)
  const key = `${min}|${max}|${minSpan}`;
  const lastKey = useRef(key);
  if (lastKey.current !== key) {
    lastKey.current = key;
    const next = clamp(initial ?? { start: min, span: max - min });
    ref.current = next;
    setView(next);
  }
  const set = useCallback((v: TimeView) => {
    const next = clamp(v);
    ref.current = next;
    setView(next);
  }, [clamp]);
  const zoom = useCallback(
    (factor: number, anchor?: number) => {
      const v = ref.current;
      const a = anchor ?? v.start + v.span / 2;
      const span = v.span / factor;
      const frac = (a - v.start) / v.span;
      set({ start: a - frac * span, span });
    },
    [set],
  );
  const scroll = useCallback((ds: number) => set({ ...ref.current, start: ref.current.start + ds }), [set]);
  const reveal = useCallback(
    (t: number) => {
      const v = ref.current;
      if (t >= v.start && t <= v.start + v.span) return;
      set({ ...v, start: t - v.span * 0.15 });
    },
    [set],
  );
  const fit = useCallback(() => set({ start: min, span: max - min }), [set, min, max]);
  return { view, set, zoom, scroll, reveal, fit, bounds: { min, max, minSpan } };
}
