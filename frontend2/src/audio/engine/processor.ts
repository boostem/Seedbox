/** The AudioWorklet: the engine core on the audio thread, driven by messages from `index.ts`. */
import { EngineCore } from "./core";
import type { EngineMessage } from "./messages";

declare const sampleRate: number;
declare const currentFrame: number;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}
declare function registerProcessor(name: string, ctor: unknown): void;

const POSITION_EVERY = 1764; // samples between position reports: 25 a second

class EngineProcessor extends AudioWorkletProcessor {
  private readonly core = new EngineCore();
  private sinceReport = 0;

  constructor() {
    super();
    if (sampleRate !== 44100) throw new Error(`the engine runs at 44100 Hz, not ${sampleRate}`);
    this.port.onmessage = (e: MessageEvent<EngineMessage>) => this.handle(e.data);
  }

  private handle(m: EngineMessage): void {
    const core = this.core;
    switch (m.type) {
      case "stream":
        core.addStream(m.key, m.frames, m.chunkFrames);
        break;
      case "chunk":
        core.setChunk(m.key, m.index, m.left, m.right);
        break;
      case "drop":
        core.dropChunk(m.key, m.index);
        break;
      case "plan":
        core.setPlan(m.plan, m.audio);
        break;
      case "play":
        core.play(m.atFrame ?? 0);
        break;
      case "pause":
        core.pause();
        break;
      case "seek":
        core.seek(m.beat);
        break;
      case "loop":
        core.setLoop(m.startBeat, m.endBeat);
        break;
      case "scrub":
        core.scrub(m.beat);
        break;
      case "endScrub":
        core.endScrub();
        break;
      case "master":
        core.setMaster(m.targetLufs, m.makeupDb);
        break;
      case "ping":
        this.port.postMessage({ type: "pong", id: m.id });
        break;
    }
  }

  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const out = outputs[0];
    const left = out[0];
    const right = out[1] ?? out[0];
    this.core.process(left, right, left.length, currentFrame);
    this.sinceReport += left.length;
    const ended = this.core.takeEnded();
    if (this.sinceReport >= POSITION_EVERY || ended) {
      this.sinceReport = 0;
      this.port.postMessage({
        type: "position",
        frame: currentFrame + left.length,
        q: this.core.position,
        playing: this.core.playing,
        missing: this.core.missing,
        ended,
      });
    }
    return true;
  }
}

registerProcessor("seedbox-engine", EngineProcessor);
