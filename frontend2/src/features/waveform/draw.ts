/**
 * Canvas painters for 3-band waveforms and their overlays. Everything is drawn on a shared time axis ("mix time"):
 * a Lane places one track on it (origin = mix time of the file's 0 s, rate = file seconds per mix second, i.e.
 * 1 + pitch%/100 for a pitched deck B). A View maps mix time → CSS px. Callers scale the context by the DPR.
 */
import type { CuePoint, TrackAnalysis } from "@/api/types";
import { textPx } from "@/lib/theme";
import { waveGL } from "./gl/renderer";
import { alpha, cueColor, edgeColor, edgeWords, rgbOf, SECTION_LABEL, sectionColor, type WavePalette, wavePalette } from "./palette";
import type { WaveData } from "./prepare";
import { useWaveSettings, type WaveMode } from "./settings";

export interface Lane {
  wave: WaveData | null;
  an: TrackAnalysis | null;
  origin: number;
  rate: number;
}

export interface View {
  start: number; // mix time at x = 0, s
  pxPerSec: number; // CSS px per mix second
  width: number; // CSS px
}

export const laneOf = (an: TrackAnalysis | null, wave: WaveData | null, origin = 0, rate = 1): Lane => ({ an, wave, origin, rate });

export const toMix = (lane: Lane, t: number) => lane.origin + t / lane.rate;
export const toFile = (lane: Lane, m: number) => (m - lane.origin) * lane.rate;
export const xOf = (view: View, m: number) => (m - view.start) * view.pxPerSec;
export const mixAt = (view: View, x: number) => view.start + x / view.pxPerSec;

function font(p: WavePalette, size: number, weight = 600): string {
  return `${weight} ${textPx(size)}px ${p.font}`;
}

// ------------------------------------------------------------------------------------------------ waveform

export interface WaveOptions {
  alpha?: number;
  /** defaults to the Settings choice */
  mode?: WaveMode;
  /** file time of the playhead: the part before it draws brighter, the part after it dimmer */
  played?: number | null;
}

/** Brightness of the unplayed part while a playhead splits the waveform (the shader uses the same). */
export const UNPLAYED = 0.55;

/**
 * The waveform of `lane` in the band [y, y + h], in the Settings mode: on the GPU (`gl/renderer.ts`, one shared
 * WebGL2 context) when it can, else painted with Canvas 2D (`drawWave2d`). Either way it lands on `ctx`, so callers
 * layer grid lines under it and cues over it as before.
 */
export function drawWave(ctx: CanvasRenderingContext2D, lane: Lane, view: View, y: number, h: number, dpr: number, opts: WaveOptions = {}): void {
  const w = lane.wave;
  if (!w || h <= 0 || view.width <= 0) return;
  const mode = opts.mode ?? useWaveSettings.getState().mode;
  const gl = waveGL();
  if (gl) {
    const ptsPerMix = w.pps * lane.rate;
    const y0 = Math.round(y * dpr);
    const split = opts.played == null ? Infinity : Math.max(0, xOf(view, toMix(lane, opts.played)) * dpr);
    const ok = gl.drawInto(
      ctx,
      {
        wave: w,
        first: (view.start - lane.origin) * ptsPerMix,
        ppc: ptsPerMix / (view.pxPerSec * dpr),
        width: Math.ceil(view.width * dpr),
        height: Math.round((y + h) * dpr) - y0,
        dpr,
        mode,
        split,
        alpha: opts.alpha ?? 1,
      },
      0,
      y0 / dpr,
    );
    if (ok) return;
  }
  drawWave2d(ctx, lane, view, y, h, dpr, { ...opts, mode });
}

const BAND_GAIN = { low: 1, mid: 0.64, high: 0.34 };
// shaping curves: mids are dense in most mixes, so compress them harder to let kicks (lows) show around them
const CURVE = { low: 0.85, mid: 1.6, high: 1.25 };

