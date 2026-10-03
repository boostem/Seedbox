/**
 * Mock fixtures: real tunebat tracks (fixtures/tunebat.json, generated from ../tracks.txt + ../search.txt by
 * `deno task fixtures`) + a TS port of musicreco/mixing.py score()/passes_rule() so recs look exactly like the
 * backend's. Extra "recs" from the search fixture are added so the crate has rule-failing (unticked) rows too.
 */
import type { CrateSummary, Move, Rec, Score, SearchPage, Track } from "@/api/types";
import { camelotMove, parseCamelot } from "@/lib/camelot";
import fixtures from "./fixtures/tunebat.json" with { type: "json" };
import { hash } from "./rng";

export const SEARCH_PAGE = fixtures.search as SearchPage;
export const FAVOUR = fixtures.seed as Track;
export const TUNEBAT_RECS = fixtures.recs as Track[];
/** FISHER tracks from the search fixture used as extra (mostly rule-failing) recommendations. */
const EXTRA_REC_IDS = ["1xtDflWAisSMU7k6QHG71U", "5zX4JjkoT2pgvlWPnjgVNQ", "5bIJREvmwLztjkTP5am1u9"];
const extraRecs = SEARCH_PAGE.items.filter((t) => EXTRA_REC_IDS.includes(t.id) || /^FISHER/.test(t.artists[0] ?? "") && t.bpm >= 120);

export const POOL: Map<string, Track> = new Map();
for (const t of [FAVOUR, ...TUNEBAT_RECS, ...SEARCH_PAGE.items]) if (!POOL.has(t.id)) POOL.set(t.id, t);

export const HOME = "/Users/dj";

// ---- mixing.py port ----

const KEY_POINTS: Record<Move, number> = { same: 60, "+1": 48, "-1": 48, relative: 48, "+2 boost": 32, diagonal: 30, "+7 boost": 28, clash: 0, "?": 0 };
const MOVE_CHIPS: Partial<Record<Move, string>> = { same: "same key", clash: "key clash", "?": "key ?" };
const EPS = 1e-9;

const validBpm = (b: number) => Number.isFinite(b) && b > 0;

export function bpmFactor(seed: number, other: number, tol: number): number | null {
  if (!validBpm(seed) || !validBpm(other)) return null;
  const limit = seed * tol + EPS;
  for (const f of [1, 2, 0.5]) if (Math.abs(other * f - seed) <= limit) return f;
  return null;
}

export function passesRule(seed: Track, other: Track, tol = 0.1): [boolean, string] {
  const m = camelotMove(seed.camelot, other.camelot);
  const keyOk = m === "same" || m === "+1" || m === "-1" || m === "relative";
  const p = parseCamelot(other.camelot);
  const keyLabel = p ? p.code : other.camelot.trim() || "?";
  const factor = bpmFactor(seed.bpm, other.bpm, tol);
  const mark = factor !== null ? "✓" : "✗";
  let bpmLabel: string;
  if (!validBpm(other.bpm)) bpmLabel = `? ${mark}`;
  else if (factor === 2 || factor === 0.5) {
    const note = factor === 2 ? "half-time" : "double-time";
    bpmLabel = `${Math.round(other.bpm)}→${Math.round(other.bpm * factor)} ${mark} (${note})`;
  } else bpmLabel = `${Math.round(other.bpm)} ${mark}`;
  return [keyOk && factor !== null, `${keyLabel} ${keyOk ? "✓" : "✗"}  ${bpmLabel}`];
}

export function pitch(seedBpm: number, otherBpm: number): [number, number] | null {
  if (!validBpm(seedBpm) || !validBpm(otherBpm)) return null;
  const factor = [1, 2, 0.5].reduce((best, f) => (Math.abs(otherBpm * f - seedBpm) < Math.abs(otherBpm * best - seedBpm) ? f : best), 1);
  return [((otherBpm * factor - seedBpm) / seedBpm) * 100, factor];
}

const closeness = (gap: number, points: number) => points * Math.max(0, 1 - Math.abs(gap) / 0.3);
const signed = (x: number, d: number) => `${x >= 0 ? "+" : ""}${x.toFixed(d)}`;

export function score(seed: Track, other: Track, tol = 0.1): Score {
  const passed = passesRule(seed, other, tol)[0];
  const move = camelotMove(seed.camelot, other.camelot);
  let raw = KEY_POINTS[move];
  const chips = [MOVE_CHIPS[move] ?? move];
  const p = pitch(seed.bpm, other.bpm);
  if (!p) chips.push("no BPM");
  else {
    const [pct, factor] = p;
    raw += 25 * Math.max(0, 1 - Math.abs(pct) / 10);
    chips.push(Math.round(pct * 10) ? `${signed(pct, 1)}% pitch` : "same tempo");
    if (factor !== 1) {
      raw -= 3;
      chips.push(factor === 2 ? "half-time" : "double-time");
    }
  }
  if (seed.energy > 0 && other.energy > 0) {
    const de = other.energy - seed.energy;
    raw += closeness(de, 10);
    if (Math.round(de * 100)) chips.push(`energy ${signed(de, 2)}`);
  } else raw += 5;
  if (seed.danceability > 0 && other.danceability > 0) {
    const dd = other.danceability - seed.danceability;
    raw += closeness(dd, 5);
    if (Math.abs(dd) >= 0.1) chips.push(`dance ${signed(dd, 2)}`);
  } else raw += 2.5;
  raw = Math.max(0, Math.min(100, raw));
  const value = passed ? Math.max(Math.round(raw), 48) : Math.min(Math.round(raw * 0.6), 47);
  return { value: Math.min(value, 100), passed, move, chips };
}

