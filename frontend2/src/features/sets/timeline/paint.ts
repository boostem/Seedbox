/**
 * The timeline, painted on one canvas at any zoom: nothing changes renderer or page between the whole set and one
 * transition, and the waveforms are the shared GL renderer's, so a zoom re-uploads nothing (`features/waveform/gl`).
 * Everything takes the view; nothing here touches React.
 */
import { drawTimeRuler, drawWave, type View, xOf } from "@/features/waveform/draw";
import { alpha, sectionColor, type WavePalette } from "@/features/waveform/palette";
import type { WaveData } from "@/features/waveform/prepare";
import { camelotMove, keyColor } from "@/lib/camelot";
import { fmtTime } from "@/lib/format";
import { textPx } from "@/lib/theme";
import type { SectionKind } from "@/api/types";
import { arcTimeAt, type Piece, type PlacedTrack, type PlacedTransition, type TimelineLayout, timeOfArc } from "./layout";

export const CAMELOT_H = 26;
export const RULER_H = 18;
export const LANE_H = 76;
export const LANE_GAP = 4;
export const LANES_TOP = CAMELOT_H + RULER_H;
export const HEIGHT = LANES_TOP + 2 * LANE_H + LANE_GAP + 4;
const SECTION_H = 3;
const LABEL_H = 15;
const CHIP_W = 28;
export const ARC_PAD = 6;

export const laneTop = (lane: number): number => LANES_TOP + lane * (LANE_H + LANE_GAP);

export interface Scene {
  layout: TimelineLayout;
  wave: (hash: string) => WaveData | null;
  /** [arc time, level], as the settings have them. */
  arc: [number, number][];
  /** What levels the energies are on: the set's quietest and most intense tracks. */
  energyRange: [number, number];
  /** A track being dragged, and the pointer's offset from where it was: it's drawn floating there. */
  drag: { position: number; dt: number; to: number } | null;
  /** The transition open in the editor. */
  open: number | null;
  /** The arc is being drawn. */
  drawing: boolean;
  /** What the pointer's over, brighter. */
  hover: { transition: number | null };
}

/** The even diagonal stripes of what isn't planned yet. */
function hatch(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, color: string): void {
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, w, h);
  ctx.clip();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.beginPath();
  const step = 8;
  for (let i = -h; i < w + h; i += step) {
    ctx.moveTo(x + i, y + h);
    ctx.lineTo(x + i + h, y);
  }
  ctx.stroke();
  ctx.restore();
}

function clippedText(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, maxWidth: number): void {
  if (maxWidth < 14) return;
  if (ctx.measureText(text).width <= maxWidth) {
    ctx.fillText(text, x, y);
    return;
  }
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(`${text.slice(0, mid)}…`).width <= maxWidth) lo = mid;
    else hi = mid - 1;
  }
  if (lo > 0) ctx.fillText(`${text.slice(0, lo)}…`, x, y);
}

/**
 * What to draw a track's waveform as. Far enough out, a pitched transition is off by less than a pixel: one draw for the
 * whole track then, as a set's worth of three each is most of what a frame of the whole set costs.
 */
export function wavePieces(track: PlacedTrack, pxPerSec: number): Piece[] {
  const { pieces } = track;
  if (pieces.length < 2) return pieces;
  const whole = { t0: pieces[0].t0, t1: pieces[pieces.length - 1].t1, f0: pieces[0].f0, f1: pieces[pieces.length - 1].f1 };
  const rate = (whole.f1 - whole.f0) / (whole.t1 - whole.t0);
  for (const p of pieces) {
    const off = (p.t0 - whole.t0) * rate - (p.f0 - whole.f0);
    if (Math.abs(off / rate) * pxPerSec > 1) return pieces;
  }
  return [whole];
}