/** Per device-pixel column of the view, each band's peak over the points the column covers, or interpolated between
 * points when zoomed past one point per pixel. Only the columns inside the file: [c0, c1). */
function waveColumns(lane: Lane, view: View, dpr: number) {
  const w = lane.wave!;
  const cols = Math.ceil(view.width * dpr);
  const step = 1 / dpr; // CSS px per column
  const ptsPerMix = w.pps * lane.rate; // waveform points per mix second
  const ptsPerCol = (ptsPerMix * step) / view.pxPerSec;
  const first = (view.start - lane.origin) * ptsPerMix; // point index at x = 0
  const c0 = Math.max(0, Math.floor(xOf(view, lane.origin) / step));
  const c1 = Math.min(cols, Math.ceil(xOf(view, toMix(lane, w.n / w.pps)) / step));
  const len = Math.max(0, c1 - c0);
  const lo = new Float32Array(len);
  const md = new Float32Array(len);
  const hi = new Float32Array(len);
  for (let c = c0; c < c1; c++) {
    const a = first + c * ptsPerCol;
    const b = a + ptsPerCol;
    let l = 0;
    let m = 0;
    let s = 0;
    if (ptsPerCol >= 1) {
      const i0 = Math.max(0, Math.floor(a));
      const i1 = Math.min(w.n, Math.ceil(b));
      for (let i = i0; i < i1; i++) {
        if (w.low[i] > l) l = w.low[i];
        if (w.mid[i] > m) m = w.mid[i];
        if (w.high[i] > s) s = w.high[i];
      }
    } else {
      const f = (a + b) / 2 - 0.5;
      const i = Math.floor(f);
      const t = f - i;
      const i0 = Math.max(0, Math.min(w.n - 1, i));
      const i1 = Math.max(0, Math.min(w.n - 1, i + 1));
      l = w.low[i0] * (1 - t) + w.low[i1] * t;
      m = w.mid[i0] * (1 - t) + w.mid[i1] * t;
      s = w.high[i0] * (1 - t) + w.high[i1] * t;
    }
    lo[c - c0] = l;
    md[c - c0] = m;
    hi[c - c0] = s;
  }
  return { c0, c1, step, lo, md, hi };
}

/**
 * The Canvas 2D painter: the fallback when WebGL2 is unavailable, with the same modes as the shader (3-band:
 * rekordbox-style blue lows at the back, amber mids, white highs in front; RGB; stacked rows) and the same dimmer
 * unplayed part, without the gradient and glow.
 */
