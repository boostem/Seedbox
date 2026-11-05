import { useRef, type PointerEvent } from "react";
import type { ArcTemplate, SetOrderTrack } from "@/api/types";
import { Tabs } from "@/components/ui";
import { cn } from "@/lib/cn";
import { ARC_GRID, arcPath, arcPoints, type ArcPoint, paint, toGrid, toPoints } from "./arc";

const W = 960;
const H = 120;
const CUSTOM = "custom";

export interface ArcEditorProps {
  arc: string | [number, number][] | null;
  templates: ArcTemplate[];
  /** The running order, drawn over the arc as where each track really sits. */
  tracks: SetOrderTrack[];
  onChange: (arc: string | [number, number][]) => void;
  className?: string;
}

/** Choose a template or draw on the curve with the pointer; drawing on a template starts from its shape. */
export function ArcEditor({ arc, templates, tracks, onChange, className }: ArcEditorProps) {
  const points = arcPoints(arc, templates);
  const stroke = useRef<{ levels: number[]; last: ArcPoint } | null>(null);
  const current = Array.isArray(arc) ? CUSTOM : (arc ?? "peak_time");
  const items = [...templates.map((t) => ({ id: t.name, label: t.label, title: t.description })), { id: CUSTOM, label: "Custom", title: "Draw on the curve" }];

  const at = (e: PointerEvent<SVGSVGElement>): ArcPoint => {
    const box = e.currentTarget.getBoundingClientRect();
    return { t: (e.clientX - box.left) / box.width, v: 1 - (e.clientY - box.top) / box.height };
  };
  const down = (e: PointerEvent<SVGSVGElement>) => {
    e.currentTarget.setPointerCapture?.(e.pointerId);
    const to = at(e);
    const levels = paint(toGrid(points, ARC_GRID), null, to);
    stroke.current = { levels, last: to };
    onChange(toPoints(levels));
  };
  const move = (e: PointerEvent<SVGSVGElement>) => {
    if (!stroke.current) return;
    const to = at(e);
    const levels = paint(stroke.current.levels, stroke.current.last, to);
    stroke.current = { levels, last: to };
    onChange(toPoints(levels));
  };
  const up = () => {
    stroke.current = null;
  };
  const choose = (id: string) => {
    if (id === CUSTOM) onChange(toPoints(toGrid(points, ARC_GRID)));
    else onChange(id);
  };

  const span = Math.max(tracks.length - 1, 1);
  const known = tracks.filter((t) => t.energy !== null);
  const lo = known.length ? Math.min(...known.map((t) => t.energy!)) : 0;
  const hi = known.length ? Math.max(...known.map((t) => t.energy!)) : 1;

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <Tabs variant="segmented" items={items} value={current} onChange={choose} />
      <svg
        role="img"
        aria-label="Energy arc: drag to draw your own"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="h-28 w-full cursor-crosshair touch-none rounded-sm border border-line bg-bg/60"
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={up}
      >
        {[0.25, 0.5, 0.75].map((y) => (
          <line key={y} x1={0} x2={W} y1={y * H} y2={y * H} className="stroke-line" vectorEffect="non-scaling-stroke" />
        ))}
        <path d={arcPath(points, W, H)} fill="none" className="stroke-accent" strokeWidth={2} vectorEffect="non-scaling-stroke" />
        {hi > lo &&
          known.map((t) => {
            const x = (t.position / span) * W;
            const y = (1 - (t.energy! - lo) / (hi - lo)) * H;
            return <line key={t.position} x1={x} x2={x} y1={y} y2={H} className="stroke-fg-subtle" strokeWidth={2} vectorEffect="non-scaling-stroke" />;
          })}
      </svg>
    </div>
  );
}