/** One track at its place (`dt` seconds off it while it's dragged). */
function paintTrack(ctx: CanvasRenderingContext2D, p: WavePalette, scene: Scene, view: View, dpr: number, track: PlacedTrack, dt: number, faded: boolean): void {
  const top = laneTop(track.lane);
  const x0 = xOf(view, track.start + dt);
  const x1 = xOf(view, track.end + dt);
  if (x1 < 0 || x0 > view.width) return;
  const left = Math.max(0, x0);
  const right = Math.min(view.width, x1);
  ctx.globalAlpha = faded ? 0.35 : 1;
  ctx.fillStyle = alpha(p.fg, 0.05);
  ctx.fillRect(left, top, right - left, LANE_H);
  const wave = scene.wave(track.order.hash);
  const waveTop = top + LABEL_H;
  const waveH = LANE_H - LABEL_H - SECTION_H - 1;
  if (wave) {
    for (const piece of wavePieces(track, view.pxPerSec)) {
      const a = Math.max(0, Math.round(xOf(view, piece.t0 + dt) * dpr) / dpr);
      const b = Math.min(view.width, Math.round(xOf(view, piece.t1 + dt) * dpr) / dpr);
      if (b <= a) continue;
      const rate = (piece.f1 - piece.f0) / (piece.t1 - piece.t0) || 1;
      const lane = { an: null, wave, rate, origin: piece.t0 + dt - piece.f0 / rate };
      ctx.save();
      ctx.translate(a, 0);
      drawWave(ctx, lane, { start: view.start + a / view.pxPerSec, pxPerSec: view.pxPerSec, width: b - a }, waveTop, waveH, dpr, { alpha: faded ? 0.5 : 1 });
      ctx.restore();
    }
  }
  // the sections: a strip along the bottom, in the track's own colours
  const sections = track.view?.sections ?? [];
  for (const piece of track.pieces) {
    const rate = (piece.f1 - piece.f0) / (piece.t1 - piece.t0) || 1;
    for (const s of sections) {
      const from = Math.max(s.start_s, piece.f0);
      const to = Math.min(s.end_s, piece.f1);
      if (to <= from) continue;
      const sx0 = xOf(view, piece.t0 + dt + (from - piece.f0) / rate);
      const sx1 = xOf(view, piece.t0 + dt + (to - piece.f0) / rate);
      if (sx1 < 0 || sx0 > view.width) continue;
      ctx.fillStyle = alpha(sectionColor((s.kind === "main" ? "body" : s.kind) as SectionKind, p), 0.9);
      ctx.fillRect(sx0, top + LANE_H - SECTION_H, Math.max(1, sx1 - sx0 - 1), SECTION_H);
    }
  }
  // the title, kept on screen while any of the track is
  ctx.font = `600 ${textPx(11)}px ${p.font}`;
  ctx.textBaseline = "middle";
  ctx.fillStyle = p.fg;
  const name = `${track.position + 1}  ${track.order.title}${track.order.pinned ? " · pinned" : ""}`;
  clippedText(ctx, name, left + 4, top + LABEL_H / 2, right - left - 8);
  ctx.strokeStyle = alpha(p.fg, 0.28);
  ctx.lineWidth = 1;
  ctx.strokeRect(Math.round(x0) + 0.5, top + 0.5, Math.max(1, Math.round(x1 - x0) - 1), LANE_H - 1);
  ctx.globalAlpha = 1;
}

const WARN = ["key_clash", "tempo_over_limit"];

function transitionLabel(t: PlacedTransition): string {
  if (t.state === "pending") return "Planning…";
  if (t.state === "waiting") return "Waiting for beats…";
  if (t.state === "failed") return "Couldn't plan this";
  return [t.technique, t.bars ? `${t.bars} bars` : null].filter(Boolean).join(" · ");
}

function paintTransition(ctx: CanvasRenderingContext2D, p: WavePalette, scene: Scene, view: View, t: PlacedTransition): void {
  const x0 = xOf(view, t.t0);
  const x1 = xOf(view, t.t1);
  if (x1 < 0 || x0 > view.width) return;
  const bandH = 2 * LANE_H + LANE_GAP;
  const failed = t.state === "failed";
  const planned = t.state === "ready";
  const warn = t.explained?.flags.some((f) => WARN.includes(f));
  const tone = failed ? p.danger : warn ? p.warn : p.accent;
  const on = scene.open === t.position || scene.hover.transition === t.position;
  ctx.fillStyle = alpha(tone, on ? 0.24 : 0.12);
  ctx.fillRect(x0, LANES_TOP, x1 - x0, bandH);
  if (!planned) hatch(ctx, x0, LANES_TOP, x1 - x0, bandH, alpha(tone, 0.4));
  ctx.fillStyle = alpha(tone, on ? 1 : 0.65);
  ctx.fillRect(Math.round(x0), LANES_TOP, 1, bandH);
  ctx.fillRect(Math.round(x1) - 1, LANES_TOP, 1, bandH);
  // the seam between the lanes says what the transition is, when it has the room
  const label = transitionLabel(t);
  ctx.font = `600 ${textPx(10)}px ${p.font}`;
  const width = ctx.measureText(label).width + 8;
  if (x1 - x0 >= width && label) {
    const y = LANES_TOP + LANE_H + LANE_GAP / 2;
    const cx = (Math.max(0, x0) + Math.min(view.width, x1)) / 2;
    ctx.fillStyle = alpha(p.bg, 0.85);
    ctx.fillRect(cx - width / 2, y - 7, width, 14);
    ctx.fillStyle = planned ? p.fg : tone;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, cx, y);
    ctx.textAlign = "left";
  }
}