export function drawWave2d(ctx: CanvasRenderingContext2D, lane: Lane, view: View, y: number, h: number, dpr: number, opts: WaveOptions = {}): void {
  if (!lane.wave) return;
  const p = wavePalette();
  const { c0, c1, step, lo, md, hi } = waveColumns(lane, view, dpr);
  if (c1 <= c0) return;
  const mode = opts.mode ?? useWaveSettings.getState().mode;
  const base = opts.alpha ?? 1;

  // mirrored filled envelope of `vals` (already in px) around `mid`
  const envelope = (vals: Float32Array, mid: number, color: string) => {
    ctx.beginPath();
    ctx.moveTo(c0 * step, mid);
    for (let k = 0; k < vals.length; k++) ctx.lineTo((c0 + k) * step, mid - vals[k]);
    ctx.lineTo(c1 * step, mid);
    for (let k = vals.length - 1; k >= 0; k--) ctx.lineTo((c0 + k) * step, mid + vals[k]);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  };
  const shaped = (vals: Float32Array, gain: number, curve: number, half: number) => vals.map((v) => Math.max(0.35, Math.pow(v, curve) * gain * half));

  const paint = () => {
    if (mode === "stacked") {
      const rowH = h / 3;
      const half = rowH / 2 - 1;
      [
        [hi, 1.1, p.high],
        [md, 1.25, p.mid],
        [lo, 0.85, p.low],
      ].forEach(([vals, curve, color], r) => envelope(shaped(vals as Float32Array, 1, curve as number, half), y + rowH * (r + 0.5), color as string));
    } else if (mode === "rgb") {
      const half = h / 2 - 0.5;
      const mid = y + h / 2;
      const [rl, rm, rh] = [rgbOf(p.rgbLow), rgbOf(p.rgbMid), rgbOf(p.rgbHigh)];
      for (let k = 0; k < lo.length; k++) {
        const wl = lo[k] ** 3 + 1e-4;
        const wm = (md[k] * 0.72) ** 3 + 1e-4;
        const wh = (hi[k] * 0.58) ** 3 + 1e-4;
        const sum = wl + wm + wh;
        const rgb = [0, 1, 2].map((i) => (wl * rl[i] + wm * rm[i] + wh * rh[i]) / sum);
        const top = Math.max(0.4, ...rgb);
        const amp = Math.max(0.35, Math.pow(Math.max(lo[k], md[k] * 0.9, hi[k] * 0.75), 0.9) * half);
        ctx.fillStyle = `rgb(${rgb.map((v) => Math.round((v / top) * 255)).join(", ")})`;
        ctx.fillRect((c0 + k) * step, mid - amp, step, amp * 2);
      }
    } else {
      const half = h / 2 - 0.5;
      const mid = y + h / 2;
      envelope(shaped(lo, BAND_GAIN.low, CURVE.low, half), mid, alpha(p.low, 1));
      envelope(shaped(md, BAND_GAIN.mid, CURVE.mid, half), mid, alpha(p.mid, 0.92));
      envelope(shaped(hi, BAND_GAIN.high, CURVE.high, half), mid, alpha(p.high, 0.88));
    }
  };

  const split = opts.played == null ? null : Math.max(0, xOf(view, toMix(lane, opts.played)));
  const region = (x0: number, x1: number, a: number) => {
    if (x1 <= x0) return;
    ctx.save();
    ctx.beginPath();
    ctx.rect(x0, y - 1, x1 - x0, h + 2);
    ctx.clip();
    ctx.globalAlpha = a;
    paint();
    ctx.restore();
  };
  if (split == null) region(0, view.width, base);
  else {
    region(0, Math.min(split, view.width), base);
    region(split, view.width, base * UNPLAYED);
  }
}

// ------------------------------------------------------------------------------------------------ grid

export interface GridOptions {
  labels?: boolean; // bar numbers at phrase starts (top of the band)
  labelY?: number;
}

/** Beat / bar / 8-bar phrase lines (phrases start at bars phrase_offset + 8k); density adapts to the zoom. */
export function drawGrid(ctx: CanvasRenderingContext2D, lane: Lane, view: View, y: number, h: number, opts: GridOptions = {}): void {
  const an = lane.an;
  if (!an || !(an.bar_s > 0)) return;
  const p = wavePalette();
  const barPx = (an.bar_s / lane.rate) * view.pxPerSec;
  const fd = an.first_downbeat_s;
  const k0 = Math.max(0, Math.floor((toFile(lane, view.start) - fd) / an.bar_s) - 1);
  const k1 = Math.min(an.bar_count, Math.ceil((toFile(lane, mixAt(view, view.width)) - fd) / an.bar_s) + 1);
  const off = ((an.phrase_offset % 8) + 8) % 8;
  const isPhrase = (k: number) => (k - off) % 8 === 0;
  ctx.save();
  // beats
  if (barPx / 4 >= 9) {
    ctx.fillStyle = alpha(p.fg, 0.07);
    for (let k = k0; k < k1; k++) {
      for (let b = 1; b < 4; b++) {
        const x = xOf(view, toMix(lane, fd + (k + b / 4) * an.bar_s));
        if (x >= -1 && x <= view.width + 1) ctx.fillRect(Math.round(x), y, 1, h);
      }
    }
  }
  for (let k = k0; k <= k1; k++) {
    const x = Math.round(xOf(view, toMix(lane, fd + k * an.bar_s)));
    if (x < -1 || x > view.width + 1) continue;
    if (isPhrase(k) && k >= off) {
      if (barPx * 8 < 6) continue;
      ctx.fillStyle = alpha(p.phrase, 0.3);
      ctx.fillRect(x, y, 1, h);
    } else if (barPx >= 6) {
      ctx.fillStyle = alpha(p.fg, barPx >= 18 ? 0.15 : 0.09);
      ctx.fillRect(x, y, 1, h);
    }
  }
  if (opts.labels) {
    ctx.font = font(p, 10, 600);
    ctx.textBaseline = "top";
    const every = [1, 2, 4, 8, 16, 32, 64].find((n) => barPx * n >= 30);
    if (every) {
      for (let k = k0; k < Math.min(k1 + 1, an.bar_count); k++) {
        if ((((k - off) % every) + every) % every !== 0) continue;
        const x = xOf(view, toMix(lane, fd + k * an.bar_s));
        if (x < -20 || x > view.width) continue;
        ctx.fillStyle = isPhrase(k) ? alpha(p.phrase, 0.72) : alpha(p.fg, 0.42);
        ctx.fillText(String(k + 1), x + 3, opts.labelY ?? y + 2);
      }
    }
  }
  ctx.restore();
}

