/** Shared state, sizes and types of the mix DAW (MixDaw.tsx) and its rows. */
import { create } from "zustand";
import type { MixPlan } from "@/api/types";
import type { View } from "@/features/waveform/draw";
import type { KeyMenu } from "./KeyList";
import type { DeckId, LaneKey } from "./lanes";

// ---- shared cursor state (hover beat, playhead beat, drag tooltip) — labels + overlay subscribe, rows don't
interface Cursor {
  hover: number | null;
  play: number | null;
  tip: { x: number; y: number; text: string } | null;
}
export const useCursor = create<Cursor>()(() => ({ hover: null, play: null, tip: null }));

export type Sel = Map<LaneKey, Set<number>>;

export const GUTTER = 132;
export const H_RULER = 30;
export const H_TEMPO = 44;
export const H_WAVE = 60;
export const H_LANE = 30;
export const H_LOOPS = 20;
export const H_FX = 20;

/** A loop or an effect event on one deck. */
export type RegionSel = { deck: DeckId; index: number } | null;

/** Everything the rows need to edit, behind stable refs (rows stay memoised during drags). */
export interface Editor {
  plan: () => MixPlan;
  sel: () => Sel;
  setSel: (s: Sel) => void;
  update: (p: MixPlan, opts?: { transient?: boolean }) => void;
  checkpoint: (p: MixPlan) => void;
  view: () => View;
  loopSel: () => RegionSel;
  setLoopSel: (s: RegionSel) => void;
  fxSel: () => RegionSel;
  setFxSel: (s: RegionSel) => void;
  menu: (m: Menu | null) => void;
  playFrom: (beat: number) => void;
}

export type Menu = KeyMenu;

export const snapTo = (v: number, step: number) => Math.round(v / step) * step;
export const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
