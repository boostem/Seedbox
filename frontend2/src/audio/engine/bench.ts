/**
 * What `python -m seedbox2.bench engine` runs: the real AudioWorklet in an OfflineAudioContext, 2 decks x 4 stems with
 * every stage on, as fast as it will go. Wall time over audio time is the share of one core the audio thread needs. Then the
 * same plan through the core on the page: they have to agree to the sample.
 */
import { audioOf, BPM, CHUNK, sources, worstPlan } from "./bench-plan";
import { SR } from "./constants";
import { EngineCore } from "./core";
import { LATENCY } from "./master";
import type { EngineMessage, EngineReport } from "./messages";
import workletUrl from "./processor?worker&url";

async function roundTrip(node: AudioWorkletNode): Promise<void> {
  await new Promise<void>((resolve) => {
    node.port.onmessage = (e: MessageEvent<EngineReport>) => {
      if (e.data.type === "pong") resolve();
    };
    node.port.postMessage({ type: "ping", id: 1 } satisfies EngineMessage);
  });
}

export interface EngineBenchResult {
  seconds: number;
  wall_s: number;
  cpu_percent: number;
  /** The worklet against the core run on the page: 0 means the same samples. */
  worklet_vs_core_max: number;
  peak: number;
  on_tempo: boolean;
}

export async function engineBench(seconds = 30, offTempo = false): Promise<EngineBenchResult> {
  const frames = Math.round(seconds * SR);
  const source = sources(frames + SR);
  const beats = Math.ceil((seconds * BPM) / 60);
  const plan = worstPlan(beats, offTempo);
  const audio = { a: audioOf("a"), b: audioOf("b") };

  const ctx = new OfflineAudioContext(2, frames, SR);
  await ctx.audioWorklet.addModule(workletUrl);
  const node = new AudioWorkletNode(ctx, "seedbox-engine", { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [2] });
  node.connect(ctx.destination);
  const send = (m: EngineMessage) => node.port.postMessage(m);
  const total = frames + SR;
  for (const s of source) {
    send({ type: "stream", key: s.key, frames: total, chunkFrames: CHUNK });
    for (let i = 0; i * CHUNK < total; i++) {
      const end = Math.min(total, (i + 1) * CHUNK);
      send({ type: "chunk", key: s.key, index: i, left: s.left.slice(i * CHUNK, end), right: s.right.slice(i * CHUNK, end) });
    }
  }
  send({ type: "plan", plan, audio });
  send({ type: "play" });
  await roundTrip(node); // everything above has reached the audio thread
  const t0 = performance.now();
  const rendered = await ctx.startRendering();
  const wall = (performance.now() - t0) / 1000;

  const core = new EngineCore();
  for (const s of source) {
    core.addStream(s.key, total, CHUNK);
    for (let i = 0; i * CHUNK < total; i++) {
      const end = Math.min(total, (i + 1) * CHUNK);
      core.setChunk(s.key, i, s.left.subarray(i * CHUNK, end), s.right.subarray(i * CHUNK, end));
    }
  }
  core.setPlan(plan, audio);
  core.play();
  const left = new Float32Array(frames);
  const right = new Float32Array(frames);
  for (let at = 0; at < frames; at += 128) {
    const n = Math.min(128, frames - at);
    core.process(left.subarray(at, at + n), right.subarray(at, at + n), n, at);
  }
  const l = rendered.getChannelData(0);
  const r = rendered.getChannelData(1);
  let diff = 0;
  let peak = 0;
  for (let i = LATENCY; i < frames; i++) {
    diff = Math.max(diff, Math.abs(l[i] - left[i]), Math.abs(r[i] - right[i]));
    peak = Math.max(peak, Math.abs(l[i]), Math.abs(r[i]));
  }
  return {
    seconds,
    wall_s: wall,
    cpu_percent: (100 * wall) / seconds,
    worklet_vs_core_max: diff,
    peak,
    on_tempo: !offTempo,
  };
}
