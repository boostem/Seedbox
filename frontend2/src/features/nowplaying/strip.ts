/** The canvases of a deck's panel: stem meters and the plan's EQ, filter and fader. They paint from the view's frame loop. */
import { alpha, type WavePalette } from "@/features/waveform/palette";
import type { Strip } from "./session";

const LABEL_H = 13;

/** Meters fall this fast, in levels per second, as a hardware meter does; they rise at once. */
const RELEASE = 2.4;

/** Follows `target` up at once and falls at `RELEASE`, in place. */
export function ballistics(levels: Float32Array, target: ArrayLike<number>, dt: number): void {
  for (let i = 0; i < levels.length; i++) levels[i] = Math.max(target[i] ?? 0, levels[i] - RELEASE * dt);
}

export function drawMeters(ctx: CanvasRenderingContext2D, levels: ArrayLike<number>, names: string[], colours: string[], w: number, h: number, p: WavePalette): void {
  ctx.clearRect(0, 0, w, h);
  const n = names.length;
  const gap = 6;
  const barW = (w - gap * (n - 1)) / n;
  const barH = h - LABEL_H;
  ctx.font = `600 10px ${p.font}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  for (let i = 0; i < n; i++) {
    const x = i * (barW + gap);
    ctx.fillStyle = alpha(p.fg, 0.07);
    ctx.fillRect(x, 0, barW, barH);
    const fill = Math.round(Math.min(1, levels[i]) * barH);
    ctx.fillStyle = colours[i];
    ctx.fillRect(x, barH - fill, barW, fill);
    ctx.fillStyle = alpha(p.fg, 0.55);
    ctx.fillText(names[i], x + barW / 2, h);
  }
  ctx.textAlign = "start";
}

const ARC_FROM = Math.PI * 0.75;
const ARC_SWEEP = Math.PI * 1.5;

function knob(ctx: CanvasRenderingContext2D, cx: number, cy: number, r: number, value: number, rest: number, label: string, killed: boolean, p: WavePalette): void {
  const angle = (v: number) => ARC_FROM + Math.max(0, Math.min(1, v)) * ARC_SWEEP;
  ctx.lineCap = "round";
  ctx.lineWidth = 3;
  ctx.strokeStyle = alpha(p.fg, 0.12);
  ctx.beginPath();
  ctx.arc(cx, cy, r, ARC_FROM, ARC_FROM + ARC_SWEEP);
  ctx.stroke();
  if (Math.abs(value - rest) > 0.004) {
    ctx.strokeStyle = killed ? p.stem.drums : p.playhead;
    ctx.beginPath();
    ctx.arc(cx, cy, r, Math.min(angle(value), angle(rest)), Math.max(angle(value), angle(rest)));
    ctx.stroke();
  }
  const a = angle(value);
  ctx.strokeStyle = alpha(p.fg, 0.85);
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(cx + Math.cos(a) * (r * 0.35), cy + Math.sin(a) * (r * 0.35));
  ctx.lineTo(cx + Math.cos(a) * (r * 0.85), cy + Math.sin(a) * (r * 0.85));
  ctx.stroke();
  ctx.fillStyle = alpha(p.fg, 0.55);
  ctx.fillText(label, cx, cy + r + 13);
}

const EQ_MIN = -30;
const EQ_MAX = 6;
const eqPos = (db: number) => (db - EQ_MIN) / (EQ_MAX - EQ_MIN);

/** Low, mid, high, filter and the fader, at the plan's values now. */
export function drawStrip(ctx: CanvasRenderingContext2D, s: Strip, w: number, h: number, p: WavePalette): void {
  ctx.clearRect(0, 0, w, h);
  ctx.font = `600 10px ${p.font}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  const faderW = 18;
  const r = Math.min(14, (w - faderW - 12) / 8 - 2);
  const cell = (w - faderW - 12) / 4;
  const cy = r + 3;
  const rest = eqPos(0);
  knob(ctx, cell * 0.5, cy, r, eqPos(s.eqLow), rest, "LOW", s.eqLow <= -29, p);
  knob(ctx, cell * 1.5, cy, r, eqPos(s.eqMid), rest, "MID", s.eqMid <= -29, p);
  knob(ctx, cell * 2.5, cy, r, eqPos(s.eqHigh), rest, "HI", s.eqHigh <= -29, p);
  knob(ctx, cell * 3.5, cy, r, (s.filter + 1) / 2, 0.5, s.filter < -0.01 ? "LPF" : s.filter > 0.01 ? "HPF" : "FILT", false, p);
  const x = w - faderW;
  const barH = h - LABEL_H;
  ctx.fillStyle = alpha(p.fg, 0.07);
  ctx.fillRect(x, 0, faderW, barH);
  const fill = Math.round(s.volume * barH);
  ctx.fillStyle = alpha(p.fg, 0.7);
  ctx.fillRect(x, barH - fill, faderW, fill);
  ctx.fillStyle = alpha(p.fg, 0.55);
  ctx.fillText("VOL", x + faderW / 2, h);
  ctx.textAlign = "start";
}
