/**
 * Mock v4 mix plans + renders (owner: FRONTEND-SET, added for the set builder's mix editor). A plausible MixPlan per
 * technique on the master beat timeline (beat 0 = B in), saved edits per pair, and a fake render (progress events,
 * a beat map from the tempo lane, a synthesised audio file at the master tempo — see mock/audio.ts).
 */
import type { ChillStyle, DeckPlan, Keyframe, LaneId, MixMarker, MixPlan, MixRender, MixStyle, MixTechnique, ServerEvent, StyleFit, SwapKind, Track, TrackAnalysis, Transition } from "@/api/types";
import { LANE_RANGES } from "@/api/types";

const LANE_IDS: LaneId[] = ["volume", "eq_low", "eq_mid", "eq_high", "filter", "vocals", "instrumental", "echo_send", "echo_feedback", "reverb_send"];
const NEUTRAL: Record<LaneId, number> = { volume: 1, eq_low: 0, eq_mid: 0, eq_high: 0, filter: 0, vocals: 1, instrumental: 1, echo_send: 0, echo_feedback: 0.5, reverb_send: 0 };
const CHILL: ChillStyle[] = ["phrase mix", "acapella", "filter fade", "double drop", "quick cut"];
const SWAPS: SwapKind[] = ["hard", "quick", "slow"];

function hash(key: string, seed = 7): number {
  let h = seed;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
}

/** The chill style the mock planner picks for a pair (asked for, else mostly the phrase mix, sometimes another). */
export function mockStyle(tech: MixTechnique | string, key: string, asked?: string | null): MixStyle {
  if (asked && (CHILL as string[]).includes(asked)) return asked as ChillStyle;
  if (tech === "quick cut" || tech === "drop swap") return "quick cut";
  const pool: ChillStyle[] = ["phrase mix", "phrase mix", "phrase mix", "filter fade", "phrase mix", "acapella", "phrase mix", "double drop"];
  return pool[hash(key) % pool.length];
}

const MOCK_WHY: Record<ChillStyle, string> = {
  "phrase mix": "the smooth default: B's highs, then its mids, then the bass",
  acapella: "A sings over the bass swap and the keys match: its vocal can ride over B's beat",
  "filter fade": "both songs are full arrangements here: a filter fade melts one into the other",
  "double drop": "both drops line up in key at the same tempo",
  "quick cut": "the songs can blend, so a cut would be abrupt",
};

/** Plausible fit scores for the mock editor: the planned style on top, the rest spread below it. */
function mockFit(style: MixStyle, key: string): StyleFit[] {
  const h = hash(key, 13);
  return CHILL.map((s, i) => ({ style: s, fit: s === style ? 0.72 : s === "quick cut" ? 0.3 : Math.round((((h >> i) % 30) / 100 + 0.3) * 100) / 100, why: MOCK_WHY[s] }))
    .sort((x, y) => y.fit - x.fit);
}

const kf = (beat: number, value: number, curve: Keyframe["curve"] = "linear"): Keyframe => ({ beat, value, curve });

function lanes(over: Partial<Record<LaneId, Keyframe[]>>): Record<LaneId, Keyframe[]> {
  return Object.fromEntries(LANE_IDS.map((id) => [id, over[id] ?? [kf(0, NEUTRAL[id])]])) as Record<LaneId, Keyframe[]>;
}

function deck(track: Track, an: TrackAnalysis | null, beat0_s: number, from: number, to: number, l: Partial<Record<LaneId, Keyframe[]>>): DeckPlan {
  return { track_id: track.id, beat0_s, native_bpm: an?.bpm ?? track.bpm, play_from_beat: from, play_to_beat: to, echo_beats: 0.75, loops: [], fx: [], lanes: lanes(l) };
}

function swapLanes(kind: SwapKind, s: number): { a: Keyframe[]; b: Keyframe[] } {
  if (kind === "quick") return { a: [kf(s - 4, 0), kf(s - 2, -3, "ease"), kf(s, -30, "hold")], b: [kf(s - 4, -30, "ease"), kf(s - 2, -3), kf(s, 0, "hold")] };
  if (kind === "slow") return { a: [kf(s - 8, 0), kf(s, -3, "ease"), kf(s + 8, -30, "hold")], b: [kf(s - 8, -30, "ease"), kf(s, -3), kf(s + 8, 0, "hold")] };
  return { a: [kf(0, 0, "hold"), kf(s, -30, "hold")], b: [kf(0, -30, "hold"), kf(s, 0, "hold")] };
}

