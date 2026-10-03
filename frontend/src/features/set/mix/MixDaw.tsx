/**
 * Mini-DAW on the MASTER beat timeline (beat 0 = B in): bar ruler with markers, master tempo lane, deck A/B
 * waveforms (positioned through each deck's beat grid, loops as repeated regions, fader shading), and per-deck
 * automation lanes + loop regions — all canvas, one canvas per row so a drag only repaints the rows it touches.
 *
 * Editing: drag keyframes (snap to beats; ⌥ = ¼-beat grid; ⇧ = fine, no snapping), double-click to add, ⌫ to
 * delete, right-click for the curve (linear / ease / hold), box-select on empty space (⇧ adds), drag a selection
 * across lanes, loops: drag to move, drag an edge to resize, double-click the loop row to add one.
 */
import { ChevronDown, ChevronRight, Info, Minus, Plus, Scan } from "lucide-react";
import { memo, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { create } from "zustand";
import type { Curve, DeckLoop, DeckPlan, Keyframe, LaneId, MixPlan, TrackAnalysis } from "@/api/types";
import { IconButton, Tooltip } from "@/components/ui";
import { setupCanvas, type View } from "@/features/waveform/draw";
import { useTimeView, useWidth } from "@/features/waveform/hooks";
import { alpha, wavePalette } from "@/features/waveform/palette";
import { prepareWave } from "@/features/waveform/prepare";
import { cn } from "@/lib/cn";
import { useHotkey } from "@/lib/hotkeys";
import { useSessionState } from "../useSessionState";
import { barLabel, CURVES, type DeckId, getLane, laneRange, LANES, type LaneKey, laneUsed, laneValue, metaOf, setLane } from "./lanes";
import { LANE_PAD, paintDeck, paintLane, paintLoops, paintMarkerLines, paintRuler, resolveColor, valueY, yValue } from "./paint";
import type { BeatClock } from "./useMixRender";

// ---- shared cursor state (hover beat, playhead beat, drag tooltip) — labels + overlay subscribe, rows don't
interface Cursor {
  hover: number | null;
  play: number | null;
  tip: { x: number; y: number; text: string } | null;
}
const useCursor = create<Cursor>()(() => ({ hover: null, play: null, tip: null }));

type Sel = Map<LaneKey, Set<number>>;

export interface MixDawProps {
  plan: MixPlan;
  update: (p: MixPlan, opts?: { transient?: boolean }) => void;
  checkpoint: (snapshot: MixPlan) => void;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
  anA: TrackAnalysis | null;
  anB: TrackAnalysis | null;
  nameA: string;
  nameB: string;
  clock: BeatClock;
  onPlayFrom: (beat: number) => void;
  hotkeys?: boolean;
}

const GUTTER = 132;
const H_RULER = 30;
const H_TEMPO = 44;
const H_WAVE = 60;
const H_LANE = 30;
const H_LOOPS = 20;
/** Per-session "show unused lanes" state: deck A, deck B, master tempo lane. */
const SHOW_KEY = "seedbox.mix.showUnused";
type ShowAll = Record<DeckId | "tempo", boolean>;
const SHOW_DEFAULT: ShowAll = { a: false, b: false, tempo: false };

/** Everything the rows need to edit, behind stable refs (rows stay memoised during drags). */
interface Editor {
  plan: () => MixPlan;
  sel: () => Sel;
  setSel: (s: Sel) => void;
  update: (p: MixPlan, opts?: { transient?: boolean }) => void;
  checkpoint: (p: MixPlan) => void;
  view: () => View;
  loopSel: () => { deck: DeckId; index: number } | null;
  setLoopSel: (s: { deck: DeckId; index: number } | null) => void;
  menu: (m: Menu | null) => void;
  playFrom: (beat: number) => void;
}

type Menu = { x: number; y: number; kind: "kf"; key: LaneKey; index: number } | { x: number; y: number; kind: "loop"; deck: DeckId; index: number };

const snapTo = (v: number, step: number) => Math.round(v / step) * step;
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

function hitKeyframe(kfs: Keyframe[], view: View, range: [number, number], h: number, x: number, y: number): number | null {
  let best: number | null = null;
  let bestD = 8;
  kfs.forEach((k, i) => {
    const d = Math.hypot((k.beat - view.start) * view.pxPerSec - x, valueY(k.value, range, h) - y);
    if (d < bestD) [best, bestD] = [i, d];
  });
  return best;
}

/** Move the selected keyframes (from a drag-start snapshot) — re-sorts lanes and remaps the selection. */
function moveSelection(snap: MixPlan, sel: Sel, dBeat: number, dNorm: number, grid: number, free: boolean): { plan: MixPlan; sel: Sel } {
  let plan = snap;
  const next: Sel = new Map();
  const lo = snap.window.start_beat - 32;
  const hi = snap.window.end_beat + 32;
  for (const [key, idx] of sel) {
    const kfs = getLane(snap, key);
    const range = laneRange(key, snap);
    const meta = metaOf(key);
    const entries = kfs.map((k, i) => {
      if (!idx.has(i)) return { k, s: false };
      const beat = clamp(free ? k.beat + dBeat : snapTo(k.beat + dBeat, grid), lo, hi);
      const raw = k.value + dNorm * (range[1] - range[0]);
      const value = clamp(free ? raw : snapTo(raw, meta.step), range[0], range[1]);
      return { k: { ...k, beat: Math.round(beat * 1000) / 1000, value: Math.round(value * 1000) / 1000 }, s: true };
    });
    entries.sort((a, b) => a.k.beat - b.k.beat);
    plan = setLane(plan, key, entries.map((e) => e.k));
    next.set(key, new Set(entries.flatMap((e, i) => (e.s ? [i] : []))));
  }
  return { plan, sel: next };
}

// ------------------------------------------------------------------------------------------------ rows

function useRowCanvas(width: number, height: number, paint: (ctx: CanvasRenderingContext2D, dpr: number) => void, deps: unknown[]) {
  const ref = useRef<HTMLCanvasElement>(null);
  useLayoutEffect(() => {
    const c = ref.current;
    if (!c || !width) return;
    const s = setupCanvas(c, width, height);
    if (s) paint(s.ctx, s.dpr);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, height, ...deps]);
  return ref;
}

const LaneLabel = memo(function LaneLabel({ k, kfs, height, deck, dim }: { k: LaneKey; kfs: Keyframe[]; height: number; deck?: DeckId; dim?: boolean }) {
  const meta = metaOf(k);
  const beat = useCursor((s) => s.hover ?? s.play);
  const v = laneValue(kfs, beat ?? kfs[0]?.beat ?? 0);
  return (
    <div style={{ height }} className={cn("flex items-center gap-1.5 border-b border-line/60 pr-2 pl-3", dim && "opacity-55")}>
      {deck && <span className="w-2 text-2xs font-bold text-fg-faint">{deck.toUpperCase()}</span>}
      <span className="size-1.5 shrink-0 rounded-full" style={{ background: meta.color }} />
      <span className="min-w-0 flex-1 truncate text-xs text-fg-muted">{meta.label}</span>
      <span className={cn("text-2xs num", beat != null ? "text-fg" : "text-fg-subtle")}>{meta.fmt(v)}</span>
    </div>
  );
});

interface LaneRowProps {
  k: LaneKey;
  kfs: Keyframe[];
  range: [number, number];
  view: View;
  width: number;
  height: number;
  selected: Set<number> | undefined;
  play?: [number, number];
  ed: Editor;
}

const LaneCanvas = memo(function LaneCanvas({ k, kfs, range, view, width, height, selected, play, ed }: LaneRowProps) {
  const meta = metaOf(k);
  const [box, setBox] = useState<{ x0: number; x1: number; y0: number; y1: number } | null>(null);
  const ref = useRowCanvas(width, height, (ctx) => paintLane(ctx, view, width, height, { kfs, range, color: meta.color, bipolar: meta.bipolar, neutral: k === "tempo" ? undefined : meta.neutral, selected, play, box }), [kfs, range, view, selected, play, box, meta]);
  const drag = useRef<
    | { kind: "move"; x: number; y: number; snap: MixPlan; sel: Sel; moved: boolean; toggle: number | null }
    | { kind: "box"; x0: number; y0: number; base: Sel }
    | null
  >(null);

  const local = (e: { clientX: number; clientY: number }) => {
    const r = ref.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const tipFor = (key: LaneKey, kf: Keyframe, e: { clientX: number; clientY: number }) =>
    useCursor.setState({ tip: { x: e.clientX, y: e.clientY, text: `${metaOf(key).fmt(kf.value)} · bar ${barLabel(kf.beat)}${kf.beat % 1 ? ` +${(kf.beat % 1).toFixed(2)}` : ""}` } });

  const onDown = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    if (e.button !== 0) return;
    const { x, y } = local(e);
    const plan = ed.plan();
    const lane = getLane(plan, k);
    const i = hitKeyframe(lane, view, range, height, x, y);
    e.currentTarget.setPointerCapture(e.pointerId);
    ed.setLoopSel(null);
    let sel = ed.sel();
    if (i != null) {
      const isSel = !!sel.get(k)?.has(i);
      if (e.shiftKey) {
        if (!isSel) {
          sel = new Map(sel);
          sel.set(k, new Set([...(sel.get(k) ?? []), i]));
        }
      } else if (!isSel) sel = new Map([[k, new Set([i])]]);
      ed.setSel(sel);
      drag.current = { kind: "move", x: e.clientX, y: e.clientY, snap: plan, sel, moved: false, toggle: e.shiftKey && isSel ? i : null };
      tipFor(k, lane[i], e);
    } else {
      const base = e.shiftKey ? sel : new Map<LaneKey, Set<number>>();
      if (!e.shiftKey) ed.setSel(base);
      drag.current = { kind: "box", x0: x, y0: y, base };
    }
  };

  const onMove = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    const { x, y } = local(e);
    useCursor.setState({ hover: view.start + x / view.pxPerSec });
    const d = drag.current;
    if (!d) return;
    if (d.kind === "move") {
      const dx = e.clientX - d.x;
      const dy = e.clientY - d.y;
      if (!d.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
      d.moved = true;
      const fine = e.shiftKey;
      const f = fine ? 0.25 : 1;
      const res = moveSelection(d.snap, d.sel, (dx / view.pxPerSec) * f, (-dy / Math.max(1, height - 2 * LANE_PAD)) * f, e.altKey ? 0.25 : 1, fine);
      ed.update(res.plan, { transient: true });
      ed.setSel(res.sel);
      const idx = [...(res.sel.get(k) ?? [])][0];
      const lane = getLane(res.plan, k);
      if (idx != null && lane[idx]) tipFor(k, lane[idx], e);
    } else {
      const b = { x0: d.x0, y0: d.y0, x1: x, y1: y };
      setBox(b);
      const lane = getLane(ed.plan(), k);
      const [bx0, bx1] = [Math.min(b.x0, b.x1), Math.max(b.x0, b.x1)];
      const [by0, by1] = [Math.min(b.y0, b.y1), Math.max(b.y0, b.y1)];
      const hits = new Set(d.base.get(k) ?? []);
      lane.forEach((kf, i) => {
        const kx = (kf.beat - view.start) * view.pxPerSec;
        const ky = valueY(kf.value, range, height);
        if (kx >= bx0 && kx <= bx1 && ky >= by0 && ky <= by1) hits.add(i);
      });
      const sel = new Map(d.base);
      if (hits.size) sel.set(k, hits);
      else sel.delete(k);
      ed.setSel(sel);
    }
  };

  const onUp = () => {
    const d = drag.current;
    drag.current = null;
    useCursor.setState({ tip: null });
    if (!d) return;
    if (d.kind === "move") {
      if (d.moved) ed.checkpoint(d.snap);
      else if (d.toggle != null) {
        const sel = new Map(ed.sel());
        const s = new Set(sel.get(k) ?? []);
        s.delete(d.toggle);
        if (s.size) sel.set(k, s);
        else sel.delete(k);
        ed.setSel(sel);
      }
    } else setBox(null);
  };

  const onDouble = (e: ReactMouseEvent<HTMLCanvasElement>) => {
    const { x, y } = local(e);
    const plan = ed.plan();
    const lane = getLane(plan, k);
    if (hitKeyframe(lane, view, range, height, x, y) != null) return;
    const grid = e.altKey ? 0.25 : 1;
    const beat = e.shiftKey ? view.start + x / view.pxPerSec : snapTo(view.start + x / view.pxPerSec, grid);
    const value = clamp(e.shiftKey ? yValue(y, range, height) : snapTo(yValue(y, range, height), meta.step), range[0], range[1]);
    const prev = [...lane].reverse().find((kf) => kf.beat <= beat);
    const kf: Keyframe = { beat: Math.round(beat * 1000) / 1000, value: Math.round(value * 1000) / 1000, curve: prev?.curve ?? "linear" };
    const next = [...lane, kf].sort((a, b) => a.beat - b.beat);
    ed.update(setLane(plan, k, next));
    ed.setSel(new Map([[k, new Set([next.indexOf(kf)])]]));
  };

  const onContext = (e: ReactMouseEvent<HTMLCanvasElement>) => {
    e.preventDefault();
    const { x, y } = local(e);
    const i = hitKeyframe(getLane(ed.plan(), k), view, range, height, x, y);
    if (i == null) return;
    if (!ed.sel().get(k)?.has(i)) ed.setSel(new Map([[k, new Set([i])]]));
    ed.menu({ x: e.clientX, y: e.clientY, kind: "kf", key: k, index: i });
  };

  return (
    <canvas
      ref={ref}
      style={{ height }}
      className="block w-full cursor-crosshair touch-none"
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      onPointerLeave={() => !drag.current && useCursor.setState({ hover: null })}
      onDoubleClick={onDouble}
      onContextMenu={onContext}
    />
  );
});

const DeckCanvas = memo(function DeckCanvas({ deck, an, view, width, tint }: { deck: DeckPlan; an: TrackAnalysis | null; view: View; width: number; tint: string }) {
  const wave = useMemo(() => (an ? prepareWave(an) : null), [an]);
  const ref = useRowCanvas(width, H_WAVE, (ctx, dpr) => paintDeck(ctx, dpr, view, width, H_WAVE, deck, wave, resolveColor(tint)), [deck, wave, view, tint]);
  return <canvas ref={ref} style={{ height: H_WAVE }} className="block w-full" onPointerMove={(e) => useCursor.setState({ hover: view.start + (e.clientX - e.currentTarget.getBoundingClientRect().left) / view.pxPerSec })} onPointerLeave={() => useCursor.setState({ hover: null })} />;
});

const LoopsCanvas = memo(function LoopsCanvas({ d, deck, view, width, selected, ed }: { d: DeckId; deck: DeckPlan; view: View; width: number; selected: number | null; ed: Editor }) {
  const ref = useRowCanvas(width, H_LOOPS, (ctx) => paintLoops(ctx, view, width, H_LOOPS, deck, selected), [deck, view, selected]);
  const drag = useRef<{ mode: "move" | "start" | "end"; index: number; x: number; snap: MixPlan; orig: DeckLoop; moved: boolean } | null>(null);
  const hit = (x: number): { index: number; mode: "move" | "start" | "end" } | null => {
    for (let i = deck.loops.length - 1; i >= 0; i--) {
      const l = deck.loops[i];
      const x0 = (l.start_beat - view.start) * view.pxPerSec;
      const x1 = (l.end_beat - view.start) * view.pxPerSec;
      if (x >= x0 - 4 && x <= x1 + 4) return { index: i, mode: Math.abs(x - x0) <= 5 ? "start" : Math.abs(x - x1) <= 5 ? "end" : "move" };
    }
    return null;
  };
  const lx = (e: { clientX: number }) => e.clientX - ref.current!.getBoundingClientRect().left;
  const setLoops = (plan: MixPlan, loops: DeckLoop[]): MixPlan => ({ ...plan, [d]: { ...plan[d], loops } });
  return (
    <canvas
      ref={ref}
      style={{ height: H_LOOPS }}
      className="block w-full touch-none"
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        const h = hit(lx(e));
        ed.setSel(new Map());
        if (!h) return ed.setLoopSel(null);
        e.currentTarget.setPointerCapture(e.pointerId);
        ed.setLoopSel({ deck: d, index: h.index });
        drag.current = { mode: h.mode, index: h.index, x: e.clientX, snap: ed.plan(), orig: deck.loops[h.index], moved: false };
      }}
      onPointerMove={(e) => {
        const x = lx(e);
        useCursor.setState({ hover: view.start + x / view.pxPerSec });
        const h = hit(x);
        e.currentTarget.style.cursor = drag.current ? (drag.current.mode === "move" ? "grabbing" : "ew-resize") : h ? (h.mode === "move" ? "grab" : "ew-resize") : "default";
        const dr = drag.current;
        if (!dr) return;
        const raw = (e.clientX - dr.x) / view.pxPerSec;
        if (!dr.moved && Math.abs(e.clientX - dr.x) < 3) return;
        dr.moved = true;
        const grid = e.altKey ? 0.25 : 1;
        const o = dr.orig;
        let l: DeckLoop;
        if (dr.mode === "move") {
          const db = snapTo(o.start_beat + raw, grid) - o.start_beat;
          l = { ...o, start_beat: o.start_beat + db, end_beat: o.end_beat + db, source_beat: o.source_beat + db };
        } else if (dr.mode === "end") l = { ...o, end_beat: Math.max(o.start_beat + 0.25, snapTo(o.end_beat + raw, grid)) };
        else l = { ...o, start_beat: Math.min(o.end_beat - 0.25, snapTo(o.start_beat + raw, grid)) };
        const loops = dr.snap[d].loops.map((x, i) => (i === dr.index ? l : x));
        ed.update(setLoops(dr.snap, loops), { transient: true });
        useCursor.setState({ tip: { x: e.clientX, y: e.clientY, text: `loop ${barLabel(l.start_beat)} → ${barLabel(l.end_beat)} · ${l.length_beats} beat${l.length_beats === 1 ? "" : "s"}` } });
      }}
      onPointerUp={() => {
        const dr = drag.current;
        drag.current = null;
        useCursor.setState({ tip: null });
        if (dr?.moved) ed.checkpoint(dr.snap);
      }}
      onPointerLeave={() => !drag.current && useCursor.setState({ hover: null })}
      onDoubleClick={(e) => {
        if (hit(lx(e))) return;
        const start = snapTo(view.start + lx(e) / view.pxPerSec, e.altKey ? 0.25 : 1);
        const plan = ed.plan();
        const l: DeckLoop = { start_beat: start, end_beat: start + 4, source_beat: start, length_beats: 4 };
        const loops = [...plan[d].loops, l].sort((a, b) => a.start_beat - b.start_beat);
        ed.update(setLoops(plan, loops));
        ed.setLoopSel({ deck: d, index: loops.indexOf(l) });
      }}
      onContextMenu={(e) => {
        e.preventDefault();
        const h = hit(lx(e));
        if (!h) return;
        ed.setLoopSel({ deck: d, index: h.index });
        ed.menu({ x: e.clientX, y: e.clientY, kind: "loop", deck: d, index: h.index });
      }}
    />
  );
});

