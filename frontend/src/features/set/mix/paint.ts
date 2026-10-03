/** Canvas painters for the mix editor. X axis = MASTER BEATS (beat 0 = B in); View.pxPerSec is px per beat here. */
import type { DeckPlan, Keyframe, MixMarker, MixPlan } from "@/api/types";
import { drawWave, type Lane, type View, xOf } from "@/features/waveform/draw";
import { alpha, wavePalette } from "@/features/waveform/palette";
import type { WaveData } from "@/features/waveform/prepare";
import { cssVar } from "@/lib/theme";
import { laneValue } from "./lanes";

const colorCache = new Map<string, string>();
/** "var(--color-x)" → resolved colour (canvas can't use CSS vars). */
export function resolveColor(c: string): string {
  const m = /^var\((--[^)]+)\)$/.exec(c);
  if (!m) return c;
  let v = colorCache.get(m[1]);
  if (!v) {
    v = cssVar(m[1]) || "#888";
    colorCache.set(m[1], v);
  }
  return v;
}

export const beatX = (view: View, beat: number) => xOf(view, beat);

/** Bar / phrase grid (every 4 beats / 32 beats from beat 0), beats when zoomed in. */
export function paintGrid(ctx: CanvasRenderingContext2D, view: View, y: number, h: number): void {
  const p = wavePalette();
  const ppb = view.pxPerSec;
  const b0 = Math.floor(view.start) - 1;
  const b1 = Math.ceil(view.start + view.width / ppb) + 1;
  for (let b = b0; b <= b1; b++) {
    const x = Math.round(beatX(view, b));
    const phrase = b % 32 === 0;
    const bar = b % 4 === 0;
    if (phrase) ctx.fillStyle = alpha(p.fg, 0.16);
    else if (bar && ppb * 4 >= 7) ctx.fillStyle = alpha(p.fg, 0.07);
    else if (!bar && ppb >= 10) ctx.fillStyle = alpha(p.fg, 0.035);
    else continue;
    ctx.fillRect(x, y, 1, h);
  }
}

const MARKER_COLOR: [RegExp, keyof ReturnType<typeof wavePalette>][] = [
  [/brake|spinback/i, "danger"],
  [/^b in/i, "accent"],
  [/bass/i, "low"],
  [/a out/i, "fgMuted"],
  [/echo/i, "fg"],
  [/loop/i, "info"],
  [/tempo/i, "warn"],
];

export function markerColor(label: string): string {
  const p = wavePalette();
  for (const [re, k] of MARKER_COLOR) if (re.test(label)) return p[k] as string;
  return p.fgMuted;
}

/** Bar ruler: bar numbers relative to B in, phrase lines, the render window, markers as flags. */
export function paintRuler(ctx: CanvasRenderingContext2D, view: View, w: number, h: number, plan: MixPlan): void {
  const p = wavePalette();
  ctx.fillStyle = p.panel;
  ctx.fillRect(0, 0, w, h);
  // render window
  const wx0 = beatX(view, plan.window.start_beat);
  const wx1 = beatX(view, plan.window.end_beat);
  ctx.fillStyle = alpha(p.accent, 0.08);
  ctx.fillRect(wx0, 0, wx1 - wx0, h);
  ctx.fillStyle = alpha(p.accent, 0.5);
  ctx.fillRect(wx0, h - 2, wx1 - wx0, 2);
  // blend region (B in → A out)
  const bx0 = beatX(view, 0);
  const bx1 = beatX(view, plan.a.play_to_beat);
  if (bx1 > bx0 + 1) {
    ctx.fillStyle = alpha(p.accent, 0.22);
    ctx.fillRect(bx0, 0, bx1 - bx0, h - 2);
  }
  const ppb = view.pxPerSec;
  const everyBars = [1, 2, 4, 8, 16].find((n) => n * 4 * ppb >= 26) ?? 32;
  const b0 = Math.floor(view.start / 4) - 1;
  const b1 = Math.ceil((view.start + w / ppb) / 4) + 1;
  ctx.font = `600 9px ${p.font}`;
  ctx.textBaseline = "top";
  for (let bar = b0; bar <= b1; bar++) {
    const x = Math.round(beatX(view, bar * 4)) + 0.5;
    const phrase = bar % 8 === 0;
    ctx.fillStyle = alpha(p.fg, phrase ? 0.5 : 0.22);
    ctx.fillRect(x - 0.5, phrase ? 0 : h - 8, 1, phrase ? h : 6);
    if (bar % everyBars === 0) {
      const label = bar >= 0 ? String(bar + 1) : `−${-bar}`;
      ctx.fillStyle = bar >= 0 && bar * 4 < plan.a.play_to_beat ? p.fg : alpha(p.fg, phrase ? 0.75 : 0.5);
      ctx.fillText(label, x + 3, 3);
    }
  }
  if (ppb >= 10) {
    ctx.fillStyle = alpha(p.fg, 0.14);
    for (let b = Math.floor(view.start); b <= view.start + w / ppb; b++) if (b % 4) ctx.fillRect(Math.round(beatX(view, b)), h - 5, 1, 3);
  }
  // marker flags
  ctx.font = `700 9px ${p.font}`;
  ctx.textBaseline = "middle";
  let lastEnd = -Infinity;
  for (const m of [...plan.markers].sort((x, y) => x.beat - y.beat)) {
    const x = beatX(view, m.beat);
    if (x < -80 || x > w + 2) continue;
    const text = m.label.toUpperCase();
    const tw = ctx.measureText(text).width + 8;
    const col = markerColor(m.label);
    const row = x < lastEnd + 2 ? 1 : 0;
    if (row === 0) lastEnd = x + tw;
    const fy = 13 + row * 0;
    if (row === 1) continue; // overlapping flags: the line still shows in the overlay
    ctx.fillStyle = col;
    ctx.fillRect(x, fy - 1, tw, 11);
    ctx.fillStyle = p.bg;
    ctx.fillText(text, x + 4, fy + 4.5);
  }
}

