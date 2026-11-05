/**
 * A plan on its master timeline: both decks' waveforms, the loop region, one lane's keyframes and the playhead.
 *
 * - Drag on the waveforms: scrub, audibly. Letting go carries on playing from there, if it was playing.
 * - Drag on the ruler: select a loop, on whole beats. A click on the ruler clears it.
 * - Drag a keyframe, double-click to add one: the edit is heard as the pointer moves.
 *
 * Everything here that moves is painted from refs (PERFORMANCE.md): React doesn't render per move or per frame.
 */
import { type PointerEvent as ReactPointerEvent, type RefObject, useEffect, useLayoutEffect, useRef } from "react";
import { LANE_RANGES } from "@/api/mixplan.generated";
import type { MixPlan } from "@/api/mixplan.generated";
import { deckTime } from "@/audio/engine/position";
import { setupCanvas } from "@/features/waveform/draw";
import { useFrame, useWidthRef } from "@/features/waveform/hooks";
import { alpha, wavePalette } from "@/features/waveform/palette";
import type { WaveData } from "@/features/waveform/prepare";
import { mayDraw } from "@/lib/frameCap";
import { addKeyframe, type DeckId, type LaneName, laneOf, moveKeyframe, withLane } from "./lanes";

/** What the timeline needs of the engine. */
export interface TimelineEngine {
  beat(): number;
  scrub(beat: number): void;
  endScrub(): void;
  seek(beat: number): void;
  setLoop(startBeat: number | null, endBeat?: number | null): void;
}

export interface PlanTimelineProps {
  /** The plan shown; read when painting, so a drag needs no render. */
  planRef: RefObject<MixPlan>;
  engine: TimelineEngine;
  waves: Record<DeckId, WaveData | null>;
  /** The lane with keyframes on it. */
  deck: DeckId;
  lane: LaneName;
  loop: [number, number] | null;
  onLoop: (loop: [number, number] | null) => void;
  onEdit: (plan: MixPlan) => void;
  className?: string;
}

const RULER = 22;
const DECK_H = 84;
const GAP = 2;
const HEIGHT = RULER + DECK_H * 2 + GAP;
const GRAB_PX = 7;
const PAD = 8; // the lane's top and bottom inside its deck

type Drag =
  | { kind: "scrub" }
  | { kind: "loop"; from: number; to: number }
  | { kind: "keyframe"; index: number };

