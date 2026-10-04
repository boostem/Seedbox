/**
 * Progressive TrackAnalysis loading. GET /api/analysis/{folder}/{id} answers 404 while the analysis is queued or
 * running (api.analysis → null); we then wait for the `analysis` SSE event to say "ready" and fetch again.
 * Results are cached for the session (LRU, they carry ~20k-point waveforms).
 */
import { useEffect, useReducer } from "react";
import { ApiError, api } from "@/api/client";
import type { TrackAnalysis } from "@/api/types";
import { APP_NAME } from "@/lib/appMode";
import { type AnalysisState, useApp } from "@/store/app";

export type AnalysisView =
  | { status: "ready"; analysis: TrackAnalysis; stale: boolean }
  | { status: "loading" }
  | { status: "pending"; state: "queued" | "running" }
  | { status: "error"; message: string }
  | { status: "none" };

const MAX = 80; // a whole crate (rows check the tempo) plus the mix view
const cache = new Map<string, TrackAnalysis>();
const errors = new Map<string, string>();
const notReady = new Set<string>(); // fetched and got 404
const stale = new Set<string>(); // re-analysis in progress (file changed): keep showing the old one
const listeners = new Set<() => void>();

const k = (folder: string, id: string) => `${folder}\u0000${id}`;

function put(key: string, an: TrackAnalysis) {
  cache.delete(key);
  cache.set(key, an);
  while (cache.size > MAX) cache.delete(cache.keys().next().value as string);
}

function notify() {
  for (const l of listeners) l();
}

const inflight = new Map<string, Promise<void>>();

function load(folder: string, id: string): Promise<void> {
  const key = k(folder, id);
  const running = inflight.get(key);
  if (running) return running;
  const p = api
    .analysis(folder, id)
    .then((an) => {
      if (an) {
        put(key, an);
        notReady.delete(key);
        stale.delete(key);
      } else notReady.add(key);
      errors.delete(key);
    })
    .catch((e: unknown) => {
      const msg =
        e instanceof ApiError
          ? e.status === 503
            ? "Audio analysis isn't available in this build."
            : e.isNetwork
              ? `Can't reach the ${APP_NAME} engine.`
              : e.message
          : String(e);
      errors.set(key, msg);
    })
    .finally(() => {
      inflight.delete(key);
      notify();
    });
  inflight.set(key, p);
  return p;
}

/** Cached analysis (no fetch) — for code that only wants it if it's already here. */
export function cachedAnalysis(folder: string, id: string): TrackAnalysis | undefined {
  return cache.get(k(folder, id));
}

export function useTrackAnalysis(folder: string, trackId: string | null | undefined, fallback?: AnalysisState): AnalysisView {
  const live = useApp((s) => (trackId ? s.analysis[folder]?.[trackId] : undefined)) ?? fallback;
  const [, bump] = useReducer((x: number) => x + 1, 0);
  const key = trackId ? k(folder, trackId) : null;

  useEffect(() => {
    listeners.add(bump);
    return () => void listeners.delete(bump);
  }, []);

  useEffect(() => {
    if (!key || !trackId) return;
    if (live === "queued" || live === "running") {
      if (cache.has(key)) stale.add(key);
      errors.delete(key);
      return;
    }
    if (cache.has(key) && !stale.has(key)) return;
    if (errors.has(key) && live === "error") return;
    let retry: ReturnType<typeof setTimeout> | undefined;
    void load(folder, trackId).then(() => {
      // the store said ready but the server still 404s (event raced the fetch): try again shortly
      if (notReady.has(key) && live === "ready") retry = setTimeout(() => void load(folder, trackId), 1500);
    });
    return () => clearTimeout(retry);
  }, [key, live, folder, trackId]);

  if (!key) return { status: "none" };
  const an = cache.get(key);
  if (an) return { status: "ready", analysis: an, stale: stale.has(key) };
  const err = errors.get(key);
  if (err) return { status: "error", message: err };
  if (live === "running") return { status: "pending", state: "running" };
  if (live === "queued" || live === "missing" || notReady.has(key)) return { status: "pending", state: "queued" };
  if (live === "error") return { status: "error", message: "Analysis failed." };
  return { status: "loading" };
}