/** Marker lines through every row + the hover line (overlay). */
export function paintMarkerLines(ctx: CanvasRenderingContext2D, view: View, h: number, markers: MixMarker[]): void {
  for (const m of markers) {
    const x = Math.round(beatX(view, m.beat)) + 0.5;
    if (x < 0 || x > view.width) continue;
    ctx.fillStyle = alpha(markerColor(m.label), 0.45);
    ctx.fillRect(x - 0.5, 0, 1, h);
  }
}

export interface LanePaint {
  kfs: Keyframe[];
  range: [number, number];
  color: string;
  bipolar?: boolean;
  neutral?: number;
  selected?: Set<number>;
  /** Dim outside [from, to] (deck play range). */
  play?: [number, number];
  box?: { x0: number; x1: number; y0: number; y1: number } | null;
}

export const LANE_PAD = 5;

export function valueY(v: number, range: [number, number], h: number): number {
  const [lo, hi] = range;
  return LANE_PAD + (1 - (v - lo) / (hi - lo || 1)) * (h - 2 * LANE_PAD);
}

export function yValue(y: number, range: [number, number], h: number): number {
  const [lo, hi] = range;
  return lo + (1 - (y - LANE_PAD) / Math.max(1, h - 2 * LANE_PAD)) * (hi - lo);
}