/** A span of the track marked on a view. */
export interface WaveRegion {
  start_s: number;
  end_s: number;
  label?: string;
}

/** Every other phrase tinted, so the eye can count phrases as they pass. Under the grid. */
export function drawPhraseBands(ctx: CanvasRenderingContext2D, lane: Lane, view: View, y: number, h: number): void {
  const an = lane.an;
  if (!an || !(an.bar_s > 0)) return;
  const starts = an.phrases?.length ? an.phrases.map((ph) => ph.bar) : null;
  const off = ((an.phrase_offset % 8) + 8) % 8;
  const barAt = (i: number): number => (starts ? (starts[i] ?? an.bar_count) : off + i * 8);
  const count = starts ? starts.length : Math.ceil((an.bar_count - off) / 8);
  const xOfBar = (bar: number) => xOf(view, toMix(lane, an.first_downbeat_s + bar * an.bar_s));
  ctx.fillStyle = alpha(wavePalette().phrase, 0.09);
  for (let i = 1; i < count; i += 2) {
    const x0 = xOfBar(barAt(i));
    const x1 = xOfBar(Math.min(barAt(i + 1), an.bar_count));
    if (x1 < 0 || x0 > view.width) continue;
    ctx.fillRect(Math.round(x0), y, Math.round(x1) - Math.round(x0), h);
  }
}

/** Marked spans: a tint between two edges, and the label at the first edge. */
export function drawRegions(ctx: CanvasRenderingContext2D, regions: WaveRegion[], view: View, y: number, h: number): void {
  const p = wavePalette();
  ctx.save();
  ctx.font = font(p, 10, 600);
  ctx.textBaseline = "top";
  for (const r of regions) {
    const x0 = Math.round(xOf(view, r.start_s));
    const x1 = Math.round(xOf(view, r.end_s));
    if (x1 < 0 || x0 > view.width) continue;
    ctx.fillStyle = alpha(p.cueMix, 0.12);
    ctx.fillRect(x0, y, Math.max(1, x1 - x0), h);
    ctx.fillStyle = alpha(p.cueMix, 0.8);
    ctx.fillRect(x0, y, 1, h);
    ctx.fillRect(x1, y, 1, h);
    // the label stays on screen while its region does
    if (r.label) ctx.fillText(r.label, Math.max(x0, 0) + 4, y + 16); // under the labels of intros and outros
  }
  ctx.restore();
}

// ------------------------------------------------------------------------------------------------ sections

