/**
 * Lanes as rows on the plan's master timeline, under `PlanTimeline` and lined up with it. A row can show what the lane
 * acts on behind its curve: a stem or a band of the track, dimmed as the curve turns it down.
 *
 * - Drag a keyframe: heard as the pointer moves. Double-click: add one. Right-click: remove it. Alt-click: its next curve.
 *
 * A drag repaints its own row from refs, and the playhead is one element over the whole stack moved by its own frame
 * loop: neither renders React (PERFORMANCE.md).
 */
import { type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type RefObject, useEffect, useLayoutEffect, useRef } from "react";
import { LANE_RANGES } from "@/api/mixplan.generated";
import type { Keyframe, MixPlan } from "@/api/mixplan.generated";
import { deckTime } from "@/audio/engine/position";
import { setupCanvas } from "@/features/waveform/draw";
import { useFrame, useWidthRef } from "@/features/waveform/hooks";
import { alpha, wavePalette } from "@/features/waveform/palette";
import { laneValue, stemBrightness, stemShape } from "@/features/waveform/stems";
import { mayDraw } from "@/lib/frameCap";
import { cn } from "@/lib/cn";
import { addKeyframe, CURVES, type DeckId, laneDefault, type LaneName, laneOf, moveKeyframe, removeKeyframe, setCurve, withLane } from "./lanes";

export interface LaneRowSpec {
  deck: DeckId;
  lane: LaneName;
  label: string;
  /** A canvas colour for the curve and what's behind it. */
  color?: string;
  /** What the lane acts on: a stem or a band of this deck's track, peaks 0..1. */
  peaks?: Float32Array | null;
  pps?: number;
}

export interface LaneGroup {
  title: string;
  rows: LaneRowSpec[];
}

export interface KeySelection {
  deck: DeckId;
  lane: LaneName;
  index: number;
}

export interface LaneEditing {
  planRef: RefObject<MixPlan>;
  /** A gesture's edits are one undo step. */
  begin: () => void;
  edit: (plan: MixPlan) => void;
  end: () => void;
  selected: KeySelection | null;
  onSelect: (selection: KeySelection | null) => void;
}

const GRAB_PX = 7;
const PAD = 5;

/** A lane's value as a gain on what's behind it, for dimming: null when it isn't one. */
function gainOf(lane: LaneName, value: number): number | null {
  if (lane.startsWith("gain_") || lane === "volume") return value;
  if (lane.startsWith("eq_")) return Math.min(1, 10 ** (value / 20));
  return null;
}

