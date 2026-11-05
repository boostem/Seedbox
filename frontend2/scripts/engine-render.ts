// Renders a mix plan through the live engine's core, outside a browser, for tests/test_seedbox2_engine_parity.py:
//   deno run --no-config --no-lock --sloppy-imports --allow-read --allow-write scripts/engine-render.ts job.json
// job.json: { plan, audio: {a: DeckAudio, b: DeckAudio}, streams: {key: "file.f32"}, out: "out.f32", fold?: "fold.json" }.
// A stream file is raw little-endian float32, left channel then right. The output is written the same way, without
// the limiter's latency, so sample 0 is the window's first. With `fold`, also writes the plan with its stem lanes folded.
import { EngineCore } from "../src/audio/engine/core";
import { foldStemLanes } from "../src/audio/engine/fold";
import { LATENCY } from "../src/audio/engine/master";
import type { MixPlan } from "../src/api/mixplan.generated";

const CHUNK = 882000;
const QUANTUM = 128;

const job = JSON.parse(Deno.readTextFileSync(Deno.args[0]));
const dir = Deno.args[0].replace(/[^/\\]*$/, "");
const plan = job.plan as MixPlan;

if (job.fold) {
  Deno.writeTextFileSync(dir + job.fold, JSON.stringify(foldStemLanes(plan, () => false)));
}

const core = new EngineCore();
for (const [key, file] of Object.entries(job.streams as Record<string, string>)) {
  const bytes = Deno.readFileSync(dir + file);
  const data = new Float32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4);
  const frames = data.length / 2;
  core.addStream(key, frames, CHUNK);
  for (let i = 0; i * CHUNK < frames; i++) {
    const end = Math.min(frames, (i + 1) * CHUNK);
    core.setChunk(key, i, data.slice(i * CHUNK, end), data.slice(frames + i * CHUNK, frames + end));
  }
}
core.setPlan(plan, job.audio);
core.play();
const total = core.windowFrames + LATENCY;
const left = new Float32Array(total);
const right = new Float32Array(total);
const t0 = performance.now();
const block = job.block ?? QUANTUM;
for (let at = 0; at < total; at += block) {
  const n = Math.min(block, total - at);
  core.process(left.subarray(at, at + n), right.subarray(at, at + n), n, at);
}
const seconds = (performance.now() - t0) / 1000;
const frames = core.windowFrames;
const out = new Float32Array(frames * 2);
out.set(left.subarray(LATENCY, LATENCY + frames), 0);
out.set(right.subarray(LATENCY, LATENCY + frames), frames);
Deno.writeFileSync(dir + job.out, new Uint8Array(out.buffer));
console.log(JSON.stringify({ frames, seconds, missing: core.missing, cpu_percent: (100 * seconds * 44100) / total }));
