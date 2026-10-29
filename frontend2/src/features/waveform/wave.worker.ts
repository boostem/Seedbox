/// <reference lib="webworker" />
/**
 * Waveform prep off the main thread: normalising the bands of an analysis's waveform, or fetching and decoding the
 * binary waveform file. The Float32Arrays come back transferred, not copied. The main-thread side is `waveData.ts`.
 */
import { decodeWaveBinary, prepareBands, type WaveData } from "./prepare";
import type { WaveRequest, WaveResponse } from "./waveData";

declare const self: DedicatedWorkerGlobalScope;

async function handle(req: WaveRequest): Promise<WaveData | null> {
  if (req.kind === "bands") return prepareBands(req.waveform, req.duration);
  const r = await fetch(req.url);
  if (!r.ok) throw new Error(`waveform: HTTP ${r.status}`);
  return decodeWaveBinary(await r.arrayBuffer(), req.duration);
}

self.onmessage = async (e: MessageEvent<WaveRequest>) => {
  const { id } = e.data;
  try {
    const data = await handle(e.data);
    const msg: WaveResponse = { id, data };
    self.postMessage(msg, data ? [data.low.buffer, data.mid.buffer, data.high.buffer] : []);
  } catch (err) {
    const msg: WaveResponse = { id, error: (err as Error).message };
    self.postMessage(msg);
  }
};