/** Section strip: tinted blocks with a coloured top rule and a label when it fits. */
export function drawSections(ctx: CanvasRenderingContext2D, lane: Lane, view: View, y: number, h: number): void {
  const an = lane.an;
  if (!an?.sections?.length) return;
  const p = wavePalette();
  ctx.save();
  ctx.font = font(p, 10, 650);
  ctx.textBaseline = "middle";
  for (const s of an.sections) {
    const x0 = xOf(view, toMix(lane, s.start_s));
    const x1 = xOf(view, toMix(lane, s.end_s));
    if (x1 < 0 || x0 > view.width) continue;
    const col = sectionColor(s.kind, p);
    const a = Math.max(x0, 0);
    const b = Math.min(x1, view.width);
    ctx.fillStyle = alpha(col, 0.16);
    ctx.fillRect(a + 0.5, y, Math.max(0, b - a - 1), h);
    ctx.fillStyle = alpha(col, 0.85);
    ctx.fillRect(a + 0.5, y, Math.max(0, b - a - 1), 2);
    const label = SECTION_LABEL[s.kind].toUpperCase();
    const bars = s.end_bar - s.start_bar;
    const full = `${label} ${bars}`;
    const tw = ctx.measureText(full).width;
    const lx = Math.max(a + 4, Math.min(x0 + 4, b - tw - 4));
    if (b - a >= tw + 8) {
      ctx.fillStyle = alpha(p.fg, 0.82);
      ctx.fillText(label, lx, y + h / 2 + 1);
      ctx.fillStyle = alpha(p.fg, 0.45);
      ctx.fillText(` ${bars}`, lx + ctx.measureText(label).width, y + h / 2 + 1);
    } else if (b - a >= ctx.measureText(label[0]).width + 6) {
      ctx.fillStyle = alpha(p.fg, 0.7);
      ctx.fillText(label[0], a + 3, y + h / 2 + 1);
    }
  }
  ctx.restore();
}

// ------------------------------------------------------------------------------------------------ intro / outro

/** A drawn label's box in canvas px, so later layers can keep clear of it. */
export interface LabelBox {
  x0: number;
  x1: number;
  y0: number;
  y1: number;
}

/** Intro/outro brackets along the top of the band + a faint tint over the region. Returns the label boxes drawn. */
export function drawEdges(ctx: CanvasRenderingContext2D, lane: Lane, view: View, y: number, h: number, opts: { labels?: boolean } = {}): LabelBox[] {
  const an = lane.an;
  const boxes: LabelBox[] = [];
  if (!an) return boxes;
  const p = wavePalette();
  ctx.save();
  ctx.font = font(p, 10, 650);
  ctx.textBaseline = "top";
  for (const [name, e] of [["INTRO", an.intro], ["OUTRO", an.outro]] as const) {
    if (!e || e.bars <= 0 || e.end_s <= e.start_s) continue;
    const x0 = xOf(view, toMix(lane, e.start_s));
    const x1 = xOf(view, toMix(lane, e.end_s));
    if (x1 < 0 || x0 > view.width) continue;
    const col = edgeColor(e, p);
    ctx.fillStyle = alpha(col, 0.06);
    ctx.fillRect(x0, y, x1 - x0, h);
    ctx.fillStyle = alpha(col, 0.9);
    ctx.fillRect(x0, y, x1 - x0, 2);
    ctx.fillRect(x0, y, 1, 6);
    ctx.fillRect(x1 - 1, y, 1, 6);
    if (opts.labels !== false) {
      const text = `${name} ${e.bars} · ${edgeWords(e)}`;
      const tw = ctx.measureText(text).width;
      if (x1 - x0 >= tw + 10) {
        const lx = name === "INTRO" ? Math.max(x0 + 4, 4) : Math.min(x1 - tw - 4, view.width - tw - 4);
        ctx.fillStyle = alpha(p.bg, 0.72);
        ctx.fillRect(lx - 3, y + 3, tw + 6, 13);
        ctx.fillStyle = col;
        ctx.fillText(text, lx, y + 4.5);
        boxes.push({ x0: lx - 3, x1: lx + tw + 3, y0: y + 3, y1: y + 16 });
      }
    }
  }
  ctx.restore();
  return boxes;
}

// ------------------------------------------------------------------------------------------------ cues

