/** Canvas rows of the mix DAW other than the automation lanes: deck waveform, loop regions, bar ruler, "+ N more". */
import { ChevronDown, ChevronRight } from "lucide-react";
import { memo, useLayoutEffect, useMemo, useRef } from "react";
import type { DeckLoop, DeckPlan, MixPlan, TrackAnalysis } from "@/api/types";
import { setupCanvas, type View } from "@/features/waveform/draw";
import { prepareWave } from "@/features/waveform/prepare";
import { type Editor, H_LOOPS, H_RULER, H_WAVE, snapTo, useCursor } from "./daw";
import { barLabel, type DeckId } from "./lanes";
import { paintDeck, paintLoops, paintRuler, resolveColor } from "./paint";

export function useRowCanvas(width: number, height: number, paint: (ctx: CanvasRenderingContext2D, dpr: number) => void, deps: unknown[]) {
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

export const DeckCanvas = memo(function DeckCanvas({ deck, an, view, width, tint }: { deck: DeckPlan; an: TrackAnalysis | null; view: View; width: number; tint: string }) {
  const wave = useMemo(() => (an ? prepareWave(an) : null), [an]);
  const ref = useRowCanvas(width, H_WAVE, (ctx, dpr) => paintDeck(ctx, dpr, view, width, H_WAVE, deck, wave, resolveColor(tint)), [deck, wave, view, tint]);
  return <canvas ref={ref} style={{ height: H_WAVE }} className="block w-full" onPointerMove={(e) => useCursor.setState({ hover: view.start + (e.clientX - e.currentTarget.getBoundingClientRect().left) / view.pxPerSec })} onPointerLeave={() => useCursor.setState({ hover: null })} />;
});

export const LoopsCanvas = memo(function LoopsCanvas({ d, deck, view, width, selected, ed }: { d: DeckId; deck: DeckPlan; view: View; width: number; selected: number | null; ed: Editor }) {
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

export const RulerCanvas = memo(function RulerCanvas({ plan, view, width, ed }: { plan: MixPlan; view: View; width: number; ed: Editor }) {
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
export function MoreRow({ open, onClick, label, title }: { open: boolean; onClick: () => void; label: string; title?: string }) {
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
