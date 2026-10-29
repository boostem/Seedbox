import type { ReactNode } from "react";
import { CAMELOT_CODES, camelotAdjacent, keyColor, parseCamelot } from "@/lib/camelot";
import { cn } from "@/lib/cn";
import { textPx } from "@/lib/theme";

export interface CamelotWheelProps {
  size?: number;
  /** Tracks per key; keys with 0/undefined are dimmed when provided. */
  counts?: Record<string, number>;
  /** Highlighted key (e.g. hovered/playing track) — its harmonic neighbours get an outline. */
  active?: string | null;
  /** Ordered keys of a set: drawn as a numbered path through the wheel. */
  path?: string[];
  onSelect?: (code: string) => void;
  /** Text in the centre (defaults to the active key). */
  center?: ReactNode;
  className?: string;
}

const TAU = Math.PI * 2;

function polar(cx: number, cy: number, r: number, a: number): [number, number] {
  return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
}

function sector(cx: number, cy: number, r0: number, r1: number, a0: number, a1: number): string {
  const [x0, y0] = polar(cx, cy, r1, a0);
  const [x1, y1] = polar(cx, cy, r1, a1);
  const [x2, y2] = polar(cx, cy, r0, a1);
  const [x3, y3] = polar(cx, cy, r0, a0);
  return `M${x0},${y0} A${r1},${r1} 0 0 1 ${x1},${y1} L${x2},${y2} A${r0},${r0} 0 0 0 ${x3},${y3} Z`;
}

/** Camelot wheel (12 at the top, clockwise like a clock; outer ring B/major, inner ring A/minor). */
export function CamelotWheel({ size = 220, counts, active, path, onSelect, center, className }: CamelotWheelProps) {
  const R = size / 2;
  const c = R;
  const rings = { B: [R * 0.7, R - 1], A: [R * 0.42, R * 0.7 - 1.5] } as const;
  const act = parseCamelot(active)?.code ?? null;
  const centers: Record<string, [number, number]> = {};

  const segs = CAMELOT_CODES.map((code) => {
    const p = parseCamelot(code)!;
    const a = ((p.n % 12) / 12) * TAU;
    const half = TAU / 24 - 0.012;
    const [r0, r1] = rings[p.letter];
    const col = keyColor(code);
    const n = counts?.[code] ?? 0;
    const lit = counts ? n > 0 : true;
    const isActive = act === code;
    const related = act && !isActive && camelotAdjacent(act, code);
    centers[code] = polar(c, c, (r0 + r1) / 2, a);
    return { code, a, half, r0, r1, col, n, lit, isActive, related, p };
  });

  const pts = (path ?? []).map((k) => parseCamelot(k)?.code).filter((k): k is string => !!k).map((k) => centers[k]);

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className={cn("select-none", className)} role="img" aria-label="Camelot wheel">
      {segs.map((s) => (
        <g key={s.code} onClick={onSelect ? () => onSelect(s.code) : undefined} className={onSelect ? "cursor-pointer" : undefined}>
          <path
            d={sector(c, c, s.r0, s.r1, s.a - s.half, s.a + s.half)}
            fill={s.col.base}
            fillOpacity={s.isActive ? 1 : s.lit ? (counts ? 0.82 : 0.7) : 0.13}
            stroke={s.isActive ? "var(--color-white)" : s.related ? s.col.base : "none"}
            strokeWidth={s.isActive ? 2 : 1.25}
            strokeOpacity={s.related ? 1 : undefined}
          >
            <title>{`${s.code}${counts ? ` · ${s.n} track${s.n === 1 ? "" : "s"}` : ""}`}</title>
          </path>
          <text
            x={centers[s.code][0]}
            y={centers[s.code][1]}
            textAnchor="middle"
            dominantBaseline="central"
            fontSize={textPx(size * (s.p.letter === "B" ? 0.05 : 0.045))}
            fontWeight={650}
            fill={s.lit || s.isActive ? s.col.solidFg : s.col.base}
            fillOpacity={s.lit || s.isActive ? 0.92 : 0.7}
            className="pointer-events-none num"
          >
            {s.code}
          </text>
          {counts && s.n > 1 && (
            <text
              x={polar(c, c, s.r1 - 4, s.a + s.half - 0.06)[0]}
              y={polar(c, c, s.r1 - 4, s.a + s.half - 0.06)[1]}
              fontSize={textPx(size * 0.032)}
              textAnchor="middle"
              dominantBaseline="central"
              fill={s.col.solidFg}
              fillOpacity={0.7}
              className="pointer-events-none num"
            >
              {s.n}
            </text>
          )}
        </g>
      ))}
      {pts.length > 1 && (
        <g className="pointer-events-none">
          <polyline points={pts.map(([x, y]) => `${x},${y}`).join(" ")} fill="none" stroke="var(--color-white)" strokeOpacity={0.85} strokeWidth={1.5} strokeLinejoin="round" />
          {pts.map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r={i === 0 ? 3.5 : 2.2} fill={i === 0 ? "var(--color-white)" : "var(--color-bg)"} stroke="var(--color-white)" strokeWidth={1.2} />
          ))}
        </g>
      )}
      <circle cx={c} cy={c} r={R * 0.42 - 3} fill="var(--color-panel)" stroke="var(--color-line)" />
      <text x={c} y={c} textAnchor="middle" dominantBaseline="central" fontSize={textPx(size * 0.085)} fontWeight={700} fill={act ? keyColor(act).base : "var(--color-fg-subtle)"} className="num">
        {center ?? act ?? ""}
      </text>
    </svg>
  );
}