/**
 * Cues merged for display: cues at the same moment (e.g. "first downbeat" and "mix in (16-bar intro)" both at bar 0)
 * become one, labels de-duplicated, and a bare "first downbeat" is dropped when something more useful sits there.
 */
export function mergeCues(cues: CuePoint[]): CuePoint[] {
  const out: { bar: number; time_s: number; labels: string[] }[] = [];
  for (const c of [...cues].sort((a, b) => a.time_s - b.time_s)) {
    const last = out[out.length - 1];
    if (last && (Math.abs(last.time_s - c.time_s) < 0.06 || last.bar === c.bar)) {
      if (!last.labels.includes(c.label)) last.labels.push(c.label);
    } else out.push({ bar: c.bar, time_s: c.time_s, labels: [c.label] });
  }
  return out.map((g) => {
    const labels = g.labels.length > 1 ? g.labels.filter((l) => l !== "first downbeat") : g.labels;
    return { bar: g.bar, time_s: g.time_s, label: labels.join(" · ") };
  });
}

interface CueMark {
  x: number;
  label: string;
  color: string;
}

const CUE_ROW_H = 15;
const CUE_LABEL_H = 13;

/**
 * Row layout for cue labels: `rows[i]` holds the [x0, x1] spans taken in row i. Seeded with the boxes of labels drawn
 * earlier (e.g. "INTRO 16") so cue labels keep clear of them.
 */
export function cueLabelRows(boxes: LabelBox[], top: number, rows = 2): [number, number][][] {
  return Array.from({ length: rows }, (_, i) => {
    const y0 = top + 7 + i * CUE_ROW_H;
    return boxes.filter((b) => b.y0 < y0 + CUE_LABEL_H && b.y1 > y0).map((b): [number, number] => [b.x0, b.x1]);
  });
}

/** First row with room for a label over [x0, x1] (2px gap), or -1. Claims the span in that row. */
export function placeCueLabel(rows: [number, number][][], x0: number, x1: number): number {
  const row = rows.findIndex((spans) => spans.every(([a, b]) => x1 + 2 <= a || x0 >= b + 2));
  if (row >= 0) rows[row].push([x0, x1]);
  return row;
}

/**
 * Cue markers: a line through the band and a labelled flag at the top (merged when they coincide, two rows max).
 * `avoid`: label boxes already drawn (from `drawEdges`) that cue labels must not cover.
 */
export function drawCues(ctx: CanvasRenderingContext2D, lane: Lane, view: View, y: number, h: number, opts: { labels?: boolean; topOffset?: number; avoid?: LabelBox[] } = {}): void {
  const an = lane.an;
  if (!an?.cues?.length) return;
  const p = wavePalette();
  const marks: CueMark[] = [];
  for (const c of mergeCues(an.cues)) {
    const x = xOf(view, toMix(lane, c.time_s));
    if (x < -120 || x > view.width + 2) continue;
    const last = marks[marks.length - 1];
    if (last && Math.abs(last.x - x) < 3) {
      for (const l of c.label.split(" · ")) if (!last.label.split(" · ").includes(l)) last.label += ` · ${l}`;
      continue;
    }
    marks.push({ x, label: c.label, color: cueColor(c.label, p) });
  }
  ctx.save();
  ctx.font = font(p, 10, 600);
  ctx.textBaseline = "middle";
  const top = y + (opts.topOffset ?? 0);
  const rows = cueLabelRows(opts.avoid ?? [], top);
  for (const m of marks) {
    const x = Math.round(m.x) + 0.5;
    ctx.strokeStyle = alpha(m.color, 0.75);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, y + h);
    ctx.stroke();
    // flag triangle
    ctx.fillStyle = m.color;
    ctx.beginPath();
    ctx.moveTo(x - 4, top);
    ctx.lineTo(x + 4, top);
    ctx.lineTo(x, top + 5);
    ctx.closePath();
    ctx.fill();
    if (opts.labels === false) continue;
    const tw = ctx.measureText(m.label).width + 8;
    const lx = Math.min(x, view.width - tw);
    if (lx < 0) continue;
    const row = placeCueLabel(rows, lx, lx + tw);
    if (row < 0) continue;
    const ly = top + 7 + row * CUE_ROW_H;
    ctx.fillStyle = alpha(p.bg, 0.8);
    ctx.fillRect(lx, ly, tw, CUE_LABEL_H);
    ctx.fillStyle = m.color;
    ctx.fillRect(lx, ly, 2, CUE_LABEL_H);
    ctx.fillStyle = alpha(p.fg, 0.9);
    ctx.fillText(m.label, lx + 5, ly + CUE_LABEL_H / 2);
  }
  ctx.restore();
}

