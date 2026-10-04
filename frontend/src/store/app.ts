/**
 * Global app store (zustand). Server-pushed state lives here so every screen sees the same live data:
 * previews by track id, jobs, analysis states, set versions, crate library, system status, settings, connection.
 * Read with selectors: `const p = useApp((s) => s.previews[id])` or the helper hooks at the bottom.
 */
import { useEffect, useState } from "react";
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";
import { api, MOCK } from "@/api/client";
import type { CrateSummary, FinalStatus, Job, JobItem, PreviewStatus, ServerEvent, SetMix, Settings, SetTrack, StemState, StemsStatus, SystemStatus, Track } from "@/api/types";
import { isSeedbox } from "@/lib/appMode";
import { toast } from "./toasts";

export type ConnectionState = "connecting" | "live" | "reconnecting" | "offline";
export type AnalysisState = SetTrack["analysis_state"];
export interface StemEntry {
  state: StemState;
  progress: number;
  error?: string | null;
}

/** What Space plays: the row the user last selected on any screen. */
export type Selection =
  | { kind: "preview"; track: Track } // a recommendation / seed → 20 s preview clip (player.playPreview)
  | { kind: "file"; track: Track; url: string }; // a downloaded file (SetTrack.audio_url)

export interface AppState {
  mock: boolean;
  connection: ConnectionState;
  /** Increments on every SSE (re)connect — include in effect deps to refetch after a reconnect. */
  epoch: number;
  system: SystemStatus | null;
  settings: Settings | null;
  crates: CrateSummary[] | null;
  previews: Record<string, PreviewStatus>;
  jobs: Record<string, Job>;
  /** analysis[folder][track_id] = state (from `analysis` events; seed it from CrateSet with seedAnalysis). */
  analysis: Record<string, Record<string, AnalysisState>>;
  /** stems[folder][track_id] = vocal splitting state + progress (from `stems` events; seed with seedStems). */
  stems: Record<string, Record<string, StemEntry>>;
  /** stemsAvailable[folder] = whether this install can split vocals (from the stems status), and why not. */
  stemsAvailable: Record<string, { available: boolean; reason: string | null }>;
  /** Set while the vocal model couldn't be downloaded (why, and when splitting tries again); null otherwise. */
  stemModelWaiting: string | null;
  /** setVersion[folder] bumps on `set_changed` — include in deps to refetch a CrateSet. */
  setVersion: Record<string, number>;
  /** setmixes[target] = the latest SetMix seen for "crate:<folder>" / "setlist:<id>" (from `setmix` events). */
  setmixes: Record<string, SetMix>;
  /** setlistVersion[id] bumps on `setlist_changed`; setlistsVersion on any of them (the setlist list). */
  setlistVersion: Record<string, number>;
  setlistsVersion: number;
  selection: Selection | null;

  setConnection: (c: ConnectionState) => void;
  bumpEpoch: () => void;
  setSystem: (s: SystemStatus) => void;
  setSettings: (s: Settings) => void;
  setCrates: (c: CrateSummary[]) => void;
  upsertPreview: (p: PreviewStatus) => void;
  upsertPreviews: (ps: PreviewStatus[]) => void;
  upsertJob: (j: Job) => void;
  /** GET /api/jobs/{id} and merge it into the store (quiet; returns null on failure). */
  refreshJob: (jobId: string) => Promise<Job | null>;
  seedAnalysis: (folder: string, tracks: Pick<SetTrack, "track" | "analysis_state">[]) => void;
  seedStems: (folder: string, status: StemsStatus) => void;
  /** GET the crate's vocal splitting state and seed it (quiet; older engines without the endpoint are ignored). */
  loadStems: (folder: string) => Promise<void>;
  upsertSetMix: (m: SetMix) => void;
  select: (sel: Selection | null) => void;
  handleEvent: (ev: ServerEvent) => void;
  /** (Re)load system status, settings, job history and the crate list. */
  refreshBasics: () => Promise<void>;
  refreshCrates: () => Promise<void>;
}

const pendingJobFetch = new Set<string>();
const FINAL_STATUSES: readonly string[] = ["downloaded", "copied", "exists", "skipped", "failed"];

