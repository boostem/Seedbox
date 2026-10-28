/**
 * Mock v4 mix plans + renders (owner: FRONTEND-SET, added for the set builder's mix editor). A plausible MixPlan per
 * technique on the master beat timeline (beat 0 = B in), saved edits per pair, and a fake render (progress events,
 * a beat map from the tempo lane, a synthesised audio file at the master tempo — see mock/audio.ts).
 */
import type { DeckLoop, DeckPlan, Keyframe, LaneId, MixMarker, MixPlan, MixRender, MixTechnique, ServerEvent, Track, TrackAnalysis, Transition } from "@/api/types";
import { LANE_RANGES } from "@/api/types";

const LANE_IDS: LaneId[] = ["volume", "eq_low", "eq_mid", "eq_high", "filter", "echo_send", "echo_feedback", "reverb_send"];
const NEUTRAL: Record<LaneId, number> = { volume: 1, eq_low: 0, eq_mid: 0, eq_high: 0, filter: 0, echo_send: 0, echo_feedback: 0.55, reverb_send: 0 };
const TECHS: MixTechnique[] = ["long blend", "bass swap", "filter sweep", "echo out", "loop roll", "phrase blend", "drop swap", "slam", "reverb wash", "quick cut"];

const kf = (beat: number, value: number, curve: Keyframe["curve"] = "linear"): Keyframe => ({ beat, value, curve });

function lanes(over: Partial<Record<LaneId, Keyframe[]>>): Record<LaneId, Keyframe[]> {
  return Object.fromEntries(LANE_IDS.map((id) => [id, over[id] ?? [kf(0, NEUTRAL[id])]])) as Record<LaneId, Keyframe[]>;
}

function deck(track: Track, an: TrackAnalysis | null, beat0_s: number, from: number, to: number, l: Partial<Record<LaneId, Keyframe[]>>, loops: DeckLoop[] = [], echo = 0.75): DeckPlan {
  return { track_id: track.id, beat0_s, native_bpm: an?.bpm ?? track.bpm, play_from_beat: from, play_to_beat: to, echo_beats: echo, loops, lanes: lanes(l) };
}