// ------------------------------------------------------------------------------------------------ helpers

/** Dims [m0, m1] (mix time) in the band: parts of a deck that aren't heard. */
export function shade(ctx: CanvasRenderingContext2D, view: View, m0: number, m1: number, y: number, h: number, a = 0.62): void {
  const x0 = Math.max(0, xOf(view, m0));
  const x1 = Math.min(view.width, xOf(view, m1));
  if (x1 <= x0) return;
  ctx.fillStyle = alpha(wavePalette().bg, a);
  ctx.fillRect(x0, y, x1 - x0, h);
}

/** Pick a "nice" tick interval (s) so ticks are at least `minPx` apart. */
export function niceStep(pxPerSec: number, minPx: number): number {
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  for (const s of steps) if (s * pxPerSec >= minPx) return s;
  return 600;
}

/** Time ruler ticks along [y, y+h] labelled with `label(m)` for mix time m. */
export function drawTimeRuler(ctx: CanvasRenderingContext2D, view: View, y: number, h: number, label: (m: number) => string, opts: { from?: number; to?: number } = {}): void {
  const p = wavePalette();
  const step = niceStep(view.pxPerSec, 64);
  const m0 = Math.max(opts.from ?? -Infinity, Math.floor(view.start / step) * step);
  const m1 = Math.min(opts.to ?? Infinity, mixAt(view, view.width));
  ctx.save();
  ctx.font = font(p, 10, 500);
  ctx.textBaseline = "middle";
  for (let m = m0; m <= m1; m += step) {
    const x = Math.round(xOf(view, m)) + 0.5;
    ctx.fillStyle = alpha(p.fg, 0.22);
    ctx.fillRect(x - 0.5, y + h - 4, 1, 4);
    ctx.fillStyle = alpha(p.fg, 0.5);
    ctx.fillText(label(m), x + 3, y + h / 2 - 1);
  }
  ctx.restore();
}

/** Prepares a canvas for drawing at the device pixel ratio; returns the context scaled to CSS px (or null). */
export function setupCanvas(canvas: HTMLCanvasElement, width: number, height: number): { ctx: CanvasRenderingContext2D; dpr: number } | null {
  const dpr = Math.min(3, window.devicePixelRatio || 1);
  const W = Math.max(1, Math.round(width * dpr));
  const H = Math.max(1, Math.round(height * dpr));
  if (canvas.width !== W) canvas.width = W;
  if (canvas.height !== H) canvas.height = H;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, dpr };
}

/** "1:02.4" style precise time for readouts. */
export function fmtPrecise(s: number): string {
  if (!Number.isFinite(s)) return "–:––";
  const neg = s < 0;
  const a = Math.abs(s);
  const m = Math.floor(a / 60);
  const sec = a - m * 60;
  return `${neg ? "−" : ""}${m}:${sec.toFixed(1).padStart(4, "0")}`;
}

/** Bar.beat position of file time t (1-based like DJ software), or null without a grid. */
export function barBeat(an: TrackAnalysis | null, t: number): string | null {
  if (!an || !(an.bar_s > 0)) return null;
  const rel = (t - an.first_downbeat_s) / an.bar_s;
  const bar = Math.floor(rel);
  const beat = Math.floor((rel - bar) * 4);
  return `${bar + 1}.${beat + 1}`;
}
