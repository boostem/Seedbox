/**
 * Mini-DAW on the MASTER beat timeline (beat 0 = B in): bar ruler with markers, master tempo lane, deck A/B
 * waveforms (positioned through each deck's beat grid, loops as repeated regions, fader shading), and per-deck
 * automation lanes + loop regions + effect events (spinback, brake, stutter) — all canvas, one canvas per row so a drag only repaints the rows it touches.
 *
 * Editing: drag keyframes (snap to beats; ⌥ = ¼-beat grid; ⇧ = fine, no snapping), double-click to add, ⌫ to
 * delete, right-click for the curve (linear / ease / hold), box-select on empty space (⇧ adds), drag a selection
 * across lanes, loops and effects: drag to move, drag an edge to resize, double-click the row to add one (an effect
 * starts as a 1-beat spinback; right-click for its type and a stutter's repeat length). Without a mouse: the
 * "Keys & loops" list under the rows (KeyList).
 *
 * `view` (settings.mix_view): "manual" shows every lane; "advanced" only the main ones (ADVANCED_LANES, no tempo);
 * "simple" just the ruler and the two decks, as a read-only overview.
 */
import { Info, Minus, Plus, Scan } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Curve, DeckFxType, LaneId, MixPlan, MixView, TrackAnalysis } from "@/api/types";
import { IconButton, Tooltip } from "@/components/ui";
import { setupCanvas, type View } from "@/features/waveform/draw";
import { useTimeView, useWidth } from "@/features/waveform/hooks";
import { alpha, wavePalette } from "@/features/waveform/palette";
import { cn } from "@/lib/cn";
import { useSessionState } from "../useSessionState";
import { ContextMenu } from "./ContextMenu";
import { type Editor, GUTTER, H_FX, H_LANE, H_LOOPS, H_RULER, H_TEMPO, H_WAVE, type Menu, type RegionSel, type Sel, useCursor } from "./daw";
import { KeyList } from "./KeyList";
import { LaneCanvas, LaneLabel } from "./LaneRow";
import { barLabel, type DeckId, getLane, laneRange, LANES, type LaneKey, laneUsed, setLane, STUTTER_DIV } from "./lanes";
import { paintMarkerLines } from "./paint";
import { DeckCanvas, FxCanvas, LoopsCanvas, MoreRow, RulerCanvas } from "./rows";
import { deleteKeyframes, pruneSelection } from "./selection";
import { useDawHotkeys } from "./useDawHotkeys";
import { ADVANCED_LANES } from "./views";
import type { BeatClock } from "./useMixRender";

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
  view?: MixView;
}

/** Per-session "show unused lanes" state: deck A, deck B, master tempo lane. */
const SHOW_KEY = "seedbox.mix.showUnused";
type ShowAll = Record<DeckId | "tempo", boolean>;
const SHOW_DEFAULT: ShowAll = { a: false, b: false, tempo: false };

const DECK_TINT: Record<DeckId, string> = { a: "var(--color-fg-muted)", b: "var(--color-accent)" };

const SHORTCUTS = "drag keys · dbl-click add · ⌫ delete · right-click curve / effect type · ⌥ ¼-beat · ⇧ fine · ⌘-scroll zoom · keyboard: the key list below";

