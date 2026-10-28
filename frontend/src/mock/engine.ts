/**
 * What the Python engine answers for the mock crates, replayed from fixtures/engine.json (written by
 * scripts/engine-fixtures.py, `deno task fixtures:engine`): synthetic analyses, scored recs, running orders, energy
 * ratings, transitions, set plans and mix plans. Nothing here scores or plans anything itself; a pair or blend
 * length without a fixture falls back to the nearest one there is (see each function).
 */
import type { MixPlan, MixTechnique, Partner, Rec, Score, Track, TrackAnalysis, Transition } from "@/api/types";
import { placeholderTransition } from "@/features/set/model";
import fixtures from "./fixtures/engine.json" with { type: "json" };
import { POOL } from "./data";
import { rng } from "./rng";

type Bare = Omit<TrackAnalysis, "waveform">;
interface Fixtures {
  analyses: Record<string, Bare>;
  names: Record<string, string>;
  recs: Record<string, { id: string; score: Score; rule_reason: string }[]>;
  crates: { folder: string; seed_id: string | null; track_ids: string[]; hours_ago: number; ratings: Record<string, number> }[];
  ratings: Record<string, number>;
  sets: Record<string, Transition[]>;
  pairs: Record<string, Transition>;
  key_only: Record<string, Transition>;
  plans: Record<string, Record<string, Record<string, number>>>;
  plan_bodies: MixPlan[];
}
const fx = fixtures as unknown as Fixtures;

const clone = <T>(x: T): T => structuredClone(x);
const key = (a: string, b: string) => `${a}>${b}`;

/** File and folder name for a track (library.sanitize_filename). */
export function fileStem(t: Track): string {
  return fx.names[t.id] ?? t.display_name;
}

/** The mock library as the engine ordered it. */
export function initialCrates() {
  return fx.crates.map((c) => ({ ...c, track_ids: [...c.track_ids] }));
}

/** A track's energy rating: the crate's own (mixing.energy_ratings over it), else the one over every crate track. */
export function energyRating(crateRatings: Record<string, number>, id: string): number | null {
  return crateRatings[id] ?? fx.ratings[id] ?? null;
}

/** The running order transitions.order_set gives these tracks, when they are one of the fixture crates. */
export function engineOrder(ids: string[]): string[] | null {
  const want = [...ids].sort().join(",");
  return fx.crates.find((c) => [...c.track_ids].sort().join(",") === want)?.track_ids.slice() ?? null;
}

/** Recs for `seed` best first (engine.rank) with their pre-tick rule (mixing.passes_rule). */
export function recsFor(seed: Track, existing: Set<string>, folderPath: string, pretickAll = false): Rec[] {
  return (fx.recs[seed.id] ?? []).flatMap(({ id, score, rule_reason }) => {
    const t = POOL.get(id);
    if (!t) return [];
    const existing_path = existing.has(id) ? `${folderPath}/${fileStem(t)}.mp3` : null;
    return [{ track: t, score: clone(score), rule_reason, ticked_default: pretickAll || score.passed, existing_path }];
  });
}

// ---- analyses

const analyses = new Map<string, TrackAnalysis>();

/** The track's analysis, with a 3-band waveform drawn from its per-bar features (the engine reads none of it). */
export function analysisFor(t: Track): TrackAnalysis | null {
  const hit = analyses.get(t.id);
  if (hit) return hit;
  const bare = fx.analyses[t.id];
  if (!bare) return null;
  const an: TrackAnalysis = { ...clone(bare), waveform: waveform(bare) };
  analyses.set(t.id, an);
  return an;
}

const r3 = (x: number) => Math.round(Math.max(0, Math.min(1, x)) * 1000) / 1000;

