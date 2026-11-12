/**
 * The analysis queue (#73): what is analysing now, how far the stems are and when it will all be done, from GET
 * /api/jobs at start and the job.* events after. Followed once from <App> (`followJobs`).
 *
 * The ETA: each job type's time is learnt from the jobs seen finishing here (a moving average, seeded with the
 * budgets in seedbox2/README.md), a running job's remaining time from its own progress, and each pool's queue is
 * shared by its workers. The pools run side by side, so the ETA is the slowest pool's.
 */
import { create } from "zustand";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { JobRow } from "@/api/types";

export interface RunningJob {
  id: string;
  kind: string;
  hash: string | null;
  progress: number;
  /** ms (performance.now()) when it started, or when we first heard of it */
  startedAt: number;
  /** we heard it start (so its duration is worth learning from) */
  seenStart: boolean;
}

/** Seconds a job of each type takes before we have seen one finish (README budgets, on the reference laptop). */
export const DEFAULT_SECONDS: Record<string, number> = { waveform: 0.5, beats: 5, key: 3, energy: 2, sections: 3, vocals: 4, embedding: 3, stems: 60, set_export: 300 };
const FALLBACK_SECONDS = 5;
const HEAVY = new Set(["stems", "render", "set_export"]);

export interface JobsState {
  running: Record<string, RunningJob>;
  /** queued job id → its kind */
  queued: Record<string, string>;
  /** queued job id → the track it is about (jobs about no track aren't here) */
  queuedHash: Record<string, string>;
  /** learnt seconds per job type */
  seconds: Record<string, number>;
  /** job type → pool, and each pool's workers */
  pools: Record<string, string>;
  workers: Record<string, number>;
  /** pools whose queued jobs wait for low-power mode to end (#85): they don't count towards the ETA */
  pausedPools: string[];
}

export const useJobs = create<JobsState>()(() => ({ running: {}, queued: {}, queuedHash: {}, seconds: {}, pools: {}, workers: { light: 2, heavy: 1 }, pausedPools: [] }));

export function poolOf(kind: string, pools: Record<string, string>): string {
  return pools[kind] ?? (HEAVY.has(kind) ? "heavy" : "light");
}

export function secondsFor(kind: string, learnt: Record<string, number>): number {
  return learnt[kind] ?? DEFAULT_SECONDS[kind] ?? FALLBACK_SECONDS;
}

/** Seconds until the queue is empty, or null when there is nothing to do. */
export function queueEta(s: Pick<JobsState, "running" | "queued" | "seconds" | "pools" | "workers"> & Partial<Pick<JobsState, "pausedPools">>, now: number): number | null {
  const work: Record<string, number> = {};
  let any = false;
  for (const j of Object.values(s.running)) {
    any = true;
    const per = secondsFor(j.kind, s.seconds);
    const elapsed = Math.max(0, (now - j.startedAt) / 1000);
    // trust the job's own progress once it has said enough; else the type's average less what has passed
    const left = j.progress > 0.05 ? (elapsed * (1 - j.progress)) / j.progress : Math.max(per * (1 - j.progress) - elapsed, per * 0.1);
    const pool = poolOf(j.kind, s.pools);
    work[pool] = (work[pool] ?? 0) + left;
  }
  for (const kind of Object.values(s.queued)) {
    const pool = poolOf(kind, s.pools);
    if (s.pausedPools?.includes(pool)) continue;
    any = true;
    work[pool] = (work[pool] ?? 0) + secondsFor(kind, s.seconds);
  }
  if (!any) return null;
  return Math.max(...Object.entries(work).map(([pool, w]) => w / Math.max(1, s.workers[pool] ?? 1)));
}

/** What the queue holds per track: the tracks with a job running or queued, and whether stems are all that's left
 * for them (with the running stems job's progress, the furthest along). Jobs about no track (an export) don't count. */
export function pendingTracks(s: Pick<JobsState, "running" | "queued" | "queuedHash">): { tracks: Set<string>; stemsOnly: boolean; stemsProgress: number | null } {
  const tracks = new Set<string>();
  let stemsOnly = true;
  let stemsProgress: number | null = null;
  for (const j of Object.values(s.running)) {
    if (!j.hash) continue;
    tracks.add(j.hash);
    if (j.kind !== "stems") stemsOnly = false;
    else stemsProgress = Math.max(stemsProgress ?? 0, j.progress);
  }
  for (const [id, hash] of Object.entries(s.queuedHash)) {
    tracks.add(hash);
    if (s.queued[id] !== "stems") stemsOnly = false;
  }
  return { tracks, stemsOnly: tracks.size > 0 && stemsOnly, stemsProgress };
}

