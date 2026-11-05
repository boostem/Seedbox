/**
 * Paints a stem waveform onto a view's canvas: on the GPU when it can (`gl/renderer.ts`), else with Canvas 2D, with the
 * same layers and brightness but without the lanes' gradient and glow.
 */
import { type Lane, toMix, UNPLAYED, type View, xOf } from "./draw";
import { waveGL } from "./gl/renderer";
import { rgbOf, wavePalette } from "./palette";
import { STEMS, stemBrightness, stemShape, stemStack, type StemView, type StemWave } from "./stems";

export interface StemOptions {
  view: StemView;
  /** linear gain of each stem now (drums, bass, vocals, other); all 1 when not given */
  gains?: ArrayLike<number> | null;
  /** file time of the playhead: the part before it draws brighter, the part after it dimmer */
  played?: number | null;
  alpha?: number;
}

const ALL_ON = [1, 1, 1, 1];
const bright = new Float32Array(4);

export function drawStems(ctx: CanvasRenderingContext2D, stems: StemWave, lane: Lane, view: View, y: number, h: number, dpr: number, opts: StemOptions): void {
  if (h <= 0 || view.width <= 0) return;
  const gains = opts.gains ?? ALL_ON;
  for (let i = 0; i < 4; i++) bright[i] = stemBrightness(gains[i]);
  const gl = waveGL();
  if (gl) {
    const ptsPerMix = stems.pps * lane.rate;
    const y0 = Math.round(y * dpr);
    const ok = gl.drawStemsInto(
      ctx,
      {
        stems,
        view: opts.view,
        bright,
        first: (view.start - lane.origin) * ptsPerMix,
        ppc: ptsPerMix / (view.pxPerSec * dpr),
        width: Math.ceil(view.width * dpr),
        height: Math.round((y + h) * dpr) - y0,
        dpr,
        split: opts.played == null ? Infinity : Math.max(0, xOf(view, toMix(lane, opts.played)) * dpr),
        alpha: opts.alpha ?? 1,
      },
      0,
      y0 / dpr,
    );
    if (ok) return;
  }
  drawStems2d(ctx, stems, lane, view, y, h, dpr, opts.view, bright, opts);
}

/** Each stem's peak over the points a device column covers, shaped, for the columns inside the file: [c0, c1). */
function stemColumns(stems: StemWave, lane: Lane, view: View, dpr: number) {
  const cols = Math.ceil(view.width * dpr);
  const step = 1 / dpr;
  const ptsPerMix = stems.pps * lane.rate;
  const ptsPerCol = (ptsPerMix * step) / view.pxPerSec;
  const first = (view.start - lane.origin) * ptsPerMix;
  const c0 = Math.max(0, Math.floor(xOf(view, lane.origin) / step));
  const c1 = Math.min(cols, Math.ceil(xOf(view, toMix(lane, stems.n / stems.pps)) / step));
  const len = Math.max(0, c1 - c0);
  const peaks = STEMS.map((name) => {
    const v = stems[name];
    const out = new Float32Array(len);
    for (let c = c0; c < c1; c++) {
      const a = first + c * ptsPerCol;
      const i0 = Math.max(0, Math.floor(a));
      const i1 = Math.max(i0 + 1, Math.min(stems.n, Math.ceil(a + ptsPerCol)));
      let m = 0;
      for (let i = i0; i < i1 && i < stems.n; i++) if (v[i] > m) m = v[i];
      out[c - c0] = stemShape(m);
    }
    return out;
  });
  return { c0, c1, step, peaks };
}

const colour = (rgb: Float32Array, b: number) => `rgb(${Math.round(rgb[0] * b * 255)}, ${Math.round(rgb[1] * b * 255)}, ${Math.round(rgb[2] * b * 255)})`;

export function drawStems2d(ctx: CanvasRenderingContext2D, stems: StemWave, lane: Lane, view: View, y: number, h: number, dpr: number, mode: StemView, light: ArrayLike<number>, opts: Pick<StemOptions, "played" | "alpha">): void {
  const p = wavePalette();
  const { c0, c1, step, peaks } = stemColumns(stems, lane, view, dpr);
  if (c1 <= c0) return;
  const colours = STEMS.map((name, i) => colour(rgbOf(p.stem[name]), light[i]));
  const paint = () => {
    if (mode === "lanes") {
      const rowH = h / 4;
      const half = rowH / 2 - 1;
      peaks.forEach((vals, r) => {
        const mid = y + rowH * (r + 0.5);
        ctx.beginPath();
        for (let k = 0; k < vals.length; k++) {
          const a = Math.max(0.35, vals[k] * half);
          ctx.rect((c0 + k) * step, mid - a, step, a * 2);
        }
        ctx.fillStyle = colours[r];
        ctx.fill();
      });
      return;
    }
    const half = h / 2 - 0.5;
    const mid = y + h / 2;
    // from the centre outwards, as the shader layers them: bass, drums, other, vocals (indices into STEMS)
    const paths = [new Path2D(), new Path2D(), new Path2D(), new Path2D()];
    for (let k = 0; k < peaks[0].length; k++) {
      const s = stemStack(peaks[0][k], peaks[1][k], peaks[2][k], peaks[3][k]);
      let at = 0;
      for (const i of [1, 0, 3, 2]) {
        const a = s[i] * half;
        if (a > 0) {
          paths[i].rect((c0 + k) * step, mid - at - a, step, a);
          paths[i].rect((c0 + k) * step, mid + at, step, a);
        }
        at += a;
      }
    }
    paths.forEach((path, i) => {
      ctx.fillStyle = colours[i];
      ctx.fill(path);
    });
  };
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
  const base = opts.alpha ?? 1;
  const split = opts.played == null ? null : Math.max(0, xOf(view, toMix(lane, opts.played)));
  if (split == null) region(0, view.width, base);
  else {
    region(0, Math.min(split, view.width), base);
    region(split, view.width, base * UNPLAYED);
  }
}
