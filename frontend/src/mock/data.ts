/**
 * Mock library: real tunebat tracks (fixtures/tunebat.json, hand-maintained; see DESIGN.md). The crates themselves,
 * their order and everything scored come from the engine fixtures (mock/engine.ts).
 */
import type { CrateSummary, SearchPage, Track } from "@/api/types";
import fixtures from "./fixtures/tunebat.json" with { type: "json" };
import { hash } from "./rng";

export const SEARCH_PAGE = fixtures.search as SearchPage;
export const FAVOUR = fixtures.seed as Track;
const TUNEBAT_RECS = fixtures.recs as Track[];

export const POOL: Map<string, Track> = new Map();
for (const t of [FAVOUR, ...TUNEBAT_RECS, ...SEARCH_PAGE.items]) if (!POOL.has(t.id)) POOL.set(t.id, t);

export const HOME = "/Users/dj";

export interface MockCrate {
  folder: string;
  seed: Track | null;
  trackIds: string[]; // set order
  ratings: Record<string, number>; // energy ratings written when the crate was made
  updatedAt: string;
}

export function crateSummary(c: MockCrate, root: string): CrateSummary {
  return { folder: c.folder, path: `${root}/${c.folder}`, seed: c.seed, track_count: c.trackIds.length, updated_at: c.updatedAt };
}

export function genreFor(t: Track): string | null {
  const g = ["Tech House", "House", "Bass House", "Melodic House & Techno", "Afro House", "Tech House", "Deep House", null];
  return g[hash(t.id) % g.length];
}