export function generateMixPlan(folder: string, a: Track, aAn: TrackAnalysis | null, b: Track, bAn: TrackAnalysis | null, t: Transition, technique?: string | null, bars?: number | null, style?: string | null, swap?: string | null): MixPlan {
  const key = a.id + b.id;
  const st = mockStyle(technique ?? t.technique ?? "phrase blend", key, style) as ChillStyle;
  const cut = st === "quick cut";
  const L = (st === "double drop" ? 16 : [4, 8, 16, 32].includes(bars ?? 0) ? bars : 16) as 4 | 8 | 16 | 32;
  const n = cut ? 0 : L * 4; // blend length in beats
  const aBpm = aAn?.bpm ?? a.bpm;
  const bBpm = bAn?.bpm ?? b.bpm;
  const out = t.out_start_s ?? Math.max(0, (aAn?.duration_s ?? a.duration_s) - 40);
  const inS = t.in_start_s ?? bAn?.first_downbeat_s ?? 0;
  const start = -32;
  const end = Math.max(n, 16) + 32;
  const tempo = Math.abs(aBpm - bBpm) > 0.05 ? [kf(start, aBpm, "hold"), kf(n, aBpm, "ease"), kf(n + 64, bBpm, "hold")] : [kf(start, aBpm, "hold")];
  const half = n / 2;
  const kind: SwapKind = SWAPS.includes(swap as SwapKind) ? (swap as SwapKind) : st === "acapella" ? "hard" : SWAPS[hash(key, 3) % 3];
  const markers: MixMarker[] = [{ beat: 0, label: cut ? "cut" : "B in" }];
  const notes: string[] = [];
  let A: DeckPlan;
  let B: DeckPlan;
  if (cut) {
    A = deck(a, aAn, out, start, 0, { volume: [kf(-4, 1, "hold"), kf(0, 0, "hold")] });
    B = deck(b, bAn, inS, 0, end, {});
    notes.push("Clean cut on the 1 at the phrase into B, no FX.");
  } else if (st === "double drop") {
    A = deck(a, aAn, out, start, n, { eq_low: [kf(30, 0), kf(31, -30, "hold"), kf(32, 0, "hold"), kf(48, 0), kf(49, -30, "hold")], eq_mid: [kf(48, 0, "ease"), kf(64, -12, "hold")], volume: [kf(56, 1, "ease"), kf(64, 0, "hold")] });
    B = deck(b, bAn, inS, 0, end, { volume: [kf(0, 0, "ease"), kf(24, 0.8), kf(48, 1, "hold")], eq_low: [kf(0, -30, "hold"), kf(48, -30), kf(49, 0, "hold")] });
    markers.push({ beat: 32, label: "double drop" }, { beat: 48, label: "bass swap" });
    notes.push("Double drop: B comes in under A's build, both drop together on the 1, the bass goes to B 4 bars later.");
  } else {
    const sw = swapLanes(kind, half);
    if (st === "filter fade") {
      A = deck(a, aAn, out, start, n, { eq_low: sw.a, filter: [kf(n / 4, 0, "ease"), kf(n, 0.6, "hold")], volume: [kf(n - n / 8, 1, "ease"), kf(n, 0, "hold")] });
      B = deck(b, bAn, inS, 0, end, { eq_low: sw.b, volume: [kf(0, 0, "ease"), kf(n / 4, 1, "hold")], filter: [kf(0, -0.7, "ease"), kf(0.75 * n, 0, "hold")] });
      notes.push(`${L}-bar filter fade: B rises out of a closed low-pass while A slides up into a high-pass; the bass still changes hands.`);
    } else {
      const rest = n - half;
      const aLanes: Partial<Record<LaneId, Keyframe[]>> = { eq_low: sw.a, eq_mid: [kf(half + rest / 4, 0, "ease"), kf(half + 0.625 * rest, -30, "hold")], eq_high: [kf(n - rest / 2, 0, "ease"), kf(n, -30, "hold")], volume: [kf(n - rest / 4, 1, "ease"), kf(n, 0, "hold")], filter: [kf(n - rest / 2, 0, "ease"), kf(n, 0.45, "hold")] };
      if (st === "acapella") {
        Object.assign(aLanes, { instrumental: [kf(half - 0.5, 1, "ease"), kf(half, 0, "hold")], vocals: [kf(half + 21, 1, "ease"), kf(half + 22, 0, "hold")], eq_mid: [kf(0, 0)], eq_high: [kf(0, 0)], filter: [kf(0, 0)], volume: [kf(half + 22, 1, "ease"), kf(half + 23, 0, "hold")] });
        markers.push({ beat: half, label: "acapella" });
      }
      A = deck(a, aAn, out, start, n, aLanes);
      B = deck(b, bAn, inS, 0, end, { eq_low: sw.b, eq_high: [kf(0, -30, "ease"), kf(n / 4, 0, "hold")], eq_mid: [kf(half / 2, -30, "ease"), kf(half, 0, "hold")] });
      notes.push(st === "acapella"
        ? `${L}-bar acapella blend: on the 1 of bar ${half / 4 + 1} A's beat drops out and its vocal rides over B's until the line ends.`
        : `${L}-bar phrase mix: B's highs open first, then its mids, a ${kind} bass swap on bar ${half / 4 + 1}, then A's mids and highs leave under a touch of high-pass.`);
    }
    markers.push({ beat: half, label: "bass swap" }, { beat: n, label: "A out" });
  }
  if (tempo.length > 1) {
    markers.push({ beat: n, label: "tempo ramp" });
    notes.push(`The tempo glides from ${aBpm.toFixed(1)} to ${bBpm.toFixed(1)} BPM over 16 bars once A is out (key lock on).`);
  }
  notes.push(`B starts at ${fmt(inS)} in its file; A's ${fmt(out)} lands on master beat 0.`);
  const split = hash(key, 5) % 4 !== 0;
  return {
    version: 4,
    folder,
    from_id: a.id,
    to_id: b.id,
    technique: (cut ? "quick cut" : st === "double drop" ? "double drop" : st === "filter fade" ? "filter sweep" : "bass swap") as MixTechnique,
    bars: cut ? 4 : L,
    key_lock: true,
    window: { start_beat: start, end_beat: end },
    tempo,
    a: A,
    b: B,
    markers: markers.sort((x, y) => x.beat - y.beat),
    notes,
    edited: false,
    style: st,
    style_fit: mockFit(st, key),
    swap_kind: cut || st === "double drop" ? null : kind,
    vocal_stems: { a: split, b: split },
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
