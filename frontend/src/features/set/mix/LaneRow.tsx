/** Automation lane row of the mix DAW: the label (live value under the cursor) and the editable keyframe canvas. */
import { memo, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, useRef, useState } from "react";
import type { Keyframe, MixPlan } from "@/api/types";
import type { View } from "@/features/waveform/draw";
import { cn } from "@/lib/cn";
import { clamp, type Editor, type Sel, snapTo, useCursor } from "./daw";
import { barLabel, type DeckId, getLane, type LaneKey, laneValue, metaOf, setLane } from "./lanes";
import { LANE_PAD, paintLane, valueY, yValue } from "./paint";
import { useRowCanvas } from "./rows";
import { hitKeyframe, moveSelection } from "./selection";

export const LaneLabel = memo(function LaneLabel({ k, kfs, height, deck, dim }: { k: LaneKey; kfs: Keyframe[]; height: number; deck?: DeckId; dim?: boolean }) {
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

export const LaneCanvas = memo(function LaneCanvas({ k, kfs, range, view, width, height, selected, play, ed }: LaneRowProps) {
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
    ed.setFxSel(null);
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