/** Applies a live step text to an item; final texts also set status ("cancelled" → failed / reason cancelled). */
function applyStep(it: JobItem, text: string): JobItem {
  const t = text.trim().toLowerCase();
  if (it.status) return { ...it, text };
  if (FINAL_STATUSES.includes(t)) return { ...it, text, status: t as FinalStatus };
  if (t === "cancelled") return { ...it, text, status: "failed", reason: "cancelled" };
  return { ...it, text };
}

/** Merge a fetched job into the stored one without rolling back progress already received over SSE. */
export function mergeJob(fresh: Job, cur: Job | undefined): Job {
  if (!cur) return fresh;
  if (cur.finished_at && !fresh.finished_at) return cur; // job_done already landed
  return {
    ...fresh,
    items: fresh.items.map((it) => {
      const old = cur.items.find((o) => o.track.id === it.track.id);
      return !old || it.status || !old.status ? it : { ...it, text: old.text, status: old.status, reason: old.reason ?? it.reason };
    }),
  };
}

export const useApp = create<AppState>()((set, get) => ({
  mock: MOCK,
  connection: "connecting",
  epoch: 0,
  system: null,
  settings: null,
  crates: null,
  previews: {},
  jobs: {},
  analysis: {},
  stems: {},
  stemsAvailable: {},
  stemModelWaiting: null,
  setVersion: {},
  setmixes: {},
  setlistVersion: {},
  setlistsVersion: 0,
  selection: null,

  setConnection: (connection) => set({ connection }),
  bumpEpoch: () => set((s) => ({ epoch: s.epoch + 1 })),
  setSystem: (system) => set({ system }),
  setSettings: (settings) => set({ settings }),
  setCrates: (crates) => set({ crates }),
  upsertPreview: (p) => set((s) => ({ previews: { ...s.previews, [p.track_id]: p } })),
  upsertPreviews: (ps) =>
    set((s) => {
      const previews = { ...s.previews };
      for (const p of ps) previews[p.track_id] = p;
      return { previews };
    }),
  upsertJob: (j) => set((s) => ({ jobs: { ...s.jobs, [j.job_id]: j } })),
  refreshJob: async (jobId) => {
    if (pendingJobFetch.has(jobId)) return null;
    pendingJobFetch.add(jobId);
    try {
      const fresh = await api.job(jobId, { quiet: true });
      const merged = mergeJob(fresh, get().jobs[jobId]);
      get().upsertJob(merged);
      return merged;
    } catch {
      return null;
    } finally {
      pendingJobFetch.delete(jobId);
    }
  },
  seedAnalysis: (folder, tracks) =>
    set((s) => ({
      analysis: {
        ...s.analysis,
        [folder]: { ...s.analysis[folder], ...Object.fromEntries(tracks.map((t) => [t.track.id, t.analysis_state])) },
      },
    })),
  seedStems: (folder, status) =>
    set((s) => ({
      stems: { ...s.stems, [folder]: { ...s.stems[folder], ...status.tracks } },
      stemsAvailable: { ...s.stemsAvailable, [folder]: { available: status.available, reason: status.reason } },
      stemModelWaiting: status.waiting === undefined ? s.stemModelWaiting : status.waiting,
    })),
  loadStems: async (folder) => {
    try {
      get().seedStems(folder, await api.crateStems(folder, { quiet: true }));
    } catch {
      /* no stems endpoint / offline: the editor just shows nothing */
    }
  },
  select: (selection) => set({ selection }),
  upsertSetMix: (m) =>
    set((s) => {
      const cur = s.setmixes[m.target];
      // an older job's late event never replaces a newer one for the same set
      if (cur && cur.mix_id !== m.mix_id && cur.started_at > m.started_at) return {};
      return { setmixes: { ...s.setmixes, [m.target]: m } };
    }),

  handleEvent: (ev) => {
    switch (ev.type) {
      case "preview":
        get().upsertPreview(ev.data);
        break;
      case "job": {
        const { job_id, track_id, text } = ev.data;
        const job = get().jobs[job_id];
        if (!job) {
          void get().refreshJob(job_id);
          break;
        }
        get().upsertJob({ ...job, items: job.items.map((it) => (it.track.id === track_id ? applyStep(it, text) : it)) });
        break;
      }
      case "job_state": {
        const { job_id, state } = ev.data;
        const job = get().jobs[job_id];
        if (!job) {
          void get().refreshJob(job_id);
          break;
        }
        if (job.state !== state && !(job.finished_at && state === "running")) get().upsertJob({ ...job, state });
        // results (status/reason/genre/path) are applied server-side right before finalizing — pick them up now
        if (state === "finalizing") void get().refreshJob(job_id);
        break;
      }
      case "job_done": {
        get().upsertJob(ev.data);
        const counts = new Map<string, number>();
        for (const it of ev.data.items) if (it.status) counts.set(it.status, (counts.get(it.status) ?? 0) + 1);
        const summary = [...counts].map(([k, n]) => `${n} ${k}`).join(" · ");
        if (ev.data.state === "done") toast.success(summary || "Nothing to do", { title: `Crate ready: ${ev.data.folder}` });
        void get().refreshCrates();
        break;
      }
      case "analysis": {
        const { folder, track_id, state } = ev.data;
        set((s) => ({ analysis: { ...s.analysis, [folder]: { ...s.analysis[folder], [track_id]: state } } }));
        break;
      }
      case "stems": {
        const { folder, track_id, state, progress, error } = ev.data;
        // a queued song with an error = the vocal model download failed and is retried; work starting clears it
        const waiting = state === "queued" && error ? error : state === "queued" ? undefined : null;
        set((s) => ({
          stems: { ...s.stems, [folder]: { ...s.stems[folder], [track_id]: { state, progress, error: state === "queued" ? null : (error ?? null) } } },
          stemModelWaiting: waiting === undefined ? s.stemModelWaiting : waiting,
        }));
        break;
      }
      case "set_changed": {
        const { folder } = ev.data;
        set((s) => ({ setVersion: { ...s.setVersion, [folder]: (s.setVersion[folder] ?? 0) + 1 } }));
        void get().refreshCrates();
        break;
      }
      case "settings_changed": {
        const root = get().settings?.output_root;
        set({ settings: ev.data });
        if (ev.data.output_root !== root) {
          void get().refreshBasics(); // the system panel shows the crates folder
          void get().refreshCrates();
        }
        break;
      }
      case "setmix": {
        const m = ev.data;
        const prev = get().setmixes[m.target];
        get().upsertSetMix(m);
        const wasLive = prev?.mix_id === m.mix_id && prev.state !== m.state;
        if (wasLive && m.state === "done" && m.result) {
          const r = m.result;
          toast.success(`${m.title} · ${Math.round(r.duration_s / 60)} min · ${r.tracks.length} tracks`, {
            title: "Mix rendered",
            key: `setmix-${m.mix_id}`,
            action: {
              label: "Play",
              onClick: () => void import("@/audio/player").then(({ player }) => player.play(`setmix:${m.mix_id}`, r.audio_url, { track: setMixTrack(m), source: "file" })),
            },
          });
        } else if (wasLive && m.state === "error") toast.error(m.error ?? "The mix render failed", { title: `Mix failed: ${m.title}`, key: `setmix-${m.mix_id}` });
        break;
      }
      case "setlist_changed": {
        const { id } = ev.data;
        set((s) => ({ setlistVersion: { ...s.setlistVersion, [id]: (s.setlistVersion[id] ?? 0) + 1 }, setlistsVersion: s.setlistsVersion + 1 }));
        break;
      }
      case "toast":
        toast[ev.data.level](ev.data.message);
        break;
    }
  },

  refreshBasics: async () => {
    const [system, settings, jobs] = await Promise.allSettled([
      api.system({ quiet: true }),
      api.settings({ quiet: true }),
      isSeedbox ? api.jobs({ quiet: true }) : Promise.resolve([]), // download jobs live in Seedbox
    ]);
    if (system.status === "fulfilled") set({ system: system.value });
    if (settings.status === "fulfilled") set({ settings: settings.value });
    if (jobs.status === "fulfilled") set((s) => ({ jobs: { ...Object.fromEntries(jobs.value.map((j) => [j.job_id, j])), ...s.jobs } }));
    await get().refreshCrates();
  },
  refreshCrates: async () => {
    try {
      set({ crates: await api.crates({ quiet: true }) });
    } catch {
      /* offline: keep the last list */
    }
  },
}));

