/** What the page and the audio thread say to each other. */
import type { MixPlan } from "../../api/mixplan.generated";
import type { DeckAudio, DeckId } from "./core";

export type EngineMessage =
  | { type: "stream"; key: string; frames: number; chunkFrames: number }
  | { type: "chunk"; key: string; index: number; left: Float32Array; right: Float32Array }
  | { type: "drop"; key: string; index: number }
  | { type: "plan"; plan: MixPlan; audio: Record<DeckId, DeckAudio> }
  | { type: "play"; atFrame?: number }
  | { type: "pause" }
  | { type: "seek"; beat: number }
  | { type: "loop"; startBeat: number | null; endBeat: number | null }
  | { type: "scrub"; beat: number }
  | { type: "endScrub" }
  | { type: "ping"; id: number };

export type EngineReport =
  | { type: "position"; frame: number; q: number; playing: boolean; missing: number; ended: boolean }
  | { type: "pong"; id: number };
