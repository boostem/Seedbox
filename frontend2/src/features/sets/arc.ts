/** A drawn energy arc as levels on an even grid of times, and the stroke that paints them. */

export const ARC_GRID = 49;

export interface ArcPoint {
  /** 0-1 across the set. */
  t: number;
  /** 0-1, quiet to intense. */
  v: number;
}

const clamp = (x: number) => Math.min(1, Math.max(0, x));

/** The curve through `points` at each of the grid's times. */
export function toGrid(points: [number, number][], size = ARC_GRID): number[] {
  const out: number[] = [];
  for (let i = 0; i < size; i++) {
    const t = i / (size - 1);
    const after = points.findIndex(([x]) => x >= t);
    if (after <= 0) {
      out.push(points[after === 0 ? 0 : points.length - 1][1]);
      continue;
    }
    const [x0, y0] = points[after - 1];
    const [x1, y1] = points[after];
    out.push(y0 + ((y1 - y0) * (t - x0)) / (x1 - x0));
  }
  return out;
}

export function toPoints(levels: number[]): [number, number][] {
  return levels.map((v, i) => [i / (levels.length - 1), Math.round(v * 1000) / 1000]);
}

/** `levels` with the stroke from `from` to `to` painted in: every grid time between them, so a fast drag leaves no gap. */
export function paint(levels: number[], from: ArcPoint | null, to: ArcPoint): number[] {
  const out = levels.slice();
  const last = out.length - 1;
  const end = { t: clamp(to.t), v: clamp(to.v) };
  const start = from ? { t: clamp(from.t), v: clamp(from.v) } : end;
  const a = Math.round(Math.min(start.t, end.t) * last);
  const b = Math.round(Math.max(start.t, end.t) * last);
  for (let i = a; i <= b; i++) {
    const across = b === a ? 1 : (i - a) / (b - a);
    const [left, right] = start.t <= end.t ? [start.v, end.v] : [end.v, start.v];
    out[i] = left + (right - left) * across;
  }
  return out;
}

/** An SVG path through the points, in a box of `w` x `h` with level 1 at the top. */
export function arcPath(points: [number, number][], w: number, h: number): string {
  return points.map(([t, v], i) => `${i ? "L" : "M"}${(t * w).toFixed(1)} ${((1 - v) * h).toFixed(1)}`).join(" ");
}

/** The settings' arc as points to draw, whichever form it's in. */
export function arcPoints(arc: string | [number, number][] | null, templates: { name: string; points: [number, number][] }[], fallback = "peak_time"): [number, number][] {
  if (Array.isArray(arc)) return arc;
  return templates.find((t) => t.name === (arc ?? fallback))?.points ?? [];
}
