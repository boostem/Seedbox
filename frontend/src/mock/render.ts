/**
 * Mock mix renders: progress events, a beat map from the plan's tempo lane and a synthesised audio file at the master
 * tempo (mock/audio.ts) instead of mixrender's audio.
 */
import type { MixPlan, MixRender, ServerEvent } from "@/api/types";
import { laneValue } from "@/features/set/mix/lanes";

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
    const bpm = laneValue(plan.tempo, beat + 0.5);
    t += 60 / bpm;
  }
  const duration = times[times.length - 1];
  mockRenders.set(id, { duration, bpm: laneValue(plan.tempo, 0) });
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

/** Emits `render` progress events for a mock render over ~1 s. */
export async function emitRenderProgress(emit: (ev: ServerEvent) => void, r: MixRender, folder: string, from: string, to: string, cached: boolean): Promise<void> {
  if (cached) return;
  for (const p of [0.1, 0.35, 0.6, 0.85]) {
    emit({ type: "render", data: { render_id: r.render_id, folder, from_id: from, to_id: to, state: "running", progress: p } });
    await new Promise((res) => setTimeout(res, 220));
  }
  emit({ type: "render", data: { render_id: r.render_id, folder, from_id: from, to_id: to, state: "done", progress: 1 } });
}