/** One automation lane: grid, neutral line, filled curve, keyframes (shape = curve type), selection, box. */
export function paintLane(ctx: CanvasRenderingContext2D, view: View, w: number, h: number, L: LanePaint, compact = false): void {
  const p = wavePalette();
  ctx.fillStyle = p.surface;
  ctx.fillRect(0, 0, w, h);
  paintGrid(ctx, view, 0, h);
  const color = resolveColor(L.color);
  const base = L.neutral != null ? valueY(L.neutral, L.range, h) : h - LANE_PAD;
  ctx.fillStyle = alpha(p.fg, 0.1);
  ctx.fillRect(0, Math.round(base), w, 1);

  // curve (sampled every 2 px; exact corners at keyframes)
  const xs: number[] = [];
  for (let x = 0; x <= w; x += 2) xs.push(x);
  for (const k of L.kfs) {
    const x = beatX(view, k.beat);
    if (x > 0 && x < w) xs.push(x - 0.01, x);
  }
  xs.sort((a, b) => a - b);
  const pts = xs.map((x) => [x, valueY(laneValue(L.kfs, view.start + x / view.pxPerSec), L.range, h)] as const);
  const fillBase = L.bipolar ? valueY(0, L.range, h) : h;
  ctx.beginPath();
  ctx.moveTo(0, fillBase);
  for (const [x, y] of pts) ctx.lineTo(x, y);
  ctx.lineTo(w, fillBase);
  ctx.closePath();
  ctx.fillStyle = alpha(color, compact ? 0.1 : 0.14);
  ctx.fill();
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();

  if (L.play) {
    const [a, b] = L.play;
    ctx.fillStyle = alpha(p.bg, 0.55);
    const xa = Math.max(0, beatX(view, a));
    const xb = Math.min(w, beatX(view, b));
    if (xa > 0) ctx.fillRect(0, 0, xa, h);
    if (xb < w) ctx.fillRect(xb, 0, w - xb, h);
  }

  // keyframes
  L.kfs.forEach((k, i) => {
    const x = beatX(view, k.beat);
    if (x < -6 || x > w + 6) return;
    const y = valueY(k.value, L.range, h);
    const sel = L.selected?.has(i);
    const r = sel ? 4.5 : 3.5;
    ctx.beginPath();
    if (k.curve === "hold") ctx.rect(x - r, y - r, r * 2, r * 2);
    else if (k.curve === "ease") {
      ctx.moveTo(x, y - r - 1);
      ctx.lineTo(x + r + 1, y);
      ctx.lineTo(x, y + r + 1);
      ctx.lineTo(x - r - 1, y);
      ctx.closePath();
    } else ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = sel ? p.fg : color;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = sel ? p.accent : p.surface;
    ctx.stroke();
  });

  if (L.box) {
    const { x0, x1, y0, y1 } = L.box;
    ctx.fillStyle = alpha(p.accent, 0.12);
    ctx.fillRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
    ctx.strokeStyle = alpha(p.accent, 0.8);
    ctx.lineWidth = 1;
    ctx.strokeRect(Math.min(x0, x1) + 0.5, Math.min(y0, y1) + 0.5, Math.abs(x1 - x0), Math.abs(y1 - y0));
  }
}

/** Deck waveform on the beat axis: loops drawn as repeated source windows, outside the play range dimmed, the
 * volume lane shading the waveform so fades are visible. */
