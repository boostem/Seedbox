/**
 * Session memory for the crate screen (module scope, gone on reload): the last Crate per seed so going back to
 * it is instant (it's refetched in the background), the user's ticks per seed, and the selected row.
 */
import type { Crate } from "@/api/types";

interface CrateMemo {
  crate: Crate;
  at: number;
}

const crates = new Map<string, CrateMemo>();
const ticks = new Map<string, Set<string>>();
const selected = new Map<string, string>();

export const crateMemo = {
  get: (seedId: string): Crate | undefined => crates.get(seedId)?.crate,
  set: (seedId: string, crate: Crate) => {
    crates.set(seedId, { crate, at: Date.now() });
    if (crate.seed.id !== seedId) crates.set(crate.seed.id, { crate, at: Date.now() });
  },
};

export const tickMemo = {
  get: (seedId: string): Set<string> | undefined => ticks.get(seedId),
  set: (seedId: string, s: Set<string>) => void ticks.set(seedId, s),
  clear: (seedId: string) => void ticks.delete(seedId),
};

export const selectionMemo = {
  get: (seedId: string) => selected.get(seedId) ?? null,
  set: (seedId: string, id: string | null) => (id ? selected.set(seedId, id) : selected.delete(seedId)),
};

/** Default ticks: the seed + every rec the server pre-ticks (ticked_default: all recs, or rule passes only). */
export function defaultTicks(crate: Crate): Set<string> {
  return new Set([crate.seed.id, ...crate.recs.filter((r) => r.ticked_default).map((r) => r.track.id)]);
}
