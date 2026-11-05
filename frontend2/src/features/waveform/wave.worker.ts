/// <reference lib="webworker" />
/**
 * Waveform prep off the main thread: normalising the bands of an analysis's waveform, or fetching and decoding the
 * binary waveform file. The Float32Arrays come back transferred, not copied. The main-thread side is `waveData.ts`.
 */
import { decodeWaveBinary, prepareBands, type WaveData } from "./prepare";
import { fetchStemWave, type StemFetch } from "./stems";
import type { WaveRequest, WaveResponse } from "./waveData";

declare const self: DedicatedWorkerGlobalScope;

async function handle(req: WaveRequest): Promise<WaveData | StemFetch | null> {
  if (req.kind === "stems") return fetchStemWave(req.url);
  if (req.kind === "bands") return prepareBands(req.waveform, req.duration);
  const r = await fetch(req.url);
  if (!r.ok) throw new Error(`waveform: HTTP ${r.status}`);
  return decodeWaveBinary(await r.arrayBuffer(), req.duration);
}

function transferable(data: WaveData | StemFetch | null): ArrayBuffer[] {
  if (!data) return [];
  if ("state" in data) return data.state === "ready" ? [data.wave.drums, data.wave.bass, data.wave.vocals, data.wave.other].map((a) => a.buffer as ArrayBuffer) : [];
  return [data.low.buffer as ArrayBuffer, data.mid.buffer as ArrayBuffer, data.high.buffer as ArrayBuffer];
}

self.onmessage = async (e: MessageEvent<WaveRequest>) => {
  const { id } = e.data;
  try {
    const data = await handle(e.data);
    const msg: WaveResponse = { id, data };
    self.postMessage(msg, transferable(data));
  } catch (err) {
    const msg: WaveResponse = { id, error: (err as Error).message };
    self.postMessage(msg);
  }
};
