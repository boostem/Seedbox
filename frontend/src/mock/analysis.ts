/** Plausible fake TrackAnalysis + Transition + Partner data (deterministic per track id) for mock mode. */
import type { CuePoint, Edge, Section, SectionKind, Technique, Track, TrackAnalysis, Transition } from "@/api/types";
import { camelotMove } from "@/lib/camelot";
import { fmtTime } from "@/lib/format";
import { bpmFactor } from "./data";
import { between, rng } from "./rng";

const cache = new Map<string, TrackAnalysis>();
const r3 = (x: number) => Math.round(Math.max(0, Math.min(1, x)) * 1000) / 1000;

export function mockAnalysis(track: Track): TrackAnalysis {
  const hit = cache.get(track.id);
  if (hit) return hit;
  const R = rng(`an:${track.id}`);
  const duration = track.duration_s || 200;
  let bpm = track.bpm || 124;
  if (bpm < 95) bpm *= 2;
  const bar_s = 240 / bpm;
  const fdb = between(R, 0.04, 0.35);
  const bar_count = Math.max(16, Math.floor((duration - fdb) / bar_s));
  const ri = R();
  const introBars = ri < 0.5 ? 32 : ri < 0.62 ? 8 : 16; // some short 8-bar intros (the planner loops them)
  const outroBars = R() < 0.55 ? 32 : 16;
  const vocalTrack = R() < 0.7;

  // structure in bars
  const plan: [SectionKind, number][] = [["intro", introBars], ["build", 8], ["drop", 32], ["breakdown", R() < 0.5 ? 16 : 32], ["build", 8], ["drop", 32]];
  const avail = bar_count - outroBars;
  const sections: Section[] = [];
  let bar = 0;
  for (const [kind, len] of plan) {
    if (bar >= avail) break;
    let end = Math.min(avail, bar + len);
    if (avail - end < 8) end = avail; // no stub sections: fold a short remainder into this one
    sections.push({ kind, start_bar: bar, end_bar: end, start_s: 0, end_s: 0 });
    bar = end;
  }
  if (bar < avail) sections.push({ kind: "body", start_bar: bar, end_bar: avail, start_s: 0, end_s: 0 });
  sections.push({ kind: "outro", start_bar: avail, end_bar: bar_count, start_s: 0, end_s: 0 });
  const t = (b: number) => Math.min(duration, fdb + b * bar_s);
  for (const s of sections) {
    s.start_s = Math.round(t(s.start_bar) * 100) / 100;
    s.end_s = Math.round((s.end_bar === bar_count ? duration : t(s.end_bar)) * 100) / 100;
  }
  const kindAt = (b: number) => sections.find((s) => b >= s.start_bar && b < s.end_bar) ?? sections[sections.length - 1];

  const energy_per_bar: number[] = [];
  const low_per_bar: number[] = [];
  const vocal_per_bar: number[] = [];
  for (let b = 0; b < bar_count; b++) {
    const s = kindAt(b);
    const f = (b - s.start_bar) / Math.max(1, s.end_bar - s.start_bar);
    const n = (R() - 0.5) * 0.08;
    const e = { intro: 0.42 + f * 0.12, build: 0.55 + f * 0.3, drop: 0.9, breakdown: 0.32, body: 0.74, outro: 0.58 - f * 0.25 }[s.kind] + n;
    const lo = { intro: 0.82, build: 0.45 + f * 0.2, drop: 0.97, breakdown: 0.08, body: 0.85, outro: 0.8 - f * 0.2 }[s.kind] + n;
    const vocalSection = vocalTrack && (s.kind === "breakdown" || s.kind === "drop" || s.kind === "body") && (b >> 3) % 2 === 0;
    energy_per_bar.push(r3(e));
    low_per_bar.push(r3(lo));
    vocal_per_bar.push(r3(vocalSection ? 0.65 + R() * 0.3 : R() * 0.15));
  }

  const introDrums = R() > 0.25;
  const outroDrums = R() > 0.3;
  const intro: Edge = { bars: introBars, start_s: round2(fdb), end_s: round2(t(introBars)), drums_only: introDrums, has_vocals: !introDrums && R() < 0.4 };
  const outro: Edge = { bars: outroBars, start_s: round2(t(avail)), end_s: round2(duration), drums_only: outroDrums, has_vocals: !outroDrums && R() < 0.5 };

  const cues: CuePoint[] = [{ bar: 0, time_s: round2(fdb), label: `mix in (${introBars}-bar intro)` }];
  sections.forEach((s, i) => {
    if (s.kind === "drop") cues.push({ bar: s.start_bar, time_s: s.start_s, label: sections.slice(0, i).some((x) => x.kind === "drop") ? "drop 2" : "drop" });
    if (s.kind === "breakdown") cues.push({ bar: s.start_bar, time_s: s.start_s, label: "breakdown" });
  });
  cues.push({ bar: avail, time_s: round2(t(avail)), label: `mix out (${outroBars}-bar outro)` });

  // 3-band waveform, ~50 points/s capped at 20k points
  const points = Math.min(20000, Math.ceil(duration * 50));
  const pps = points / duration;
  const beat = bar_s / 4;
  const low: number[] = new Array(points);
  const mid: number[] = new Array(points);
  const high: number[] = new Array(points);
  for (let i = 0; i < points; i++) {
    const ts = i / pps;
    const b = Math.max(0, Math.min(bar_count - 1, Math.floor((ts - fdb) / bar_s)));
    const ph = (((ts - fdb) / beat) % 1 + 1) % 1;
    const kick = Math.exp(-ph * 9);
    const hat = Math.exp(-Math.abs(ph - 0.5) * 22);
    const e = energy_per_bar[b];
    const lo = low_per_bar[b];
    const v = vocal_per_bar[b];
    const nz = R();
    low[i] = r3(lo * (0.25 + 0.75 * kick) * (0.9 + nz * 0.1));
    mid[i] = r3(e * 0.45 + v * 0.3 + nz * 0.12 + kick * 0.08);
    high[i] = r3(e * 0.25 + hat * 0.35 * e + nz * 0.1);
  }

  const an: TrackAnalysis = {
    version: 1,
    track_id: track.id,
    duration_s: duration,
    bpm,
    bpm_source: "tunebat",
    first_downbeat_s: round2(fdb),
    bar_s,
    bar_count,
    phrase_offset: 0,
    energy_per_bar,
    low_per_bar,
    vocal_per_bar,
    sections,
    intro,
    outro,
    cues,
    waveform: { points_per_second: pps, low, mid, high },
  };
  cache.set(track.id, an);
  return an;
}