export function generateMixPlan(folder: string, a: Track, aAn: TrackAnalysis | null, b: Track, bAn: TrackAnalysis | null, t: Transition, technique?: string | null, bars?: number | null): MixPlan {
  const tech = (TECHS.includes(technique as MixTechnique) ? technique : t.technique === "quick cut" ? "quick cut" : t.technique ?? "phrase blend") as MixTechnique;
  const cut = tech === "quick cut" || tech === "drop swap" || tech === "slam";
  const L = ([4, 8, 16, 32].includes(bars ?? 0) ? bars : Math.max(4, Math.min(32, t.overlap_bars || 16))) as 4 | 8 | 16 | 32;
  const n = cut ? 0 : L * 4; // blend length in beats
  const aBpm = aAn?.bpm ?? a.bpm;
  const bBpm = bAn?.bpm ?? b.bpm;
  const out = t.out_start_s ?? Math.max(0, (aAn?.duration_s ?? a.duration_s) - 40);
  const inS = t.in_start_s ?? bAn?.first_downbeat_s ?? 0;
  const start = -32;
  const end = Math.max(n, 16) + 32;
  const ramp = Math.max(64, n);
  const tempo = [kf(start, aBpm, "hold"), kf(0, aBpm, "ease"), kf(ramp, bBpm, "linear")];
  const half = Math.round(n / 2 / 4) * 4;
  const markers: MixMarker[] = [{ beat: 0, label: "B in" }];
  const notes: string[] = [];
  let A: DeckPlan;
  let B: DeckPlan;
  switch (tech) {
    case "filter sweep":
      A = deck(a, aAn, out, start, n, { volume: [kf(0, 1, "linear"), kf(n - 4, 1, "ease"), kf(n, 0)], filter: [kf(0, 0, "ease"), kf(n, 0.85)], reverb_send: [kf(n - 8, 0, "linear"), kf(n, 0.5)] });
      B = deck(b, bAn, inS, 0, end, { volume: [kf(0, 0.0, "ease"), kf(8, 0.9, "linear"), kf(half, 1)], filter: [kf(0, -0.85, "ease"), kf(n, 0)] });
      notes.push(`Open a high-pass on A over ${L} bars while B's low-pass opens — the two swap places in the spectrum.`);
      markers.push({ beat: n, label: "A out" });
      break;
    case "echo out":
      A = deck(a, aAn, out, start, n, { volume: [kf(0, 1, "hold"), kf(n, 0)], echo_send: [kf(n - 4, 0, "hold"), kf(n - 1, 1, "hold"), kf(n + 4, 0)], echo_feedback: [kf(0, 0.6)] });
      B = deck(b, bAn, inS, 0, end, { volume: [kf(0, 0.6, "linear"), kf(n, 1)], eq_low: [kf(0, -30, "hold"), kf(n, 0)] });
      markers.push({ beat: n - 1, label: "echo throw" }, { beat: n, label: "A out" });
      notes.push(`Ride B in under A with its lows cut, throw A's last beat into a dotted-8th echo and pull A's fader on the 1.`);
      break;
    case "loop roll": {
      const loops: DeckLoop[] = [
        { start_beat: n - 16, end_beat: n - 8, source_beat: n - 16, length_beats: 4 },
        { start_beat: n - 8, end_beat: n - 4, source_beat: n - 8, length_beats: 2 },
        { start_beat: n - 4, end_beat: n, source_beat: n - 4, length_beats: 1 },
      ];
      A = deck(a, aAn, out, start, n, { volume: [kf(0, 1, "hold"), kf(n, 0)], filter: [kf(n - 16, 0, "ease"), kf(n, 0.6)], echo_send: [kf(n - 2, 0, "linear"), kf(n, 0.8, "hold"), kf(n + 4, 0)] }, loops);
      B = deck(b, bAn, inS, 0, end, { volume: [kf(0, 0, "ease"), kf(n - 16, 0.8, "linear"), kf(n, 1)], eq_low: [kf(0, -30, "hold"), kf(n, 0)] });
      markers.push({ beat: n - 16, label: "loop" }, { beat: n, label: "A out" });
      notes.push("Roll A's last phrase down 4 → 2 → 1 beat loops with a rising high-pass, then drop B's bass on the 1.");
      break;
    }
    case "quick cut":
    case "drop swap":
    case "slam":
      A = deck(a, aAn, out, start, 0, tech === "slam" ? { reverb_send: [kf(-2, 0, "linear"), kf(0, 0.9, "hold"), kf(4, 0)], echo_send: [kf(-1, 0, "hold"), kf(-0.5, 1, "hold"), kf(4, 0)] } : { volume: [kf(0, 1)] });
      B = deck(b, bAn, inS, 0, end, {});
      markers.push({ beat: 0, label: "A out" });
      if (tech === "slam") markers.push({ beat: -0.5, label: "echo throw" });
      notes.push(tech === "slam" ? "Slam: wash A into reverb + echo on its last half-beat and hit B's drop on the 1." : `${tech === "drop swap" ? "Drop swap" : "Quick cut"} on the 1 — no overlap.`);
      break;
    case "reverb wash":
      A = deck(a, aAn, out, start, 0, { volume: [kf(-4, 1, "hold"), kf(0, 0)], filter: [kf(-32, 0, "ease"), kf(0, 0.55)], reverb_send: [kf(-32, 0, "ease"), kf(0, 0.9, "hold"), kf(1, 0.9, "linear"), kf(2, 0)] });
      A.reverb_size = 0.95;
      B = deck(b, bAn, inS, 0, end, {});
      markers.push({ beat: -32, label: "reverb wash" }, { beat: 0, label: "A out" });
      notes.push("Reverb wash: A's reverb swells over its last 8 bars under a rising high-pass, then cut to B on the 1.");
      break;
    default: {
      // long blend / phrase blend / bass swap
      const swap = tech === "bass swap" ? Math.min(n, (t.swap_bar ?? L / 2 + 1) * 4 - 4) : half;
      A = deck(a, aAn, out, start, n, { volume: [kf(0, 1, "linear"), kf(half, 1, "ease"), kf(n, 0)], eq_low: [kf(swap, 0, "hold"), kf(swap, -30)], eq_high: [kf(half, 0, "linear"), kf(n, -12)] });
      B = deck(b, bAn, inS, 0, end, { volume: [kf(0, tech === "bass swap" ? 0.75 : 0.0, "ease"), kf(Math.min(half, 16), 1)], eq_low: [kf(0, -30, "hold"), kf(swap, 0)], eq_high: [kf(0, -10, "linear"), kf(half, 0)] });
      markers.push({ beat: swap, label: "bass swap" }, { beat: n, label: "A out" });
      notes.push(`${L}-bar ${tech}: bring B in on the phrase with its lows cut, swap the bass on bar ${swap / 4 + 1}, fade A out by bar ${L + 1}.`);
    }
  }
  if (Math.abs(aBpm - bBpm) > 0.05) {
    markers.push({ beat: 0, label: "tempo ramp" });
    notes.push(`Master tempo eases from ${aBpm.toFixed(1)} to ${bBpm.toFixed(1)} BPM over ${ramp / 4} bars (key lock on).`);
  }
  notes.push(`B starts at ${fmt(inS)} in its file; A's ${fmt(out)} lands on master beat 0.`);
  return {
    version: 1,
    folder,
    from_id: a.id,
    to_id: b.id,
    technique: tech,
    bars: L,
    key_lock: true,
    window: { start_beat: start, end_beat: end },
    tempo,
    a: A,
    b: B,
    markers: markers.sort((x, y) => x.beat - y.beat),
    notes,
    edited: false,
  };
}