/** A Track-shaped label for the player / mini player while a set mix plays. */
export function setMixTrack(m: SetMix): Track {
  return {
    id: `setmix:${m.mix_id}`,
    name: m.title,
    artists: ["Setbox mix"],
    display_name: `Setbox mix - ${m.title}`,
    album: "",
    release_date: "",
    duration_s: m.result?.duration_s ?? 0,
    popularity: 0,
    key: "",
    camelot: "",
    bpm: 0,
    energy: 0,
    danceability: 0,
    cover_url: null,
    spotify_url: "",
  };
}

// ---- convenience hooks ----

export const usePreview = (trackId: string | null | undefined): PreviewStatus | undefined =>
  useApp((s) => (trackId ? s.previews[trackId] : undefined));

export const useJob = (jobId: string | null | undefined): Job | undefined => useApp((s) => (jobId ? s.jobs[jobId] : undefined));

export const useAnalysisState = (folder: string, trackId: string): AnalysisState | undefined =>
  useApp((s) => s.analysis[folder]?.[trackId]);

export const useSetVersion = (folder: string): number => useApp((s) => s.setVersion[folder] ?? 0);

export const useStemState = (folder: string, trackId: string): StemEntry | undefined => useApp((s) => s.stems[folder]?.[trackId]);

/** How many of `trackIds` (all of the crate's known tracks when omitted) have their vocals split / are splitting. */
export function useStemsSummary(folder: string, trackIds?: string[]): { ready: number; running: StemEntry | null; total: number; available: boolean; reason: string | null; waiting: string | null } {
  return useApp(
    useShallow((s) => {
      const rows = s.stems[folder] ?? {};
      const ids = trackIds ?? Object.keys(rows);
      let ready = 0;
      let running: StemEntry | null = null;
      for (const id of ids) {
        const r = rows[id];
        if (r?.state === "ready") ready += 1;
        else if (r?.state === "running") running = r;
      }
      const av = s.stemsAvailable[folder];
      return { ready, running, total: ids.length, available: av?.available ?? true, reason: av?.reason ?? null, waiting: s.stemModelWaiting };
    }),
  );
}