function round2(x: number) {
  return Math.round(x * 100) / 100;
}

const KEY_PTS: Record<string, number> = { same: 60, "+1": 50, "-1": 50, relative: 46, "+2 boost": 34, diagonal: 30, "+7 boost": 30, clash: 4, "?": 20 };

function snapBars(n: number): number {
  return n >= 32 ? 32 : n >= 16 ? 16 : 8;
}

/** a → b transition suggestion (mirrors the shape transitions.suggest returns). */
export function mockTransition(a: Track, aAn: TrackAnalysis | null, b: Track, bAn: TrackAnalysis | null, tol = 0.1): Transition {
  const key_move = camelotMove(a.camelot, b.camelot);
  const factor = bpmFactor(a.bpm, b.bpm, 0.5) ?? 1;
  const pitch_pct = Math.round(((a.bpm / (b.bpm * factor)) - 1) * 1000) / 10;
  const warnings: string[] = [];
  const notes: string[] = [];
  if (key_move === "clash") warnings.push(`key clash (${a.camelot} → ${b.camelot}) — keep the overlap short`);
  if (Math.abs(pitch_pct) > tol * 100 * 0.6) warnings.push(`big tempo change (${pitch_pct > 0 ? "+" : ""}${pitch_pct}% on B)`);
  if (factor !== 1) notes.push(`B plays ${factor === 2 ? "half" : "double"}-time against A`);
  let s = KEY_PTS[key_move] ?? 0;
  s += 25 * Math.max(0, 1 - Math.abs(pitch_pct) / 10);
  const ready = !!aAn && !!bAn;
  if (!ready) {
    notes.unshift("Waiting for audio analysis — key and tempo only");
    return { from_id: a.id, to_id: b.id, score: Math.round(Math.min(100, s * 1.18)), key_move, pitch_pct, out_start_s: null, in_start_s: null, overlap_bars: null, swap_bar: null, technique: null, notes, warnings, ready: false };
  }
  let overlap = snapBars(Math.min(aAn.outro.bars, bAn.intro.bars));
  // an 8-bar edge is stretched to a 16-bar blend by looping it (musicreco.transitions._a_loop / _b_loop)
  const a_loop_bars = overlap === 8 && aAn.outro.bars === 8 ? 8 : 0;
  const b_loop_bars = overlap === 8 && bAn.intro.bars === 8 ? 8 : 0;
  if (a_loop_bars || b_loop_bars) overlap = 16;
  const out_start_s = round2(aAn.outro.start_s + Math.max(0, aAn.outro.bars - overlap + a_loop_bars) * aAn.bar_s);
  const in_start_s = bAn.intro.start_s;
  const vocalClash = aAn.outro.has_vocals && bAn.intro.has_vocals;
  let technique: Technique;
  if (key_move === "clash") technique = "echo out";
  else if (Math.abs(pitch_pct) > 6) technique = "quick cut";
  else if (aAn.outro.drums_only && bAn.intro.drums_only && overlap >= 16) technique = "long blend";
  else if (vocalClash || !bAn.intro.drums_only) technique = "bass swap";
  else technique = "phrase blend";
  const describe = (e: { bars: number; drums_only: boolean; has_vocals: boolean }) => (e.drums_only ? "drums-only" : e.has_vocals ? "vocal" : "full");
  notes.unshift(
    `A's ${aAn.outro.bars}-bar outro is ${describe(aAn.outro)}; B's ${bAn.intro.bars}-bar intro is ${describe(bAn.intro)} → ${overlap}-bar ${technique} starting at A ${fmtTime(out_start_s)}`,
    `Start B from ${fmtTime(in_start_s)} (first downbeat)`,
  );
  if (technique === "bass swap") notes.push(`Swap the lows at bar ${overlap / 2} of the overlap`);
  if (vocalClash) warnings.push(`vocals overlap for ${overlap} bars`);
  if (bAn.intro.bars < 16) warnings.push(`short intro (${bAn.intro.bars} bars)`);
  s += (aAn.outro.drums_only ? 6 : 0) + (bAn.intro.drums_only ? 6 : 0) + (overlap >= 16 ? 5 : 0) - (vocalClash ? 8 : 0);
  return {
    from_id: a.id,
    to_id: b.id,
    score: Math.max(0, Math.min(100, Math.round(s))),
    key_move,
    pitch_pct,
    out_start_s,
    in_start_s,
    overlap_bars: overlap,
    swap_bar: technique === "bass swap" ? overlap / 2 + 1 : null,
    technique,
    a_loop_bars,
    b_loop_bars,
    notes,
    warnings,
    ready: true,
  };
}
