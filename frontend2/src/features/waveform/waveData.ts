/**
 * Waveform data without blocking the main thread: `useWave(analysis)` and `loadWave(url)` hand the work to one
 * shared Web Worker (`wave.worker.ts`) and cache the result per waveform, so remounts and other views of the same
 * track get it at once. Where there are no workers (tests), the work runs synchronously.
 */
import { useCallback, useEffect, useState } from "react";
import type { TrackAnalysis, Waveform } from "@/api/types";
import { decodeWaveBinary, prepareBands, type WaveData } from "./prepare";
import { fetchStemWave, type StemFetch, type StemWave } from "./stems";

type Job = { kind: "bands"; waveform: Waveform; duration: number } | { kind: "binary"; url: string; duration?: number } | { kind: "stems"; url: string };
export type WaveRequest = Job & { id: number };
export type WaveResponse = { id: number; data?: WaveData | StemFetch | null; error?: string };

let worker: Worker | null | undefined;
let nextId = 0;
const pending = new Map<number, { resolve: (d: never) => void; reject: (e: Error) => void }>();

function getWorker(): Worker | null {
  if (worker !== undefined) return worker;
  try {
    worker = typeof Worker === "undefined" ? null : new Worker(new URL("./wave.worker.ts", import.meta.url), { type: "module" });
  } catch {
    worker = null;
  }
  worker?.addEventListener("message", (e: MessageEvent<WaveResponse>) => {
    const p = pending.get(e.data.id);
    pending.delete(e.data.id);
    if (e.data.error != null) p?.reject(new Error(e.data.error));
    else p?.resolve((e.data.data ?? null) as never);
  });
  return worker;
}

async function inline(job: Exclude<Job, { kind: "stems" }>): Promise<WaveData | null> {
  if (job.kind === "bands") return prepareBands(job.waveform, job.duration);
  const r = await fetch(job.url);
  if (!r.ok) throw new Error(`waveform: HTTP ${r.status}`);
  return decodeWaveBinary(await r.arrayBuffer(), job.duration);
}

function post<T>(w: Worker, job: Job): Promise<T> {
  const id = ++nextId;
  return new Promise<T>((resolve, reject) => {
    pending.set(id, { resolve: resolve as (d: never) => void, reject });
    w.postMessage({ ...job, id });
  });
}

function run(job: Exclude<Job, { kind: "stems" }>): Promise<WaveData | null> {
  const w = getWorker();
  return w ? post(w, job) : inline(job);
}

const done = new WeakMap<Waveform, WaveData | null>();
const inFlight = new WeakMap<Waveform, Promise<WaveData | null>>();

/** The analysis's waveform, prepared in the worker (cached per Waveform object). */
export function prepareWaveAsync(an: TrackAnalysis): Promise<WaveData | null> {
  const w = an.waveform;
  if (!w) return Promise.resolve(null);
  if (done.has(w)) return Promise.resolve(done.get(w) ?? null);
  let p = inFlight.get(w);
  if (!p) {
    p = run({ kind: "bands", waveform: w, duration: an.duration_s }).then((d) => {
      done.set(w, d);
      return d;
    });
    inFlight.set(w, p);
  }
  return p;
}

const binaries = new Map<string, Promise<WaveData | null>>();

/** A binary waveform file (#49), fetched and decoded in the worker (cached per URL). */
export function loadWave(url: string, duration?: number): Promise<WaveData | null> {
  let p = binaries.get(url);
  if (!p) {
    p = run({ kind: "binary", url: new URL(url, location.href).href, duration });
    p.catch(() => binaries.delete(url));
    binaries.set(url, p);
  }
  return p;
}

/** The prepared waveform of `an`: null until the worker has it (immediately when cached). */
export function useWave(an: TrackAnalysis | null | undefined): WaveData | null {
  const w = an?.waveform;
  const [state, setState] = useState<{ w: Waveform | undefined; data: WaveData | null }>(() => ({ w, data: (w && done.get(w)) ?? null }));
  useEffect(() => {
    if (!an || !w || done.has(w)) return;
    let live = true;
    prepareWaveAsync(an).then((data) => live && setState({ w, data }), () => live && setState({ w, data: null }));
    return () => {
      live = false;
    };
  }, [an, w]);
  if (!w) return null;
  return state.w === w ? state.data : (done.get(w) ?? null);
}

// ------------------------------------------------------------------------------------------------ stems

const STEM_KEEP = 16; // a 6-minute track's stems are about 860 KB here
const stemWaves = new Map<string, StemWave>();
const POLL_ANALYSING_MS = 1500;
const POLL_NO_STEMS_MS = 10_000;
const MAX_ANALYSING_POLLS = 40; // a minute: the job takes seconds, unless it keeps failing

export interface StemWaveState {
  state: "idle" | "loading" | "analysing" | "none" | "ready" | "failed";
  wave: StemWave | null;
  retry: () => void;
}

/** A track's stem waveform, fetched in the worker and kept for the last few tracks. It waits for what isn't there
 * yet: the stems job runs in the background, and the waveform job adds this file when it is done. */
export function useStemWave(hash: string | null | undefined): StemWaveState {
  const [got, setGot] = useState<{ hash: string; state: StemWaveState["state"]; wave: StemWave | null } | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!hash || stemWaves.has(hash)) return;
    let live = true;
    let timer = 0;
    let polls = 0;
    const url = new URL(`/api/tracks/${hash}/waveform/stems`, location.href).href;
    const fetchOnce = () => {
      const w = getWorker();
      return w ? post<StemFetch>(w, { kind: "stems", url }) : fetchStemWave(url);
    };
    const step = () => {
      fetchOnce().then(
        (r) => {
          if (!live) return;
          if (r.state === "ready") {
            stemWaves.set(hash, r.wave);
            for (const k of stemWaves.keys()) {
              if (stemWaves.size <= STEM_KEEP) break;
              stemWaves.delete(k);
            }
            setGot({ hash, state: "ready", wave: r.wave });
          } else if (r.state === "analysing" && ++polls > MAX_ANALYSING_POLLS) {
            setGot({ hash, state: "failed", wave: null });
          } else {
            setGot({ hash, state: r.state, wave: null });
            timer = window.setTimeout(step, r.state === "analysing" ? POLL_ANALYSING_MS : POLL_NO_STEMS_MS);
          }
        },
        () => live && setGot({ hash, state: "failed", wave: null }),
      );
    };
    setGot({ hash, state: "loading", wave: null });
    step();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [hash, attempt]);
  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  const kept = hash ? stemWaves.get(hash) : undefined;
  if (kept) return { state: "ready", wave: kept, retry };
  if (!hash) return { state: "idle", wave: null, retry };
  return got?.hash === hash ? { state: got.state, wave: got.wave, retry } : { state: "loading", wave: null, retry };
}