export function PlanTimeline({ planRef, engine, waves, deck, lane, loop, onLoop, onEdit, className }: PlanTimelineProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const baseRef = useRef<HTMLCanvasElement>(null);
  const topRef = useRef<HTMLCanvasElement>(null);
  const drag = useRef<Drag | null>(null);
  const shown = useRef({ deck, lane, loop });
  shown.current = { deck, lane, loop };

  const range = () => planRef.current.window;
  const xOf = (beat: number, width: number) => {
    const { start_beat: a, end_beat: b } = range();
    return ((beat - a) / (b - a || 1)) * width;
  };
  const beatOf = (x: number, width: number) => {
    const { start_beat: a, end_beat: b } = range();
    return a + (Math.max(0, Math.min(width, x)) / (width || 1)) * (b - a);
  };
  const deckTop = (id: DeckId) => (id === "a" ? RULER : RULER + DECK_H + GAP);
  const yOf = (value: number) => {
    const [lo, hi] = LANE_RANGES[shown.current.lane];
    return deckTop(shown.current.deck) + PAD + (1 - (value - lo) / (hi - lo)) * (DECK_H - 2 * PAD);
  };
  const valueOf = (y: number) => {
    const [lo, hi] = LANE_RANGES[shown.current.lane];
    return lo + (1 - (y - deckTop(shown.current.deck) - PAD) / (DECK_H - 2 * PAD)) * (hi - lo);
  };

  const paintBase = () => {
    const c = baseRef.current;
    const width = widthRef.current;
    if (!c || !width) return;
    const s = setupCanvas(c, width, HEIGHT);
    if (!s) return;
    const { ctx } = s;
    const p = wavePalette();
    const plan = planRef.current;
    ctx.fillStyle = p.panel;
    ctx.fillRect(0, 0, width, RULER);
    ctx.fillStyle = p.bg;
    ctx.fillRect(0, RULER, width, HEIGHT - RULER);
    const { start_beat: a, end_beat: b } = plan.window;
    const beatsPerBar = 4;
    const everyBars = [1, 2, 4, 8, 16, 32, 64].find((n) => (n * beatsPerBar * width) / (b - a || 1) >= 36) ?? 128;
    ctx.font = `600 10px ${p.font}`;
    ctx.textBaseline = "middle";
    for (let bar = Math.ceil(a / beatsPerBar / everyBars) * everyBars; bar * beatsPerBar <= b; bar += everyBars) {
      const x = Math.round(xOf(bar * beatsPerBar, width)) + 0.5;
      ctx.fillStyle = alpha(p.fg, 0.14);
      ctx.fillRect(x - 0.5, RULER, 1, HEIGHT - RULER);
      ctx.fillStyle = alpha(p.fg, 0.55);
      ctx.fillText(String(bar + 1), x + 3, RULER / 2);
    }
    for (const id of ["a", "b"] as const) paintDeck(ctx, plan, id, waves[id], deckTop(id), width, xOf);
  };

  const paintTop = () => {
    const c = topRef.current;
    const width = widthRef.current;
    if (!c || !width) return;
    const s = setupCanvas(c, width, HEIGHT);
    if (!s) return;
    const { ctx } = s;
    const p = wavePalette();
    const d = drag.current;
    const region: [number, number] | null = d?.kind === "loop" ? [Math.min(d.from, d.to), Math.max(d.from, d.to)] : shown.current.loop;
    if (region) {
      const x0 = xOf(region[0], width);
      const x1 = xOf(region[1], width);
      ctx.fillStyle = alpha(p.accent, 0.14);
      ctx.fillRect(x0, RULER, x1 - x0, HEIGHT - RULER);
      ctx.fillStyle = alpha(p.accent, 0.8);
      ctx.fillRect(x0, 0, x1 - x0, 4);
      ctx.fillRect(Math.round(x0), 0, 1, HEIGHT);
      ctx.fillRect(Math.round(x1), 0, 1, HEIGHT);
    }
    const keyframes = laneOf(planRef.current[shown.current.deck], shown.current.lane);
    if (keyframes.length) {
      ctx.strokeStyle = p.accent;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(0, yOf(keyframes[0].value));
      keyframes.forEach((k, i) => {
        const x = xOf(k.beat, width);
        if (i > 0 && keyframes[i - 1].curve === "hold") ctx.lineTo(x, yOf(keyframes[i - 1].value));
        ctx.lineTo(x, yOf(k.value));
      });
      ctx.lineTo(width, yOf(keyframes[keyframes.length - 1].value));
      ctx.stroke();
      ctx.fillStyle = p.accent;
      for (const k of keyframes) {
        ctx.beginPath();
        ctx.arc(xOf(k.beat, width), yOf(k.value), 3.5, 0, 2 * Math.PI);
        ctx.fill();
      }
    }
    const x = Math.round(xOf(engine.beat(), width)) + 0.5;
    ctx.fillStyle = p.playhead;
    ctx.fillRect(x - 0.5, 0, 1.5, HEIGHT);
  };

  const frameTop = useFrame(paintTop);
  const widthRef = useWidthRef(boxRef, () => {
    paintBase();
    paintTop();
  });
  useLayoutEffect(() => {
    paintBase();
    paintTop();
  });
  const topFn = useRef(paintTop);
  topFn.current = paintTop;
  useEffect(() => {
    let raf = 0;
    const loop = () => {
      if (mayDraw()) topFn.current();
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, []);

  const pointer = (e: ReactPointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top, width: r.width || widthRef.current };
  };

  const hitKeyframe = (x: number, y: number, width: number): number => {
    const keyframes = laneOf(planRef.current[shown.current.deck], shown.current.lane);
    let best = -1;
    let bestD = GRAB_PX;
    keyframes.forEach((k, i) => {
      const d = Math.hypot(xOf(k.beat, width) - x, yOf(k.value) - y);
      if (d <= bestD) {
        best = i;
        bestD = d;
      }
    });
    return best;
  };

  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const { x, y, width } = pointer(e);
    if (y < RULER) {
      const beat = Math.round(beatOf(x, width));
      drag.current = { kind: "loop", from: beat, to: beat };
    } else {
      const index = hitKeyframe(x, y, width);
      if (index >= 0) {
        drag.current = { kind: "keyframe", index };
      } else {
        drag.current = { kind: "scrub" };
        engine.scrub(beatOf(x, width));
      }
    }
    frameTop();
  };

  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    const { x, y, width } = pointer(e);
    if (d.kind === "scrub") {
      engine.scrub(beatOf(x, width));
    } else if (d.kind === "loop") {
      d.to = Math.round(beatOf(x, width));
    } else {
      const { deck: id, lane: name } = shown.current;
      const plan = planRef.current;
      const moved = moveKeyframe(laneOf(plan[id], name), name, d.index, beatOf(x, width), valueOf(y));
      onEdit(withLane(plan, id, name, moved));
    }
    frameTop();
  };

  const onUp = () => {
    const d = drag.current;
    drag.current = null;
    if (d?.kind === "scrub") {
      engine.endScrub();
    } else if (d?.kind === "loop") {
      const from = Math.min(d.from, d.to);
      const to = Math.max(d.from, d.to);
      onLoop(to > from ? [from, to] : null);
    }
    frameTop();
  };

  const onDouble = (e: React.MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - r.left;
    const y = e.clientY - r.top;
    const { deck: id, lane: name } = shown.current;
    if (y < deckTop(id) || y >= deckTop(id) + DECK_H) return;
    const width = r.width || widthRef.current;
    const plan = planRef.current;
    const { keyframes } = addKeyframe(laneOf(plan[id], name), name, beatOf(x, width), valueOf(y));
    onEdit(withLane(plan, id, name, keyframes));
    frameTop();
  };

  return (
    <div
      ref={boxRef}
      data-testid="plan-timeline"
      className={`relative w-full cursor-crosshair touch-none overflow-hidden rounded-sm border border-line bg-bg ${className ?? ""}`}
      style={{ height: HEIGHT }}
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      onDoubleClick={onDouble}
    >
      <canvas ref={baseRef} className="absolute inset-0 h-full w-full" />
      <canvas ref={topRef} className="pointer-events-none absolute inset-0 h-full w-full" />
      <span className="pointer-events-none absolute left-1.5 text-2xs font-semibold text-fg-subtle" style={{ top: RULER + 3 }}>A</span>
      <span className="pointer-events-none absolute left-1.5 text-2xs font-semibold text-fg-subtle" style={{ top: RULER + DECK_H + GAP + 3 }}>B</span>
    </div>
  );
}

