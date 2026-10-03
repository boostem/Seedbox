/** Pure set-builder helpers: optimistic re-ordering, placeholder transitions, set statistics. */
import type { CrateSet, SetTrack, Track, Transition } from "@/api/types";
import { camelotMove } from "@/lib/camelot";

/** A transition shown before the server has scored the pair (right after a re-order). */
export type SetTransition = Transition & { pending?: boolean };

export const pairKey = (from: string, to: string) => `${from}>${to}`;

/** Tempo change on B (in %) to match A, picking B's half/double time when that's closer (like mixing._pitch). */
export function pitchPct(aBpm: number, bBpm: number): number {
  if (!(aBpm > 0) || !(bBpm > 0)) return 0;
  let best = 0;
  let bestAbs = Infinity;
  for (const f of [1, 2, 0.5]) {
    const p = (aBpm / (bBpm * f) - 1) * 100;
    if (Math.abs(p) < bestAbs - 1e-9) [best, bestAbs] = [p, Math.abs(p)];
  }
  return Math.round(best * 100) / 100;
}

export function placeholderTransition(a: Track, b: Track): SetTransition {
  return {
    from_id: a.id,
    to_id: b.id,
    score: 0,
    key_move: camelotMove(a.camelot, b.camelot),
    pitch_pct: pitchPct(a.bpm, b.bpm),
    out_start_s: null,
    in_start_s: null,
    overlap_bars: null,
    swap_bar: null,
    technique: null,
    notes: [],
    warnings: [],
    ready: false,
    pending: true,
  };
}

/**
 * The set in a new order (optimistic UI): tracks re-numbered, transitions reused from the set itself or the
 * session cache when the pair is known, placeholders otherwise. Ids not in the set are ignored; tracks missing
 * from `ids` keep their relative order at the end (same rule as the server).
 */
export function withOrder(set: CrateSet, ids: string[], cache: Map<string, Transition>): CrateSet {
  const byId = new Map(set.tracks.map((t) => [t.track.id, t]));
  const seen = new Set<string>();
  const ordered: SetTrack[] = [];
  for (const id of ids) {
    const t = byId.get(id);
    if (t && !seen.has(id)) {
      seen.add(id);
      ordered.push(t);
    }
  }
  for (const t of set.tracks) if (!seen.has(t.track.id)) ordered.push(t);
  const known = new Map(set.transitions.map((t) => [pairKey(t.from_id, t.to_id), t]));
  const tracks = ordered.map((t, i) => (t.order === i + 1 ? t : { ...t, order: i + 1 }));
  const transitions: SetTransition[] = [];
  for (let i = 0; i + 1 < tracks.length; i++) {
    const a = tracks[i].track;
    const b = tracks[i + 1].track;
    const k = pairKey(a.id, b.id);
    transitions.push(known.get(k) ?? cache.get(k) ?? placeholderTransition(a, b));
  }
  return { ...set, tracks, transitions };
}

export const orderOf = (set: CrateSet | undefined | null): string[] => (set ? set.tracks.map((t) => t.track.id) : []);