function waveform(an: Bare): TrackAnalysis["waveform"] {
  const R = rng(`wf:${an.track_id}`);
  const points = Math.min(20000, Math.ceil(an.duration_s * 50)); // ~50 points/s capped at 20k
  const pps = points / an.duration_s;
  const fdb = an.first_downbeat_s;
  const beat = an.bar_s / 4;
  const low: number[] = new Array(points);
  const mid: number[] = new Array(points);
  const high: number[] = new Array(points);
  for (let i = 0; i < points; i++) {
    const ts = i / pps;
    const b = Math.max(0, Math.min(an.bar_count - 1, Math.floor((ts - fdb) / an.bar_s)));
    const ph = ((((ts - fdb) / beat) % 1) + 1) % 1;
    const kick = Math.exp(-ph * 9);
    const hat = Math.exp(-Math.abs(ph - 0.5) * 22);
    const e = an.energy_per_bar[b];
    const v = an.vocal_per_bar[b];
    const nz = R();
    low[i] = r3(an.low_per_bar[b] * (0.25 + 0.75 * kick) * (0.9 + nz * 0.1));
    mid[i] = r3(e * 0.45 + v * 0.3 + nz * 0.12 + kick * 0.08);
    high[i] = r3(e * 0.25 + hat * 0.35 * e + nz * 0.1);
  }
  return { points_per_second: pps, low, mid, high };
}

// ---- transitions

/** a → b (transitions.suggest): with both analyses when both are ready, else key/tempo only. A pair no fixture crate
 * holds (a download added it) gets the set builder's placeholder. */
export function transition(a: Track, b: Track, ready: boolean): Transition {
  const t = (ready ? fx.pairs : fx.key_only)[key(a.id, b.id)];
  if (t) return clone(t);
  const { pending: _, ...placeholder } = placeholderTransition(a, b);
  return { ...placeholder, notes: ["No engine fixture for this pair (mock mode)"] };
}

/** crate_set's transitions for this running order: the set plan's (setplan.annotate) when it is a fixture crate's
 * order, else each pair's own. */
export function setTransitions(tracks: Track[], ready: (id: string) => boolean): Transition[] {
  const planned = fx.sets[tracks.map((t) => t.id).join(",")];
  return tracks.slice(1).map((b, i) => {
    const a = tracks[i];
    const both = ready(a.id) && ready(b.id);
    return both && planned ? clone(planned[i]) : transition(a, b, both);
  });
}

/** transitions.partners: the best tracks to play after `me` ("out") or before it ("in"), best first. */
export function partners(me: Track, others: Track[], direction: "out" | "in", ready: (id: string) => boolean, limit = 8): Partner[] {
  return others
    .filter((t) => t.id !== me.id)
    .map((t) => ({ track: t, direction, transition: direction === "out" ? transition(me, t, ready(me.id) && ready(t.id)) : transition(t, me, ready(me.id) && ready(t.id)) }))
    .sort((x, y) =>
      y.transition.score - x.transition.score || Number(!x.transition.ready) - Number(!y.transition.ready) ||
      x.track.display_name.toLowerCase().localeCompare(y.track.display_name.toLowerCase()) || x.track.id.localeCompare(y.track.id)
    )
    .slice(0, limit);
}

// ---- mix plans

/** mixplan.generate for a → b in this crate: the set's pick (no technique), a technique at its own length, or at
 * `bars` blend length. Only the first crate has every length; elsewhere a length without a fixture gets the
 * technique's own. null: no fixture for the pair. */
export function mixPlan(folder: string, a: string, b: string, technique?: string | null, bars?: number | null): MixPlan | null {
  const entry = fx.plans[folder]?.[key(a, b)];
  if (!entry) return null;
  const tech = technique ?? (bars ? fx.plan_bodies[entry[""]].technique : null);
  const at = (k: string) => (entry[k] === undefined ? null : fx.plan_bodies[entry[k]]);
  const plan = (tech && bars ? at(`${tech}@${bars}`) : null) ?? (tech ? at(tech) : at(""));
  return plan && { ...clone(plan), edited: false };
}

export const TECHNIQUES: MixTechnique[] = ["long blend", "phrase blend", "bass swap", "drop swap", "quick cut", "echo out", "filter sweep", "loop roll", "slam", "reverb wash"];