/** Recommendations for any seed: tunebat's r list + extras, scored and sorted like the backend. ticked_default = pretickAll || passed. */
export function recsFor(seed: Track, tol: number, existing: Set<string>, folderPath: string, pretickAll = false): Rec[] {
  const seen = new Set([seed.id]);
  const recs: Rec[] = [];
  for (const t of [...TUNEBAT_RECS, ...extraRecs, ...(seed.id === FAVOUR.id ? [] : [FAVOUR])]) {
    if (seen.has(t.id) || t.display_name === seed.display_name) continue;
    seen.add(t.id);
    const s = score(seed, t, tol);
    recs.push({
      track: t,
      score: s,
      rule_reason: passesRule(seed, t, tol)[1],
      ticked_default: pretickAll || s.passed,
      existing_path: existing.has(t.id) ? `${folderPath}/${sanitize(t.display_name)}.mp3` : null,
    });
  }
  return recs.sort((a, b) => b.score.value - a.score.value);
}

export function sanitize(s: string): string {
  return s.replace(/[/\\:*?"<>|]/g, "_").replace(/\s+/g, " ").trim().slice(0, 180);
}

// ---- energy rating + order (simplified mixing.py) ----

export function energyRatings(tracks: Track[]): Record<string, number> {
  const es = tracks.map((t) => t.energy * 0.7 + t.danceability * 0.3);
  const mean = es.reduce((a, b) => a + b, 0) / Math.max(1, es.length);
  const sd = Math.sqrt(es.reduce((a, b) => a + (b - mean) ** 2, 0) / Math.max(1, es.length)) || 0.05;
  return Object.fromEntries(tracks.map((t, i) => [t.id, Math.max(1, Math.min(10, Math.round(5.5 + ((es[i] - mean) / sd) * 1.9)))]));
}

const KEY_COST: Partial<Record<Move, number>> = { same: 0, "+1": 1, "-1": 1, relative: 1, "+2 boost": 2, "+7 boost": 2, diagonal: 2 };

export function orderSet(seed: Track | null, tracks: Track[]): Track[] {
  const rest = tracks.filter((t) => t.id !== seed?.id);
  const out: Track[] = seed ? [seed] : rest.length ? [rest.shift()!] : [];
  const n = rest.length + out.length;
  while (rest.length) {
    const last = out[out.length - 1];
    const pos = out.length / Math.max(1, n - 1);
    const target = pos < 0.66 ? 0.72 + pos * 0.3 : 0.92 - (pos - 0.66) * 0.5;
    let best = 0;
    let bestCost = Infinity;
    rest.forEach((t, i) => {
      const k = KEY_COST[camelotMove(last.camelot, t.camelot)] ?? 5;
      const p = Math.abs(pitch(last.bpm, t.bpm)?.[0] ?? 10);
      const cost = k + p * 0.5 + Math.abs(t.energy - target) * 4;
      if (cost < bestCost) [best, bestCost] = [i, cost];
    });
    out.push(rest.splice(best, 1)[0]);
  }
  return out;
}

// ---- crate library ----

export interface MockCrate {
  folder: string;
  seed: Track | null;
  trackIds: string[]; // set order
  updatedAt: string;
}

function ago(hours: number): string {
  return new Date(Date.now() - hours * 3600_000).toISOString();
}

function topFor(seed: Track, n: number): Track[] {
  return recsFor(seed, 0.1, new Set(), "").slice(0, n).map((r) => r.track);
}

function crateFor(seed: Track | null, tracks: Track[], hoursAgo: number, folder?: string): MockCrate {
  const ordered = orderSet(seed, tracks);
  return { folder: folder ?? sanitize(seed!.display_name), seed, trackIds: ordered.map((t) => t.id), updatedAt: ago(hoursAgo) };
}

const byId = (id: string) => POOL.get(id)!;

export function initialCrates(): MockCrate[] {
  const favourRecs = recsFor(FAVOUR, 0.1, new Set(), "").filter((r) => r.score.passed).map((r) => r.track);
  const rain = byId("1xtDflWAisSMU7k6QHG71U");
  const moreBaby = TUNEBAT_RECS.find((t) => t.name === "More Baby") ?? TUNEBAT_RECS[6];
  const dontStop = TUNEBAT_RECS.find((t) => t.name === "Don't Stop") ?? TUNEBAT_RECS[1];
  const atmosphere = SEARCH_PAGE.items.find((t) => t.name === "Atmosphere") ?? SEARCH_PAGE.items[5];
  const shesGone = TUNEBAT_RECS[0];
  return [
    crateFor(FAVOUR, [FAVOUR, ...favourRecs.slice(0, 7)], 2.2),
    crateFor(rain, [rain, ...topFor(rain, 8)], 26),
    crateFor(moreBaby, [moreBaby, ...topFor(moreBaby, 10)], 74),
    crateFor(dontStop, [dontStop, ...topFor(dontStop, 7)], 24 * 12),
    crateFor(atmosphere, [atmosphere, ...topFor(atmosphere, 5)], 24 * 40),
    crateFor(shesGone, [shesGone, ...topFor(shesGone, 11)], 24 * 64),
    crateFor(null, TUNEBAT_RECS.slice(3, 10), 24 * 300, "Warm-up Ideas (old crate)"),
  ];
}

export function crateSummary(c: MockCrate, root: string): CrateSummary {
  return { folder: c.folder, path: `${root}/${c.folder}`, seed: c.seed, track_count: c.trackIds.length, updated_at: c.updatedAt };
}

export function genreFor(t: Track): string | null {
  const g = ["Tech House", "House", "Bass House", "Melodic House & Techno", "Afro House", "Tech House", "Deep House", null];
  return g[hash(t.id) % g.length];
}