export function sameOrder(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/** Move `id` by `delta` positions. */
export function moveBy(ids: string[], id: string, delta: number): string[] {
  const i = ids.indexOf(id);
  if (i < 0) return ids;
  const j = Math.max(0, Math.min(ids.length - 1, i + delta));
  if (i === j) return ids;
  const next = ids.filter((x) => x !== id);
  next.splice(j, 0, id);
  return next;
}

/** Put `id` directly after/before `anchor`. */
export function placeNextTo(ids: string[], id: string, anchor: string, where: "after" | "before"): string[] {
  if (id === anchor) return ids;
  const next = ids.filter((x) => x !== id);
  const k = next.indexOf(anchor);
  if (k < 0) return ids;
  next.splice(where === "after" ? k + 1 : k, 0, id);
  return next;
}

export interface SetStats {
  count: number;
  totalS: number;
  /** Estimated length when mixed as suggested (null until every transition has mix points). */
  mixedS: number | null;
  avgScore: number | null;
  worst: { index: number; t: Transition } | null;
  bpmMin: number | null;
  bpmMax: number | null;
  scored: number; // transitions with a server score
  ready: number; // transitions with mix points
}

export function setStats(set: CrateSet): SetStats {
  const tracks = set.tracks;
  const ts = set.transitions as SetTransition[];
  const totalS = tracks.reduce((s, t) => s + (t.track.duration_s || 0), 0);
  const scored = ts.filter((t) => !t.pending);
  const ready = ts.filter((t) => t.ready && !t.pending);
  let worst: SetStats["worst"] = null;
  ts.forEach((t, index) => {
    if (t.pending) return;
    if (!worst || t.score < worst.t.score) worst = { index, t };
  });
  const bpms = tracks.map((t) => t.track.bpm).filter((b) => b > 0);
  let mixedS: number | null = null;
  if (tracks.length && ready.length === ts.length) {
    // Σ (exit − cue) per track − Σ overlaps: B starts at its cue when A's overlap begins
    let sum = 0;
    for (let i = 0; i < tracks.length; i++) {
      const d = tracks[i].track.duration_s || 0;
      const into = i > 0 ? (ts[i - 1].in_start_s ?? 0) : 0;
      const out = i < ts.length ? ts[i] : null;
      const barA = 240 / (tracks[i].track.bpm || 120);
      const exit = out ? Math.min(d, (out.out_start_s ?? d) + (out.overlap_bars ?? 0) * barA) : d;
      sum += Math.max(0, exit - into);
      if (out) sum -= (out.overlap_bars ?? 0) * barA;
    }
    mixedS = Math.max(0, sum);
  }
  return {
    count: tracks.length,
    totalS,
    mixedS,
    avgScore: scored.length ? scored.reduce((s, t) => s + t.score, 0) / scored.length : null,
    worst,
    bpmMin: bpms.length ? Math.min(...bpms) : null,
    bpmMax: bpms.length ? Math.max(...bpms) : null,
    scored: scored.length,
    ready: ready.length,
  };
}

/** Score → tone used by chips/markers (independent of the key move). */
export function scoreTone(score: number): "ok" | "warn" | "danger" {
  return score >= 75 ? "ok" : score >= 55 ? "warn" : "danger";
}

type TechniqueFields = Pick<Transition, "technique" | "overlap_bars" | "swap_bar">;

/**
 * Compact technique label for rows/cards: "16-bar phrase blend", "8-bar bass swap", "8-bar blend → echo out",
 * "echo out", "quick cut". An echo-out with an overlap is a blend that ends in an echo, not an 8-bar echo.
 */
export function techniqueShort(t: TechniqueFields): string | null {
  if (!t.technique) return null;
  const n = t.overlap_bars && t.overlap_bars > 0 ? t.overlap_bars : 0;
  if (t.technique === "echo out") return n ? `${n}-bar blend → echo out` : "echo out";
  return n ? `${n}-bar ${t.technique}` : t.technique;
}

/** The plan spelled out for the mix view: "Blend 8 bars, swap bass at bar 5, then echo A out". */
export function techniqueLong(t: TechniqueFields): string | null {
  if (!t.technique) return null;
  const n = t.overlap_bars && t.overlap_bars > 0 ? t.overlap_bars : 0;
  const swap = n && t.swap_bar != null ? `, swap bass at bar ${t.swap_bar}` : "";
  if (t.technique === "echo out") return n ? `Blend ${n} bars${swap}, then echo A out` : "Echo A out, B in on the 1";
  if (!n) return t.technique[0].toUpperCase() + t.technique.slice(1);
  if (t.technique === "bass swap") return t.swap_bar != null ? `${n}-bar blend, swap bass at bar ${t.swap_bar}` : `${n}-bar bass swap`;
  return `${n}-bar ${t.technique}${swap}`;
}

/** The headline (first) note of a transition, without the trailing period. */
export function headline(t: Transition): string | null {
  const n = t.notes[0];
  return n ? n.replace(/\.$/, "") : null;
}

/**
 * Wrong-download check: the analysis had to detect its own tempo (tunebat's didn't fit the audio) and that tempo
 * isn't tunebat's ×1/×2/×½ within ±3 % — the file is most likely a different song than the tunebat track.
 */
export function tempoMismatch(trackBpm: number, an: { bpm: number; bpm_source: string } | null | undefined): boolean {
  if (!an || an.bpm_source !== "detected" || !(trackBpm > 0) || !(an.bpm > 0)) return false;
  return ![0.5, 1, 2].some((r) => Math.abs(an.bpm / trackBpm - r) / r <= 0.03);
}

export function tempoMismatchText(trackBpm: number, anBpm: number): string {
  return `Tempo doesn't match tunebat (audio ${anBpm.toFixed(1)} vs ${Math.round(trackBpm)} BPM) — probably a wrong download.`;
}
