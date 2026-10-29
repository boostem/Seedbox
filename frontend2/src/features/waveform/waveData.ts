/**
 * Waveform data without blocking the main thread: `useWave(analysis)` and `loadWave(url)` hand the work to one
 * shared Web Worker (`wave.worker.ts`) and cache the result per waveform, so remounts and other views of the same
 * track get it at once. Where there are no workers (tests), the work runs synchronously.
 */
import { useEffect, useState } from "react";
import type { TrackAnalysis, Waveform } from "@/api/types";
import { decodeWaveBinary, prepareBands, type WaveData } from "./prepare";

type Job = { kind: "bands"; waveform: Waveform; duration: number } | { kind: "binary"; url: string; duration?: number };
export type WaveRequest = Job & { id: number };
export type WaveResponse = { id: number; data?: WaveData | null; error?: string };

let worker: Worker | null | undefined;
let nextId = 0;
const pending = new Map<number, { resolve: (d: WaveData | null) => void; reject: (e: Error) => void }>();

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
    else p?.resolve(e.data.data ?? null);
  });
  return worker;
}

async function inline(job: Job): Promise<WaveData | null> {
  if (job.kind === "bands") return prepareBands(job.waveform, job.duration);
  const r = await fetch(job.url);
  if (!r.ok) throw new Error(`waveform: HTTP ${r.status}`);
  return decodeWaveBinary(await r.arrayBuffer(), job.duration);
}

function run(job: Job): Promise<WaveData | null> {
  const w = getWorker();
  if (!w) return inline(job);
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    w.postMessage({ ...job, id });
  });
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