export const useSelection = (): Selection | null => useApp((s) => s.selection);

export const useSetMixLive = (target: string): SetMix | undefined => useApp((s) => s.setmixes[target]);

export const useSetlistVersion = (id: string): number => useApp((s) => (s.setlistVersion[id] ?? 0) + s.epoch * 1e6);

/** Jobs that are still running/finalizing (for badges). */
export const useActiveJobs = (): Job[] =>
  useApp(useShallow((s) => Object.values(s.jobs).filter((j) => j.state === "running" || j.state === "finalizing")));

/**
 * Refetch key for a crate's set: changes (debounced, default 400 ms) after `set_changed` or when any of the
 * crate's analyses finishes (ready/error) or a track's vocals finish splitting. The server sends bursts (queued/running/ready per track), so put this in
 * your effect deps instead of reacting to every event: useAsync(() => api.crateSet(folder), [folder, key]).
 */
export function useSetRefreshKey(folder: string, delayMs = 400): number {
  const raw = useApp((s) => {
    const done = Object.values(s.analysis[folder] ?? {}).filter((x) => x === "ready" || x === "error").length;
    const split = Object.values(s.stems[folder] ?? {}).filter((x) => x.state === "ready").length; // vocals feed the plans
    return (s.setVersion[folder] ?? 0) * 100000 + done + split * 1000 + s.epoch * 1e9;
  });
  return useDebounced(raw, delayMs);
}

/** Debounce any value (search terms, refetch keys). */
export function useDebounced<T>(value: T, delayMs: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    if (Object.is(v, value)) return;
    const t = setTimeout(() => setV(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs, v]);
  return v;
}