export function MixDaw({ plan, update, checkpoint, undo, redo, canUndo, canRedo, anA, anB, nameA, nameB, clock, onPlayFrom, hotkeys = true, view: mode = "manual" }: MixDawProps) {
  const allLanes = mode === "manual" ? LANES : mode === "advanced" ? ADVANCED_LANES : [];
  const editable = mode !== "simple";
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
  const [loopSel, setLoopSel] = useState<RegionSel>(null);
  const [fxSel, setFxSel] = useState<RegionSel>(null);
  const [menu, setMenu] = useState<Menu | null>(null);

  const refs = useRef({ plan, sel, view, loopSel, fxSel });
  refs.current = { plan, sel, view, loopSel, fxSel };
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
      fxSel: () => refs.current.fxSel,
      setFxSel: (s) => {
        refs.current.fxSel = s;
        setFxSel(s);
      },
      menu: setMenu,
      playFrom: onPlayFrom,
    }),
    [update, checkpoint, onPlayFrom],
  );

  // a different plan shape (regenerated) → drop selections that no longer exist
  useEffect(() => {
    setSel((s) => pruneSelection(s, plan));
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
    const fs = refs.current.fxSel;
    if (fs) {
      update({ ...p, [fs.deck]: { ...p[fs.deck], fx: (p[fs.deck].fx ?? []).filter((_, i) => i !== fs.index) } });
      ed.setFxSel(null);
      return true;
    }
    const s = refs.current.sel;
    if (!s.size) return false;
    update(deleteKeyframes(p, s));
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

  const editFx = (patch: { type?: DeckFxType; div?: number }) => {
    if (menu?.kind !== "fx") return;
    const p = refs.current.plan;
    const d = menu.deck;
    const fx = (p[d].fx ?? []).map((f, i) => {
      if (i !== menu.index) return f;
      const type = patch.type ?? f.type;
      const { div: _, ...rest } = f;
      return type === "stutter" ? { ...rest, type, div: patch.div ?? f.div ?? STUTTER_DIV } : { ...rest, type };
    });
    update({ ...p, [d]: { ...p[d], fx } });
    setMenu(null);
  };

  const fit = () => tv.set({ start: plan.window.start_beat - 2, span: plan.window.end_beat - plan.window.start_beat + 4 });
  const clear = () => {
    if (!menu && !refs.current.sel.size && !refs.current.loopSel && !refs.current.fxSel) return false;
    setMenu(null);
    ed.setSel(new Map());
    ed.setLoopSel(null);
    ed.setFxSel(null);
    return true;
  };
  useDawHotkeys({
    enabled: hotkeys,
    deleteSelection,
    clear,
    undo,
    redo,
    canUndo,
    canRedo,
    zoom: tv.zoom,
    fit,
  });

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
    const unused = allLanes.filter((m) => !isUsed(m.id));
    const open = !!showAll[d];
    const hasLoops = editable && (deck.loops.length > 0 || loopSel?.deck === d);
    const fxCount = deck.fx?.length ?? 0;
    const hasFx = editable && (fxCount > 0 || fxSel?.deck === d);
    // lanes behind the "+ N more" row (empty loops / effects rows count)
    const hidden = editable ? unused.length + (hasLoops ? 0 : 1) + (hasFx ? 0 : 1) : 0;
    const lanes = open ? allLanes : allLanes.filter((m) => !unused.includes(m));
    const name = d === "a" ? nameA : nameB;
    const details = `${name} · ${deck.native_bpm.toFixed(1)} BPM · plays bar ${barLabel(deck.play_from_beat)} → ${barLabel(deck.play_to_beat)} · echo ${deck.echo_beats} beat${deck.loops.length ? ` · ${deck.loops.length} loop${deck.loops.length === 1 ? "" : "s"}` : ""}${fxCount ? ` · ${fxCount} effect${fxCount === 1 ? "" : "s"}` : ""}`;
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
            <LaneLabel key={`${k}-l`} k={k} kfs={deck.lanes[m.id as LaneId]} height={H_LANE} dim={dim} label={m.label} />,
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
        {(open || hasFx) && (
          <>
            <div style={{ height: H_FX }} className="flex items-center gap-1.5 border-b border-line/60 pr-2 pl-3 text-xs text-fg-muted" title="Spinback, brake, stutter: they replace what the deck plays there">
              <span className="size-1.5 rounded-full bg-danger" /> Effects
            </div>
            <div className="border-b border-line/60">
              <FxCanvas d={d} deck={deck} view={view} width={width} selected={fxSel?.deck === d ? fxSel.index : null} ed={ed} />
            </div>
          </>
        )}
        {hidden > 0 && (
          <MoreRow
            open={open}
            onClick={() => toggleShow(d)}
            title={[...unused.map((m) => m.label), ...(hasLoops ? [] : ["Loops"]), ...(hasFx ? [] : ["Effects"])].join(", ")}
            label={open ? `Hide ${hidden} unused lane${hidden === 1 ? "" : "s"}` : `+ ${hidden} more lane${hidden === 1 ? "" : "s"}`}
          />
        )}
      </>
    );
  };

  const tempoLane = mode === "manual"; // advanced / simple: the planner's tempo ramp runs under the hood
  const tempoUsed = laneUsed("tempo", plan.tempo) || sel.has("tempo");
  const tempoOpen = tempoLane && (tempoUsed || !!showAll.tempo);

  return (
    <div className="flex flex-col">
      <div className="mb-1.5 flex h-6 items-center gap-2">
        <span className="label-caps">{editable ? "Automation" : "Overview"}</span>
        {editable && (
          <Tooltip content={SHORTCUTS} side="bottom">
            <button type="button" aria-label={`Editing shortcuts: ${SHORTCUTS}`} className="grid size-4 place-items-center rounded-xs text-fg-faint hover:text-fg-muted">
              <Info size={12} />
            </button>
          </Tooltip>
        )}
        <span className="flex-1" />
        <span className="shrink-0 text-2xs whitespace-nowrap text-fg-subtle num">{Math.round(tv.view.span / 4)} bars visible</span>
        <IconButton icon={Minus} label="Zoom out (−)" size="sm" onClick={() => tv.zoom(1 / 1.5)} />
        <IconButton icon={Plus} label="Zoom in (=)" size="sm" onClick={() => tv.zoom(1.5)} />
        <IconButton icon={Scan} label="Fit the render window (0)" size="sm" onClick={fit} />
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
          {tempoLane && !tempoUsed && (
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
      {editable && <KeyList lanes={allLanes} tempo={tempoLane} plan={plan} sel={sel} setSel={ed.setSel} loopSel={loopSel} setLoopSel={ed.setLoopSel} fxSel={fxSel} setFxSel={ed.setFxSel} update={update} menu={setMenu} />}
      {tip &&
        createPortal(
          <div className="pointer-events-none fixed z-[1000] rounded-xs bg-overlay px-1.5 py-0.5 text-2xs whitespace-nowrap text-fg shadow-pop num" style={{ left: tip.x + 12, top: tip.y - 26 }}>
            {tip.text}
          </div>,
          document.body,
        )}
      {menu && <ContextMenu menu={menu} plan={plan} onClose={() => setMenu(null)} onCurve={setCurve} onLength={setLoopLength} onFx={editFx} onDelete={() => (deleteSelection(), setMenu(null))} />}
    </div>
  );
}
