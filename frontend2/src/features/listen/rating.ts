import type { ListenRate, ListenScore, ListenScores, ListenSlot } from "@/api/types";

export const SLOTS: ListenSlot[] = ["a", "b"];
export const SCORES: { id: ListenScore; label: string }[] = [
  { id: "smoothness", label: "Smoothness" },
  { id: "energy", label: "Energy" },
  { id: "dj", label: "Would a DJ do this?" },
];

export const slotName = (slot: ListenSlot) => slot.toUpperCase();

const RATER_KEY = "seedbox2.listen.rater";

export function loadRater(): string {
  try {
    return localStorage.getItem(RATER_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveRater(name: string): void {
  try {
    localStorage.setItem(RATER_KEY, name);
  } catch {
    /* private mode: the name just isn't remembered */
  }
}

/** Sets a score; picking the same value again clears it, since scores are optional. */
export function withScore(scores: ListenScores, slot: ListenSlot, score: ListenScore, value: number): ListenScores {
  const side = { ...scores[slot] };
  if (side[score] === value) delete side[score];
  else side[score] = value;
  return { ...scores, [slot]: side };
}

const pct = (x: number) => `${Math.round(x * 100)}%`;

export function winRate(r: ListenRate): string {
  return r.win_rate == null ? "n/a" : pct(r.win_rate);
}

export function interval(r: ListenRate): string {
  return r.win_rate == null ? "n/a" : `${pct(r.ci_low)}–${pct(r.ci_high)}`;
}

/** Does the interval leave out a coin flip? Below that, the numbers don't say one side is better. */
export function verdict(r: ListenRate): "better" | "worse" | "unclear" {
  if (r.win_rate == null) return "unclear";
  if (r.ci_low > 0.5) return "better";
  return r.ci_high < 0.5 ? "worse" : "unclear";
}