const RulerCanvas = memo(function RulerCanvas({ plan, view, width, ed }: { plan: MixPlan; view: View; width: number; ed: Editor }) {
  const ref = useRowCanvas(width, H_RULER, (ctx) => paintRuler(ctx, view, width, H_RULER, plan), [plan.window, plan.markers, plan.a.play_to_beat, view]);
  return (
    <canvas
      ref={ref}
      style={{ height: H_RULER }}
      className="block w-full cursor-pointer"
      title="Click to play from here"
      onPointerMove={(e) => useCursor.setState({ hover: view.start + (e.clientX - e.currentTarget.getBoundingClientRect().left) / view.pxPerSec })}
      onPointerLeave={() => useCursor.setState({ hover: null })}
      onClick={(e) => ed.playFrom(snapTo(view.start + (e.clientX - e.currentTarget.getBoundingClientRect().left) / view.pxPerSec, 1))}
    />
  );
});

/** The single subtle "+ N more lanes" row (expands / collapses the unused lanes). */
function MoreRow({ open, onClick, label, title }: { open: boolean; onClick: () => void; label: string; title?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-expanded={open}
      style={{ gridColumn: "1 / span 2", height: 20 }}
      className="flex items-center gap-1 pl-3 text-left text-2xs text-fg-faint transition-colors hover:bg-raised/50 hover:text-fg-muted"
    >
      {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
      {label}
    </button>
  );
}

// ------------------------------------------------------------------------------------------------ DAW

const DECK_TINT: Record<DeckId, string> = { a: "var(--color-fg-muted)", b: "var(--color-accent)" };

export function MixDaw({ plan, update, checkpoint, undo, redo, canUndo, canRedo, anA, anB, nameA, nameB, clock, onPlayFrom, hotkeys = true }: MixDawProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const width = useWidth(boxRef);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const rowsRef = useRef<HTMLDivElement>(null);
  const min = plan.window.start_beat - 16;
  const max = plan.window.end_beat + 16;
  const tv = useTimeView(min, max, 8, { start: plan.window.start_beat - 2, span: plan.window.end_beat - plan.window.start_beat + 4 });
  const tvRef = useRef(tv);
  tvRef.current = tv;
  const view: View = useMemo(() => ({ start: tv.view.start, pxPerSec: width / tv.view.span || 1, width }), [tv.view, width]);
  const [showAll, setShowAll] = useSessionState<ShowAll>(SHOW_KEY, SHOW_DEFAULT);
  const [sel, setSel] = useState<Sel>(() => new Map());
  const [loopSel, setLoopSel] = useState<{ deck: DeckId; index: number } | null>(null);
  const [menu, setMenu] = useState<Menu | null>(null);

  const refs = useRef({ plan, sel, view, loopSel });
  refs.current = { plan, sel, view, loopSel };
  const ed = useMemo<Editor>(
    () => ({
      plan: () => refs.current.plan,
      sel: () => refs.current.sel,
      setSel: (s) => {
        refs.current.sel = s;
        setSel(s);
      },
      update,
      checkpoint,
      view: () => refs.current.view,
      loopSel: () => refs.current.loopSel,
      setLoopSel: (s) => {
        refs.current.loopSel = s;
        setLoopSel(s);
      },
      menu: setMenu,
      playFrom: onPlayFrom,
    }),
    [update, checkpoint, onPlayFrom],
  );

  // a different plan shape (regenerated) → drop selections that no longer exist
  useEffect(() => {
    setSel((s) => {
      let changed = false;
      const next: Sel = new Map();
      for (const [k, idx] of s) {
        const n = getLane(plan, k).length;
        const keep = new Set([...idx].filter((i) => i < n));
        if (keep.size !== idx.size) changed = true;
        if (keep.size) next.set(k, keep);
      }
      return changed ? next : s;
    });
  }, [plan]);

  const toggleShow = (k: keyof ShowAll) => setShowAll((v) => ({ ...SHOW_DEFAULT, ...v, [k]: !v[k] }));

  const deleteSelection = useCallback(() => {
    const p = refs.current.plan;
    const ls = refs.current.loopSel;
    if (ls) {
      update({ ...p, [ls.deck]: { ...p[ls.deck], loops: p[ls.deck].loops.filter((_, i) => i !== ls.index) } });
      ed.setLoopSel(null);
      return true;
    }
    const s = refs.current.sel;
    if (!s.size) return false;
    let next = p;
    for (const [k, idx] of s) {
      const lane = getLane(p, k);
      let kept = lane.filter((_, i) => !idx.has(i));
      if (!kept.length) kept = [lane[0]];
      next = setLane(next, k, kept);
    }
    update(next);
    ed.setSel(new Map());
    return true;
  }, [update, ed]);

  const setCurve = (curve: Curve) => {
    const p = refs.current.plan;
    let next = p;
    for (const [k, idx] of refs.current.sel) next = setLane(next, k, getLane(next, k).map((kf, i) => (idx.has(i) ? { ...kf, curve } : kf)));
    update(next);
    setMenu(null);
  };
  const setLoopLength = (len: number) => {
    if (menu?.kind !== "loop") return;
    const p = refs.current.plan;
    const d = menu.deck;
    update({ ...p, [d]: { ...p[d], loops: p[d].loops.map((l, i) => (i === menu.index ? { ...l, length_beats: len } : l)) } });
    setMenu(null);
  };

  useHotkey(["Delete", "Backspace"], () => deleteSelection(), { enabled: hotkeys });
  useHotkey("Escape", () => {
    if (!menu && !refs.current.sel.size && !refs.current.loopSel) return false;
    setMenu(null);
    ed.setSel(new Map());
    ed.setLoopSel(null);
  }, { enabled: hotkeys });
  useHotkey("Mod+z", () => (canUndo ? undo() : false), { enabled: hotkeys });
  useHotkey("Mod+Shift+z", () => (canRedo ? redo() : false), { enabled: hotkeys });
  useHotkey(["=", "Shift+Equal"], () => tv.zoom(1.5), { enabled: hotkeys });
  useHotkey("-", () => tv.zoom(1 / 1.5), { enabled: hotkeys });
  useHotkey("0", () => tv.set({ start: plan.window.start_beat - 2, span: plan.window.end_beat - plan.window.start_beat + 4 }), { enabled: hotkeys });

  // wheel: ⌘/pinch zoom, horizontal scroll
  useEffect(() => {
    const el = boxRef.current?.parentElement;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const t = tvRef.current;
      const r = boxRef.current!.getBoundingClientRect();
      const beat = t.view.start + ((e.clientX - r.left) / r.width) * t.view.span;
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        t.zoom(Math.exp(-e.deltaY * 0.012), beat);
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
        e.preventDefault();
        const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        t.scroll((d / r.width) * t.view.span);
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // overlay: marker lines, hover line, playhead (store subscriptions, no React renders)
  const paintOverlay = useCallback(() => {
    const c = overlayRef.current;
    const rows = rowsRef.current;
    if (!c || !rows || !width) return;
    const h = rows.clientHeight;
    const s = setupCanvas(c, width, h);
    if (!s) return;
    const { ctx } = s;
    const p = wavePalette();
    paintMarkerLines(ctx, view, h, plan.markers);
    const { hover } = useCursor.getState();
    if (hover != null) {
      const x = Math.round((hover - view.start) * view.pxPerSec) + 0.5;
      ctx.fillStyle = alpha(p.fg, 0.3);
      ctx.fillRect(x - 0.5, 0, 1, h);
    }
    const beat = clock.get();
    if (beat != null) {
      const x = Math.round((beat - view.start) * view.pxPerSec) + 0.5;
      if (x >= 0 && x <= width) {
        ctx.fillStyle = alpha(p.bg, 0.35);
        ctx.fillRect(x - 1.5, 0, 3, h);
        ctx.fillStyle = p.playhead;
        ctx.fillRect(x - 0.5, 0, 1.5, h);
        ctx.beginPath();
        ctx.moveTo(x - 5, 0);
        ctx.lineTo(x + 5.5, 0);
        ctx.lineTo(x + 0.25, 6);
        ctx.closePath();
        ctx.fill();
      }
    }
  }, [width, view, plan.markers, clock]);

  useLayoutEffect(() => paintOverlay());
  useEffect(() => {
    let raf = 0;
    const schedule = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        paintOverlay();
      });
    };
    const un1 = useCursor.subscribe(schedule);
    const un2 = clock.subscribe(() => {
      const b = clock.get();
      if (useCursor.getState().play !== b) useCursor.setState({ play: b });
      // follow the playhead while it plays
      const t = tvRef.current;
      if (b != null && (b < t.view.start || b > t.view.start + t.view.span)) t.set({ ...t.view, start: b - t.view.span * 0.1 });
      schedule();
    });
    return () => {
      un1();
      un2();
      cancelAnimationFrame(raf);
    };
  }, [paintOverlay, clock]);

  const tip = useCursor((s) => s.tip);
  const tempoRange = useMemo(() => laneRange("tempo", plan), [plan]);
  const ranges = useMemo(() => Object.fromEntries(LANES.map((m) => [m.id, laneRange(`a.${m.id as LaneId}`, plan)])) as Record<LaneId, [number, number]>, [plan]);

  const deckRows = (d: DeckId) => {
    const deck = plan[d];
    const an = d === "a" ? anA : anB;
    const keyOf = (id: string) => `${d}.${id}` as LaneKey;
    const isUsed = (id: string) => laneUsed(keyOf(id), deck.lanes[id as LaneId]) || sel.has(keyOf(id));
    const unused = LANES.filter((m) => !isUsed(m.id));
    const open = !!showAll[d];
    const hasLoops = deck.loops.length > 0 || loopSel?.deck === d;
    const hidden = unused.length + (hasLoops ? 0 : 1); // lanes behind the "+ N more" row (the empty loops row counts)
    const lanes = open ? LANES : LANES.filter((m) => !unused.includes(m));
    const name = d === "a" ? nameA : nameB;
    const details = `${name} · ${deck.native_bpm.toFixed(1)} BPM · plays bar ${barLabel(deck.play_from_beat)} → ${barLabel(deck.play_to_beat)} · echo ${deck.echo_beats} beat${deck.loops.length ? ` · ${deck.loops.length} loop${deck.loops.length === 1 ? "" : "s"}` : ""}`;
    return (
      <>
        <div className="flex h-6 items-center bg-panel pl-3" style={{ gridColumn: "1 / span 2" }}>
          <Tooltip content={details} side="bottom" className="min-w-0">
            <span className="flex min-w-0 items-center gap-2">
              <span className={cn("text-xs font-bold", d === "a" ? "text-fg-muted" : "text-accent-hover")}>{d.toUpperCase()}</span>
              <span className="min-w-0 truncate text-xs text-fg-muted">{name}</span>
            </span>
          </Tooltip>
        </div>
        <div style={{ height: H_WAVE }} className="border-b border-line/60" />
        <DeckCanvas deck={deck} an={an} view={view} width={width} tint={DECK_TINT[d]} />
        {lanes.map((m) => {
          const k = keyOf(m.id);
          const dim = !isUsed(m.id);
          return [
            <LaneLabel key={`${k}-l`} k={k} kfs={deck.lanes[m.id as LaneId]} height={H_LANE} dim={dim} />,
            <div key={`${k}-c`} className="border-b border-line/60">
              <LaneCanvas k={k} kfs={deck.lanes[m.id as LaneId]} range={ranges[m.id as LaneId]} view={view} width={width} height={H_LANE - 1} selected={sel.get(k)} play={[deck.play_from_beat, deck.play_to_beat]} ed={ed} />
            </div>,
          ];
        })}
        {(open || hasLoops) && (
          <>
            <div style={{ height: H_LOOPS }} className="flex items-center gap-1.5 border-b border-line/60 pr-2 pl-3 text-xs text-fg-muted">
              <span className="size-1.5 rounded-full bg-info" /> Loops
            </div>
            <div className="border-b border-line/60">
              <LoopsCanvas d={d} deck={deck} view={view} width={width} selected={loopSel?.deck === d ? loopSel.index : null} ed={ed} />
            </div>
          </>
        )}
        {hidden > 0 && (
          <MoreRow
            open={open}
            onClick={() => toggleShow(d)}
            title={[...unused.map((m) => m.label), ...(hasLoops ? [] : ["Loops"])].join(", ")}
            label={open ? `Hide ${hidden} unused lane${hidden === 1 ? "" : "s"}` : `+ ${hidden} more lane${hidden === 1 ? "" : "s"}`}
          />
        )}
      </>
    );
  };

  const tempoUsed = laneUsed("tempo", plan.tempo) || sel.has("tempo");
  const tempoOpen = tempoUsed || !!showAll.tempo;

  return (
    <div className="flex flex-col">
      <div className="mb-1.5 flex h-6 items-center gap-2">
        <span className="label-caps">Automation</span>
        <Tooltip content="drag keys · dbl-click add · ⌫ delete · right-click curve · ⌥ ¼-beat · ⇧ fine · ⌘-scroll zoom" side="bottom">
          <Info size={12} className="text-fg-faint" aria-label="Editing shortcuts" />
        </Tooltip>
        <span className="flex-1" />
        <span className="shrink-0 text-2xs whitespace-nowrap text-fg-subtle num">{Math.round(tv.view.span / 4)} bars visible</span>
        <IconButton icon={Minus} label="Zoom out (−)" size="sm" onClick={() => tv.zoom(1 / 1.5)} />
        <IconButton icon={Plus} label="Zoom in (=)" size="sm" onClick={() => tv.zoom(1.5)} />
        <IconButton icon={Scan} label="Fit the render window (0)" size="sm" onClick={() => tv.set({ start: plan.window.start_beat - 2, span: plan.window.end_beat - plan.window.start_beat + 4 })} />
      </div>
      <div className="relative overflow-hidden rounded-sm border border-line bg-bg">
        <div ref={rowsRef} className="grid" style={{ gridTemplateColumns: `${GUTTER}px minmax(0, 1fr)` }}>
          {/* ruler */}
          <div style={{ height: H_RULER }} title="Bars counted from B coming in (bar 1.1)" className="flex items-center border-b border-line bg-panel pl-3 text-2xs font-semibold tracking-wide text-fg-subtle uppercase">
            Bars
          </div>
          <div ref={boxRef} className="border-b border-line">
            <RulerCanvas plan={plan} view={view} width={width} ed={ed} />
          </div>
          {/* master tempo */}
          {tempoOpen && (
            <>
              <LaneLabel k="tempo" kfs={plan.tempo} height={H_TEMPO} dim={!tempoUsed} />
              <div className="border-b border-line/60">
                <LaneCanvas k="tempo" kfs={plan.tempo} range={tempoRange} view={view} width={width} height={H_TEMPO - 1} selected={sel.get("tempo")} ed={ed} />
              </div>
            </>
          )}
          {!tempoUsed && (
            <MoreRow
              open={tempoOpen}
              onClick={() => toggleShow("tempo")}
              title={`Master tempo is constant at ${plan.tempo[0]?.value.toFixed(1)} BPM`}
              label={tempoOpen ? "Hide tempo lane" : "+ 1 more lane"}
            />
          )}
          {deckRows("a")}
          {deckRows("b")}
        </div>
        <canvas ref={overlayRef} className="pointer-events-none absolute top-0 bottom-0" style={{ left: GUTTER + 1, width: width || 0 }} />
      </div>
      {tip &&
        createPortal(
          <div className="pointer-events-none fixed z-[1000] rounded-xs bg-overlay px-1.5 py-0.5 text-2xs whitespace-nowrap text-fg shadow-pop num" style={{ left: tip.x + 12, top: tip.y - 26 }}>
            {tip.text}
          </div>,
          document.body,
        )}
      {menu && <ContextMenu menu={menu} plan={plan} onClose={() => setMenu(null)} onCurve={setCurve} onLength={setLoopLength} onDelete={() => (deleteSelection(), setMenu(null))} />}
    </div>
  );
}

