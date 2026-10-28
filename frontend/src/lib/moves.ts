/** Camelot move → colour/label. Colours: same=green, ±1=cyan, relative=yellow, boosts/diagonal=magenta, clash=red. */
import type { Move } from "@/api/types";

export type MoveTone = "same" | "step" | "relative" | "boost" | "clash" | "unknown";

export const MOVE_TONE: Record<Move, MoveTone> = {
  same: "same",
  "+1": "step",
  "-1": "step",
  relative: "relative",
  "+2 boost": "boost",
  "+7 boost": "boost",
  diagonal: "boost",
  clash: "clash",
  "?": "unknown",
};

/** CSS colour value (a token var) for a move — usable in style={{ color }} and canvas via cssVar(). */
export const MOVE_COLOR_VAR: Record<MoveTone, string> = {
  same: "var(--color-move-same)",
  step: "var(--color-move-step)",
  relative: "var(--color-move-relative)",
  boost: "var(--color-move-boost)",
  clash: "var(--color-move-clash)",
  unknown: "var(--color-move-unknown)",
};

export const MOVE_LABEL: Record<Move, string> = {
  same: "Same key",
  "+1": "+1",
  "-1": "−1",
  relative: "Relative",
  "+2 boost": "+2 boost",
  "+7 boost": "+7 boost",
  diagonal: "Diagonal",
  clash: "Clash",
  "?": "Key ?",
};

export const MOVE_HELP: Record<Move, string> = {
  same: "Same key — always harmonic",
  "+1": "+1 on the wheel (a fifth up) — smooth lift",
  "-1": "−1 on the wheel (a fifth down) — smooth",
  relative: "Relative major/minor — same notes, mood change",
  "+2 boost": "+2 energy boost",
  "+7 boost": "+7 energy boost (semitone up)",
  diagonal: "Diagonal (±1 and other letter)",
  clash: "Key clash — use a cut or echo out",
  "?": "Unknown key",
};

export function moveColor(move: Move): string {
  return MOVE_COLOR_VAR[MOVE_TONE[move] ?? "unknown"];
}