/** A deck's waveform on the master timeline, column by column, wherever its grid puts each beat in the file. */
function paintDeck(
  ctx: CanvasRenderingContext2D,
  plan: MixPlan,
  id: DeckId,
  wave: WaveData | null,
  top: number,
  width: number,
  xOf: (beat: number, width: number) => number,
): void {
  const p = wavePalette();
  const deck = plan[id];
  const { start_beat: a, end_beat: b } = plan.window;
  const x0 = Math.max(0, Math.floor(xOf(deck.play_from_beat, width)));
  const x1 = Math.min(width, Math.ceil(xOf(deck.play_to_beat, width)));
  ctx.fillStyle = alpha(p.fg, 0.04);
  ctx.fillRect(x0, top, x1 - x0, DECK_H);
  if (!wave || !wave.n) return;
  const mid = top + DECK_H / 2;
  const beatAt = (x: number) => a + (x / (width || 1)) * (b - a);
  for (let x = x0; x < x1; x++) {
    const t0 = deckTime(deck, beatAt(x));
    const t1 = deckTime(deck, beatAt(x + 1));
    const i0 = Math.max(0, Math.floor(t0 * wave.pps));
    const i1 = Math.min(wave.n, Math.max(i0 + 1, Math.ceil(t1 * wave.pps)));
    let low = 0;
    let midBand = 0;
    let high = 0;
    for (let i = i0; i < i1; i++) {
      low = Math.max(low, wave.low[i]);
      midBand = Math.max(midBand, wave.mid[i]);
      high = Math.max(high, wave.high[i]);
    }
    const half = (DECK_H / 2 - 2) * Math.min(1, Math.max(low, midBand, high));
    ctx.fillStyle = low >= midBand && low >= high ? p.low : midBand >= high ? p.mid : p.high;
    ctx.fillRect(x, mid - half, 1, 2 * half);
  }
}