function ContextMenu({ menu, plan, onClose, onCurve, onLength, onDelete }: { menu: Menu; plan: MixPlan; onClose: () => void; onCurve: (c: Curve) => void; onLength: (n: number) => void; onDelete: () => void }) {
  useEffect(() => {
    const close = () => onClose();
    window.addEventListener("pointerdown", close);
    window.addEventListener("blur", close);
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("blur", close);
    };
  }, [onClose]);
  const current = menu.kind === "kf" ? getLane(plan, menu.key)[menu.index]?.curve : null;
  const loopLen = menu.kind === "loop" ? plan[menu.deck].loops[menu.index]?.length_beats : null;
  const item = "flex h-7 w-full items-center gap-2 rounded-xs px-2 text-left text-sm hover:bg-raised";
  return createPortal(
    <div className="fixed z-[1000] w-44 rounded-sm bg-overlay p-1 shadow-pop" style={{ left: Math.min(menu.x, window.innerWidth - 190), top: Math.min(menu.y, window.innerHeight - 220) }} onPointerDown={(e) => e.stopPropagation()}>
      {menu.kind === "kf" ? (
        <>
          <div className="px-2 pt-1 pb-0.5 label-caps">Curve to next key</div>
          {CURVES.map((c) => (
            <button key={c} type="button" className={item} onClick={() => onCurve(c)}>
              <span className="w-3 text-accent-hover">{current === c ? "✓" : ""}</span>
              <span className="capitalize">{c}</span>
              <span className="ml-auto text-2xs text-fg-subtle">{c === "hold" ? "step" : c === "ease" ? "S-curve" : "ramp"}</span>
            </button>
          ))}
        </>
      ) : (
        <>
          <div className="px-2 pt-1 pb-0.5 label-caps">Loop length</div>
          {[1, 2, 4, 8, 16].map((n) => (
            <button key={n} type="button" className={item} onClick={() => onLength(n)}>
              <span className="w-3 text-accent-hover">{loopLen === n ? "✓" : ""}</span>
              {n} beat{n === 1 ? "" : "s"}
            </button>
          ))}
        </>
      )}
      <div className="my-1 h-px bg-line" />
      <button type="button" className={cn(item, "text-danger")} onClick={onDelete}>
        <span className="w-3" />
        Delete <span className="ml-auto text-2xs text-fg-subtle">⌫</span>
      </button>
    </div>,
    document.body,
  );
}