function fmt(s: number): string {
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
}

/** Clamp + sort like mixplan.validate. */
export function validateMixPlan(p: MixPlan): MixPlan {
  const fix = (kfs: Keyframe[], [lo, hi]: [number, number]) =>
    (kfs.length ? kfs : [kf(0, lo)]).map((k) => ({ ...k, value: Math.max(lo, Math.min(hi, k.value)) })).sort((x, y) => x.beat - y.beat);
  const d = (x: DeckPlan): DeckPlan => ({ ...x, lanes: Object.fromEntries(LANE_IDS.map((id) => [id, fix(x.lanes[id] ?? [], LANE_RANGES[id])])) as Record<LaneId, Keyframe[]> });
  return { ...p, tempo: fix(p.tempo, [60, 200]), a: d(p.a), b: d(p.b) };
}

// ---- renders

export interface MockRenderInfo {
  duration: number;
  bpm: number;
}
/** render_id → what mock/audio.ts should synthesise. */
export const mockRenders = new Map<string, MockRenderInfo>();

function hashStr(s: string): string {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return (h >>> 0).toString(16).padStart(8, "0");
}

export function mockRender(plan: MixPlan): MixRender {
  const id = hashStr(JSON.stringify(plan));
  const times: number[] = [];
  let t = 0;
  for (let beat = plan.window.start_beat; beat <= plan.window.end_beat; beat++) {
    times.push(Math.round(t * 1000) / 1000);
    const bpm = laneAt(plan.tempo, beat + 0.5);
    t += 60 / bpm;
  }
  const duration = times[times.length - 1];
  mockRenders.set(id, { duration, bpm: laneAt(plan.tempo, 0) });
  const warnings: string[] = [];
  const peakA = Math.max(...plan.a.lanes.volume.map((k) => k.value));
  const peakB = Math.max(...plan.b.lanes.volume.map((k) => k.value));
  if (peakA + peakB > 1.9 && plan.bars >= 16) warnings.push("both decks near full volume during the blend (+3 dB bump) — limiter engaged");
  return {
    render_id: id,
    audio_url: `/api/renders/${id}.m4a`,
    duration_s: duration,
    start_beat: plan.window.start_beat,
    beat_times_s: times,
    lufs: -9.4 + ((parseInt(id.slice(0, 2), 16) % 10) - 5) / 10,
    true_peak_db: -1.0,
    warnings,
  };
}

function laneAt(kfs: Keyframe[], beat: number): number {
  if (beat <= kfs[0].beat) return kfs[0].value;
  for (let i = 0; i + 1 < kfs.length; i++) {
    const [k0, k1] = [kfs[i], kfs[i + 1]];
    if (beat < k1.beat) {
      if (k0.curve === "hold" || k1.beat === k0.beat) return k0.value;
      const f = (beat - k0.beat) / (k1.beat - k0.beat);
      return k0.value + (k1.value - k0.value) * (k0.curve === "ease" ? f * f * (3 - 2 * f) : f);
    }
  }
  return kfs[kfs.length - 1].value;
}

/** Emits `render` progress events for a mock render over ~1 s. */
export async function emitRenderProgress(emit: (ev: ServerEvent) => void, r: MixRender, folder: string, from: string, to: string, cached: boolean): Promise<void> {
  if (cached) return;
  for (const p of [0.1, 0.35, 0.6, 0.85]) {
    emit({ type: "render", data: { render_id: r.render_id, folder, from_id: from, to_id: to, state: "running", progress: p } });
    await new Promise((res) => setTimeout(res, 220));
  }
  emit({ type: "render", data: { render_id: r.render_id, folder, from_id: from, to_id: to, state: "done", progress: 1 } });
}