/** Each track's pending jobs: hash → { running: the running jobs' kinds and progress, queued: how many wait }. */
export function jobsByTrack(s: Pick<JobsState, "running" | "queued" | "queuedHash">): Map<string, { running: { kind: string; progress: number }[]; queued: number }> {
  const out = new Map<string, { running: { kind: string; progress: number }[]; queued: number }>();
  const at = (h: string) => {
    let e = out.get(h);
    if (!e) out.set(h, (e = { running: [], queued: 0 }));
    return e;
  };
  for (const j of Object.values(s.running)) if (j.hash) at(j.hash).running.push({ kind: j.kind, progress: j.progress });
  for (const h of Object.values(s.queuedHash)) at(h).queued += 1;
  return out;
}

/** How many jobs of each type are queued. */
export function queuedByKind(queued: Record<string, string>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const k of Object.values(queued)) out[k] = (out[k] ?? 0) + 1;
  return out;
}

function hashOf(row: { payload?: { hash?: unknown } }): string | null {
  return typeof row.payload?.hash === "string" ? row.payload.hash : null;
}

const now = () => performance.now();

/** Apply one job.* event to the state (exported for tests). */
export function applyJobEvent(s: JobsState, type: string, data: unknown, t: number = now()): Partial<JobsState> | null {
  const row = data as Partial<JobRow> & { id?: string; kind?: string; progress?: number };
  if (!row || typeof row.id !== "string") return null;
  const id = row.id;
  switch (type) {
    case "job.queued": {
      if (s.queued[id] || s.running[id] || typeof row.kind !== "string") return null;
      const hash = hashOf(row);
      return { queued: { ...s.queued, [id]: row.kind }, ...(hash ? { queuedHash: { ...s.queuedHash, [id]: hash } } : {}) };
    }
    case "job.started": {
      const { [id]: _, ...queued } = s.queued;
      const { [id]: queuedHashOf, ...queuedHash } = s.queuedHash;
      const kind = row.kind ?? s.queued[id] ?? "job";
      return { queued, queuedHash, running: { ...s.running, [id]: { id, kind, hash: hashOf(row) ?? queuedHashOf ?? null, progress: row.progress ?? 0, startedAt: t, seenStart: true } } };
    }
    case "job.progress": {
      const j = s.running[id];
      if (!j) {
        // started before we listened: follow it from here
        const kind = row.kind ?? "job";
        const { [id]: _, ...queued } = s.queued;
        const { [id]: queuedHashOf, ...queuedHash } = s.queuedHash;
        return { queued, queuedHash, running: { ...s.running, [id]: { id, kind, hash: hashOf(row) ?? queuedHashOf ?? null, progress: row.progress ?? 0, startedAt: t, seenStart: false } } };
      }
      return { running: { ...s.running, [id]: { ...j, progress: row.progress ?? j.progress } } };
    }
    case "job.done":
    case "job.failed":
    case "job.cancelled": {
      const j = s.running[id];
      const { [id]: _r, ...running } = s.running;
      const { [id]: _q, ...queued } = s.queued;
      const { [id]: _h, ...queuedHash } = s.queuedHash;
      const kind = row.kind ?? j?.kind ?? s.queued[id];
      const out: Partial<JobsState> = { running, queued, queuedHash };
      if (type === "job.done" && j?.seenStart && kind) {
        // learn from the jobs we saw from start to end: a moving average that settles after a few
        const took = (t - j.startedAt) / 1000;
        const prev = s.seconds[kind];
        out.seconds = { ...s.seconds, [kind]: prev === undefined ? took : prev * 0.7 + took * 0.3 };
      }
      return out;
    }
    default:
      return null;
  }
}

let started = false;
/** Load the queue and follow job.* events (once, from <App>). */
export function followJobs(): () => void {
  if (started) return () => {};
  started = true;
  const load = async () => {
    try {
      const [running, queued] = await Promise.all([api.jobs.list("running", { quiet: true }), api.jobs.list("queued", { quiet: true })]);
      const t = now();
      const workers: Record<string, number> = {};
      for (const [name, p] of Object.entries(running.status.pools)) workers[name] = p.size || p.workers || 1;
      useJobs.setState((s) => ({
        pools: running.pools ?? s.pools,
        workers: Object.keys(workers).length ? workers : s.workers,
        running: Object.fromEntries(running.items.map((j) => [j.id, s.running[j.id] ?? { id: j.id, kind: j.kind, hash: hashOf(j), progress: j.progress, startedAt: t, seenStart: false }])),
        queued: Object.fromEntries(queued.items.map((j) => [j.id, j.kind])),
        queuedHash: Object.fromEntries(queued.items.flatMap((j) => (hashOf(j) ? [[j.id, hashOf(j) as string]] : []))),
      }));
    } catch {
      // the engine isn't there yet: the events fill it in
    }
  };
  void load();
  const off = onServerEvent((ev) => {
    if (!ev.type.startsWith("job.")) return;
    const patch = applyJobEvent(useJobs.getState(), ev.type, ev.data);
    if (patch) useJobs.setState(patch);
  });
  return () => {
    started = false;
    off();
  };
}