function LaneRow({ row, height, editing, revision }: { row: LaneRowSpec; height: number; editing: LaneEditing; revision: number }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const drag = useRef<{ index: number } | null>(null);
  const { planRef } = editing;
  const { deck, lane } = row;

  const range = () => planRef.current.window;
  const xOf = (beat: number, width: number) => {
    const { start_beat: a, end_beat: b } = range();
    return ((beat - a) / (b - a || 1)) * width;
  };
  const beatOf = (x: number, width: number) => {
    const { start_beat: a, end_beat: b } = range();
    return a + (Math.max(0, Math.min(width, x)) / (width || 1)) * (b - a);
  };
  const yOf = (value: number) => {
    const [lo, hi] = LANE_RANGES[lane];
    return PAD + (1 - (value - lo) / (hi - lo)) * (height - 2 * PAD);
  };
  const valueOf = (y: number) => {
    const [lo, hi] = LANE_RANGES[lane];
    return lo + (1 - (y - PAD) / (height - 2 * PAD)) * (hi - lo);
  };
  const shownKeys = (keyframes: Keyframe[]): Keyframe[] => (keyframes.length ? keyframes : [{ beat: range().start_beat, value: laneDefault(lane), curve: "linear" }]);

  const selectedRef = useRef(editing.selected);
  selectedRef.current = editing.selected;

  const paint = () => {
    const c = canvasRef.current;
    const width = widthRef.current;
    if (!c || !width) return;
    const s = setupCanvas(c, width, height);
    if (!s) return;
    const { ctx } = s;
    const p = wavePalette();
    const plan = planRef.current;
    const d = plan[deck];
    const keyframes = shownKeys(laneOf(d, lane));
    const color = row.color ?? p.accent;
    ctx.fillStyle = p.bg;
    ctx.fillRect(0, 0, width, height);
    const x0 = Math.max(0, Math.floor(xOf(d.play_from_beat, width)));
    const x1 = Math.min(width, Math.ceil(xOf(d.play_to_beat, width)));
    ctx.fillStyle = alpha(p.fg, 0.03);
    ctx.fillRect(x0, 0, x1 - x0, height);

    const { peaks, pps } = row;
    if (peaks && pps) {
      const mid = height / 2;
      const { start_beat: a, end_beat: b } = plan.window;
      const beatAt = (x: number) => a + (x / width) * (b - a);
      ctx.fillStyle = color;
      for (let x = x0; x < x1; x++) {
        const beat = beatAt(x);
        const i0 = Math.max(0, Math.floor(deckTime(d, beat) * pps));
        const i1 = Math.min(peaks.length, Math.max(i0 + 1, Math.ceil(deckTime(d, beatAt(x + 1)) * pps)));
        let top = 0;
        for (let i = i0; i < i1; i++) if (peaks[i] > top) top = peaks[i];
        if (!top) continue;
        const gain = gainOf(lane, laneValue(keyframes, beat)) ?? 1;
        ctx.globalAlpha = 0.55 * stemBrightness(gain);
        const half = (height / 2 - 1) * stemShape(top);
        ctx.fillRect(x, mid - half, 1, 2 * half);
      }
      ctx.globalAlpha = 1;
    }

    ctx.strokeStyle = peaks ? p.fg : color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(0, yOf(keyframes[0].value));
    keyframes.forEach((k, i) => {
      const x = xOf(k.beat, width);
      const before = keyframes[i - 1];
      if (before?.curve === "hold") ctx.lineTo(x, yOf(before.value));
      else if (before?.curve === "ease") {
        const from = xOf(before.beat, width);
        for (let px = from + 4; px < x; px += 4) ctx.lineTo(px, yOf(laneValue(keyframes, beatOf(px, width))));
      }
      ctx.lineTo(x, yOf(k.value));
    });
    ctx.lineTo(width, yOf(keyframes[keyframes.length - 1].value));
    ctx.stroke();
    const chosen = selectedRef.current;
    keyframes.forEach((k, i) => {
      const on = chosen?.deck === deck && chosen.lane === lane && chosen.index === i;
      ctx.fillStyle = on || peaks ? p.fg : color;
      ctx.beginPath();
      ctx.arc(xOf(k.beat, width), yOf(k.value), on ? 4.5 : 3, 0, 2 * Math.PI);
      ctx.fill();
    });
  };

  const frame = useFrame(paint);
  const widthRef = useWidthRef(boxRef, paint);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `revision` and the selection are when the plan on show changes
  useLayoutEffect(paint, [revision, editing.selected, row.peaks, height]);

  const at = (e: { clientX: number; clientY: number; currentTarget: HTMLElement }) => {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top, width: r.width || widthRef.current };
  };

  const hit = (x: number, y: number, width: number): number => {
    let best = -1;
    let bestD = GRAB_PX;
    laneOf(planRef.current[deck], lane).forEach((k, i) => {
      const d = Math.hypot(xOf(k.beat, width) - x, yOf(k.value) - y);
      if (d <= bestD) {
        best = i;
        bestD = d;
      }
    });
    return best;
  };

  const put = (keyframes: Keyframe[]) => editing.edit(withLane(planRef.current, deck, lane, keyframes));

  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const { x, y, width } = at(e);
    const index = hit(x, y, width);
    if (index < 0) {
      editing.onSelect(null);
      return;
    }
    editing.onSelect({ deck, lane, index });
    const keyframes = laneOf(planRef.current[deck], lane);
    if (e.altKey) {
      const next = CURVES[(CURVES.indexOf(keyframes[index].curve) + 1) % CURVES.length];
      put(setCurve(keyframes, index, next));
      return;
    }
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { index };
    editing.begin();
  };

  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    const { x, y, width } = at(e);
    const moved = moveKeyframe(laneOf(planRef.current[deck], lane), lane, d.index, beatOf(x, width), valueOf(y));
    put(moved);
    frame();
  };

  const onUp = () => {
    if (!drag.current) return;
    drag.current = null;
    editing.end();
  };

  const onDouble = (e: ReactMouseEvent<HTMLDivElement>) => {
    const { x, y, width } = at(e);
    if (hit(x, y, width) >= 0) return;
    const { keyframes, index } = addKeyframe(laneOf(planRef.current[deck], lane), lane, beatOf(x, width), valueOf(y));
    put(keyframes);
    editing.onSelect({ deck, lane, index });
  };

  const onContext = (e: ReactMouseEvent<HTMLDivElement>) => {
    const { x, y, width } = at(e);
    const index = hit(x, y, width);
    if (index < 0) return;
    e.preventDefault();
    editing.onSelect(null);
    put(removeKeyframe(laneOf(planRef.current[deck], lane), index));
  };

  return (
    <div
      ref={boxRef}
      data-testid={`lane-${deck}-${lane}`}
      className="relative w-full cursor-crosshair touch-none overflow-hidden border-t border-line first:border-t-0"
      style={{ height }}
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      onDoubleClick={onDouble}
      onContextMenu={onContext}
    >
      <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" />
      <span className="pointer-events-none absolute left-1 top-0.5 rounded-xs bg-bg/70 px-1 text-2xs font-semibold text-fg-muted">{row.label}</span>
    </div>
  );
}

export interface LaneStackProps {
  groups: LaneGroup[];
  rowHeight: number;
  editing: LaneEditing;
  /** Goes up when the plan on show changes: rows repaint on it. */
  revision: number;
  /** Where the music is, in master beats. */
  beat: () => number;
  className?: string;
}

export function LaneStack({ groups, rowHeight, editing, revision, beat, className }: LaneStackProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const read = useRef({ beat, planRef: editing.planRef });
  read.current = { beat, planRef: editing.planRef };

  useEffect(() => {
    let raf = 0;
    const tick = () => {
      if (mayDraw() && headRef.current && boxRef.current) {
        const { start_beat: a, end_beat: b } = read.current.planRef.current.window;
        const x = ((read.current.beat() - a) / (b - a || 1)) * boxRef.current.clientWidth;
        headRef.current.style.transform = `translateX(${Math.round(x)}px)`;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <div ref={boxRef} className={cn("relative flex flex-col gap-2", className)}>
      {groups.map((group) => (
        <section key={group.title} aria-label={group.title}>
          <h3 className="mb-1 pl-1.5 text-2xs font-semibold uppercase tracking-wide text-fg-subtle">{group.title}</h3>
          <div className="overflow-hidden rounded-sm border border-line bg-bg">
            {group.rows.map((row) => (
              <LaneRow key={`${row.deck}/${row.lane}`} row={row} height={rowHeight} editing={editing} revision={revision} />
            ))}
          </div>
        </section>
      ))}
      <div ref={headRef} aria-hidden className="pointer-events-none absolute inset-y-0 left-0 w-px bg-playhead" />
    </div>
  );
}