const levelY = (v: number): number => {
  const h = 2 * LANE_H + LANE_GAP - 2 * ARC_PAD;
  return LANES_TOP + ARC_PAD + (1 - Math.min(1, Math.max(0, v))) * h;
};

/** The target arc: the line the order is asked to follow. */
function paintArc(ctx: CanvasRenderingContext2D, p: WavePalette, scene: Scene, view: View): void {
  const { arc, layout } = scene;
  if (arc.length < 2 || !layout.tracks.length) return;
  ctx.strokeStyle = scene.drawing ? p.accent : alpha(p.accent, 0.9);
  ctx.lineWidth = scene.drawing ? 2.5 : 1.75;
  ctx.setLineDash([6, 4]);
  ctx.beginPath();
  // an arc's segments are straight in arc time, not across tracks of different lengths: every pixel asks for its level
  const levelAt = (t: number): number => {
    const u = arcTimeAt(layout, t);
    const after = arc.findIndex(([x]) => x >= u);
    if (after <= 0) return arc[after === 0 ? 0 : arc.length - 1][1];
    const [x0, y0] = arc[after - 1];
    const [x1, y1] = arc[after];
    return y0 + ((y1 - y0) * (u - x0)) / (x1 - x0 || 1);
  };
  const STEP = 4;
  const from = Math.max(0, Math.floor(xOf(view, timeOfArc(layout, 0))));
  const to = Math.min(view.width, Math.ceil(xOf(view, timeOfArc(layout, 1))));
  for (let x = from; x <= to; x += STEP) {
    const y = levelY(levelAt(view.start + x / view.pxPerSec));
    if (x === from) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.setLineDash([]);
}

/** Each track's energy across its time on the set, on the arc's scale: where the set really goes. */
function paintEnergy(ctx: CanvasRenderingContext2D, p: WavePalette, scene: Scene, view: View): void {
  const [lo, hi] = scene.energyRange;
  const span = hi - lo || 1;
  ctx.strokeStyle = alpha(p.fg, 0.85);
  ctx.lineWidth = 1.5;
  for (const track of scene.layout.tracks) {
    if (xOf(view, track.end) < 0 || xOf(view, track.start) > view.width) continue;
    ctx.beginPath();
    let drawn = 0;
    const curve = track.view?.curve ?? [];
    for (const piece of track.pieces) {
      const rate = (piece.f1 - piece.f0) / (piece.t1 - piece.t0) || 1;
      for (const [f, level] of curve) {
        if (f < piece.f0 || f > piece.f1) continue;
        const x = xOf(view, piece.t0 + (f - piece.f0) / rate);
        const y = levelY((level - lo) / span);
        if (drawn++) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
      }
    }
    if (!drawn && track.order.energy !== null) {
      // no curve yet: the track's one number
      const y = levelY((track.order.energy - lo) / span);
      ctx.moveTo(xOf(view, track.start), y);
      ctx.lineTo(xOf(view, track.end), y);
    }
    ctx.stroke();
  }
}

/** Tempo, step by step: each track's BPM across its time on the set, with a ramp over each transition. */
function paintBpm(ctx: CanvasRenderingContext2D, p: WavePalette, scene: Scene, view: View): void {
  const known = scene.layout.tracks.filter((t) => t.order.bpm !== null);
  if (!known.length) return;
  const bpms = known.map((t) => t.order.bpm!);
  const lo = Math.min(...bpms) - 1;
  const hi = Math.max(...bpms) + 1;
  const yOf = (bpm: number) => levelY(0.05 + ((bpm - lo) / (hi - lo)) * 0.9);
  ctx.strokeStyle = p.info;
  ctx.fillStyle = p.info;
  ctx.lineWidth = 1.25;
  ctx.font = `600 ${textPx(9)}px ${p.font}`;
  ctx.textBaseline = "bottom";
  ctx.beginPath();
  let first = true;
  const last = scene.layout.tracks.length - 1;
  for (const track of known) {
    // its own tempo between the transitions; the line ramps across them
    const y = yOf(track.order.bpm!);
    const x0 = xOf(view, track.position > 0 ? track.pieces[0].t1 : track.start);
    const x1 = xOf(view, track.position < last ? track.pieces[track.pieces.length - 1].t0 : track.end);
    if (first) ctx.moveTo(x0, y);
    else ctx.lineTo(x0, y);
    ctx.lineTo(Math.max(x0, x1), y);
    first = false;
  }
  ctx.stroke();
  for (const track of known) {
    const x = xOf(view, (track.start + track.end) / 2);
    const width = xOf(view, track.end) - xOf(view, track.start);
    if (width < 34 || x < 0 || x > view.width) continue;
    ctx.fillText(track.order.bpm!.toFixed(0), x + 3, yOf(track.order.bpm!) - 1);
  }
}

/** The Camelot path along the top: each track's key at its middle, joined by what each move is. */
function paintCamelot(ctx: CanvasRenderingContext2D, p: WavePalette, scene: Scene, view: View): void {
  const { tracks } = scene.layout;
  const y = CAMELOT_H / 2;
  // a track's chip stays on screen for as long as any of the track is, so a zoomed view still shows its path
  const centre = (t: PlacedTrack) => {
    const x0 = xOf(view, t.start);
    const x1 = xOf(view, t.end);
    const left = Math.max(x0, 0) + CHIP_W / 2;
    const right = Math.min(x1, view.width) - CHIP_W / 2;
    return left > right ? (x0 + x1) / 2 : Math.max(left, Math.min(right, (x0 + x1) / 2));
  };
  ctx.lineWidth = 2;
  for (let i = 0; i + 1 < tracks.length; i++) {
    const a = centre(tracks[i]);
    const b = centre(tracks[i + 1]);
    if (b < 0 || a > view.width) continue;
    const clash = camelotMove(tracks[i].order.key, tracks[i + 1].order.key) === "clash";
    ctx.strokeStyle = clash ? p.warn : alpha(p.fg, 0.28);
    ctx.setLineDash(clash ? [3, 3] : []);
    ctx.beginPath();
    ctx.moveTo(a, y);
    ctx.lineTo(b, y);
    ctx.stroke();
  }
  ctx.setLineDash([]);
  const spacing = tracks.length > 1 ? (xOf(view, tracks[0].end) - xOf(view, tracks[0].start)) / 2 : view.width;
  const chips = spacing >= 30;
  ctx.font = `700 ${textPx(10)}px ${p.font}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const t of tracks) {
    const x = centre(t);
    if (x < -20 || x > view.width + 20) continue;
    const color = keyColor(t.order.key);
    if (!chips) {
      ctx.fillStyle = t.order.key ? color.base : p.fgFaint;
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, 2 * Math.PI);
      ctx.fill();
      continue;
    }
    ctx.fillStyle = p.panel;
    ctx.fillRect(x - CHIP_W / 2, y - 8, CHIP_W, 16);
    ctx.fillStyle = color.bg;
    ctx.fillRect(x - CHIP_W / 2, y - 8, CHIP_W, 16);
    ctx.strokeStyle = color.border;
    ctx.lineWidth = 1;
    ctx.strokeRect(x - CHIP_W / 2 + 0.5, y - 7.5, CHIP_W - 1, 15);
    ctx.fillStyle = t.order.key ? color.base : p.fgSubtle;
    ctx.fillText(t.order.key ?? "?", x, y + 0.5);
  }
  ctx.textAlign = "left";
}

export function paintTimeline(ctx: CanvasRenderingContext2D, p: WavePalette, scene: Scene, view: View, dpr: number): void {
  ctx.fillStyle = p.bg;
  ctx.fillRect(0, 0, view.width, HEIGHT);
  ctx.fillStyle = p.panel;
  ctx.fillRect(0, 0, view.width, LANES_TOP);
  drawTimeRuler(ctx, view, CAMELOT_H, RULER_H, (m) => fmtTime(m), { from: 0, to: scene.layout.total });
  const drag = scene.drag;
  for (const track of scene.layout.tracks) paintTrack(ctx, p, scene, view, dpr, track, 0, drag?.position === track.position);
  for (const t of scene.layout.transitions) paintTransition(ctx, p, scene, view, t);
  paintBpm(ctx, p, scene, view);
  paintEnergy(ctx, p, scene, view);
  paintArc(ctx, p, scene, view);
  paintCamelot(ctx, p, scene, view);
  if (drag) {
    const dragged = scene.layout.tracks[drag.position];
    if (dragged) {
      paintTrack(ctx, p, scene, view, dpr, dragged, drag.dt, false);
      // where it lands: before the track now at that position, counting the others
      const others = scene.layout.tracks.filter((t) => t.position !== drag.position);
      const at = drag.to >= others.length ? scene.layout.total : (others[drag.to].start + (drag.to > 0 ? others[drag.to - 1].end : others[drag.to].start)) / 2;
      const x = Math.round(xOf(view, at));
      ctx.fillStyle = p.accent;
      ctx.fillRect(x - 1, LANES_TOP - 4, 2, 2 * LANE_H + LANE_GAP + 8);
    }
  }
}