export function paintDeck(ctx: CanvasRenderingContext2D, dpr: number, view: View, w: number, h: number, deck: DeckPlan, wave: WaveData | null, tint: string): void {
  const p = wavePalette();
  ctx.fillStyle = p.bg;
  ctx.fillRect(0, 0, w, h);
  paintGrid(ctx, view, 0, h);
  if (!wave) {
    ctx.fillStyle = alpha(p.fg, 0.35);
    ctx.font = `500 11px ${p.font}`;
    ctx.textBaseline = "middle";
    ctx.fillText("waveform loads with the analysis…", 10, h / 2);
    return;
  }
  const spb = 60 / (deck.native_bpm || 120);
  const lane = (origin: number): Lane => ({ wave, an: null, origin, rate: spb });
  const base = lane(-deck.beat0_s / spb);
  const loops = [...deck.loops].sort((x, y) => x.start_beat - y.start_beat);
  const clip = (b0: number, b1: number, fn: () => void) => {
    const x0 = Math.max(0, beatX(view, b0));
    const x1 = Math.min(w, beatX(view, b1));
    if (x1 <= x0) return;
    ctx.save();
    ctx.beginPath();
    ctx.rect(x0, 0, x1 - x0, h);
    ctx.clip();
    fn();
    ctx.restore();
  };
  // un-looped stretches
  let cursor = -1e6;
  for (const l of loops) {
    clip(cursor, l.start_beat, () => drawWave(ctx, base, view, 2, h - 4, dpr));
    cursor = l.end_beat;
  }
  clip(cursor, 1e6, () => drawWave(ctx, base, view, 2, h - 4, dpr));
  // loops: repeat the source window
  for (const l of loops) {
    if (l.length_beats <= 0) continue;
    const reps = Math.ceil((l.end_beat - l.start_beat) / l.length_beats);
    for (let k = 0; k < reps; k++) {
      const s = l.start_beat + k * l.length_beats;
      const e = Math.min(l.end_beat, s + l.length_beats);
      clip(s, e, () => drawWave(ctx, lane(s - l.source_beat - deck.beat0_s / spb), view, 2, h - 4, dpr));
      const x = Math.round(beatX(view, s)) + 0.5;
      ctx.fillStyle = alpha(p.info, k === 0 ? 0.9 : 0.45);
      ctx.fillRect(x - 0.5, 0, 1, h);
    }
    const x0 = beatX(view, l.start_beat);
    const x1 = beatX(view, l.end_beat);
    ctx.fillStyle = alpha(p.info, 0.08);
    ctx.fillRect(x0, 0, x1 - x0, h);
    ctx.fillStyle = alpha(p.info, 0.9);
    ctx.fillRect(x0, 0, x1 - x0, 2);
  }
  // turntable FX: the effect's stretch tinted and labelled (the deck is silent after it)
  ctx.font = `700 9px ${p.font}`;
  ctx.textBaseline = "top";
  for (const f of deck.fx ?? []) {
    const x0 = beatX(view, f.beat);
    const x1 = beatX(view, f.beat + f.beats);
    if (x1 < 0 || x0 > w) continue;
    const g = ctx.createLinearGradient(x0, 0, x1, 0);
    g.addColorStop(0, alpha(p.danger, 0.05));
    g.addColorStop(1, alpha(p.danger, 0.32));
    ctx.fillStyle = g;
    ctx.fillRect(x0, 0, x1 - x0, h);
    ctx.fillStyle = alpha(p.danger, 0.95);
    ctx.fillRect(x0, 0, x1 - x0, 2);
    const text = f.kind === "brake" ? "BRAKE" : "SPINBACK";
    const tw = ctx.measureText(text).width;
    if (x1 - x0 > tw + 6) {
      ctx.fillStyle = alpha(p.bg, 0.75);
      ctx.fillRect(x0 + 2, 4, tw + 6, 12);
      ctx.fillStyle = p.danger;
      ctx.fillText(text, x0 + 5, 5.5);
    }
  }
  // volume shading (fader position) — every 2 px
  const vol = deck.lanes.volume;
  for (let x = 0; x < w; x += 2) {
    const beat = view.start + (x + 1) / view.pxPerSec;
    const outside = beat < deck.play_from_beat || beat > deck.play_to_beat;
    const v = outside ? 0 : laneValue(vol, beat);
    const a = outside ? 0.78 : (1 - Math.max(0, Math.min(1, v))) * 0.7;
    if (a > 0.01) {
      ctx.fillStyle = alpha(p.bg, a);
      ctx.fillRect(x, 0, 2, h);
    }
  }
  // play range edges
  for (const b of [deck.play_from_beat, deck.play_to_beat]) {
    const x = Math.round(beatX(view, b)) + 0.5;
    if (x < 0 || x > w) continue;
    ctx.fillStyle = alpha(tint, 0.9);
    ctx.fillRect(x - 0.5, 0, 1.5, h);
  }
}

/** Loop regions row. */
export function paintLoops(ctx: CanvasRenderingContext2D, view: View, w: number, h: number, deck: DeckPlan, selected: number | null): void {
  const p = wavePalette();
  ctx.fillStyle = p.surface;
  ctx.fillRect(0, 0, w, h);
  paintGrid(ctx, view, 0, h);
  ctx.font = `600 9px ${p.font}`;
  ctx.textBaseline = "middle";
  deck.loops.forEach((l, i) => {
    const x0 = beatX(view, l.start_beat);
    const x1 = beatX(view, l.end_beat);
    if (x1 < 0 || x0 > w) return;
    const sel = selected === i;
    ctx.fillStyle = alpha(p.info, sel ? 0.4 : 0.22);
    ctx.fillRect(x0 + 0.5, 2, x1 - x0 - 1, h - 4);
    ctx.strokeStyle = sel ? p.fg : alpha(p.info, 0.9);
    ctx.lineWidth = sel ? 1.5 : 1;
    ctx.strokeRect(x0 + 0.5, 2.5, x1 - x0 - 1, h - 5);
    for (let b = l.start_beat + l.length_beats; b < l.end_beat - 1e-6; b += l.length_beats) {
      const x = Math.round(beatX(view, b)) + 0.5;
      ctx.fillStyle = alpha(p.info, 0.6);
      ctx.fillRect(x - 0.5, 4, 1, h - 8);
    }
    const text = `LOOP ${l.length_beats} beat${l.length_beats === 1 ? "" : "s"}`;
    if (x1 - x0 > ctx.measureText(text).width + 10) {
      ctx.fillStyle = p.fg;
      ctx.fillText(text, Math.max(x0, 0) + 5, h / 2 + 0.5);
    }
  });
}
