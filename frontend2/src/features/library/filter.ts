/**
 * The library's filters and smart crates (#73): pure functions over the rows of GET /api/library/view, so the table
 * filters and sorts 10,000 tracks on the client in a few milliseconds, with no round trip per keystroke.
 */
import type { AnalysisState, LibraryRow } from "@/api/types";
import { camelotAdjacent, parseCamelot } from "@/lib/camelot";

export interface LibraryFilter {
  /** Vibe search: every word must appear in the title, artist, album, genre, comment, label or key. */
  text: string;
  bpmMin: number | null;
  bpmMax: number | null;
  /** Count half and double tempo as a match (a 87 BPM track fits a 174 BPM range). */
  halfDouble: boolean;
  /** Only keys that mix with this Camelot code: the same, ±1 on the wheel or its relative. */
  keyCompat: string | null;
  /** 1-10, as Mixed In Key shows energy. */
  energyMin: number | null;
  energyMax: number | null;
  /** Only tracks whose analysis isn't done (new, queued, analysing or failed). */
  unanalysed: boolean;
}

export const EMPTY_FILTER: LibraryFilter = { text: "", bpmMin: null, bpmMax: null, halfDouble: false, keyCompat: null, energyMin: null, energyMax: null, unanalysed: false };

/** A saved filter, shown with the crates. */
export interface SmartCrate {
  id: string;
  name: string;
  filter: LibraryFilter;
}

/** Reads a stored filter back, dropping fields this version doesn't know and ones of the wrong type. */
export function cleanFilter(raw: Record<string, unknown> | null | undefined): LibraryFilter {
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const r = raw ?? {};
  return {
    text: typeof r.text === "string" ? r.text : "",
    bpmMin: num(r.bpmMin),
    bpmMax: num(r.bpmMax),
    halfDouble: r.halfDouble === true,
    keyCompat: typeof r.keyCompat === "string" ? (parseCamelot(r.keyCompat)?.code ?? null) : null,
    energyMin: num(r.energyMin),
    energyMax: num(r.energyMax),
    unanalysed: r.unanalysed === true,
  };
}

export function isEmptyFilter(f: LibraryFilter): boolean {
  return !activeFilters(f).length;
}

/** The filters in use, as short labels ("120-124 BPM", "fits 8A"), for chips and smart-crate names. */
export function activeFilters(f: LibraryFilter): string[] {
  const out: string[] = [];
  if (f.text.trim()) out.push(`“${f.text.trim()}”`);
  if (f.bpmMin !== null || f.bpmMax !== null) {
    const range = f.bpmMin !== null && f.bpmMax !== null ? `${f.bpmMin}-${f.bpmMax}` : f.bpmMin !== null ? `≥ ${f.bpmMin}` : `≤ ${f.bpmMax}`;
    out.push(`${range} BPM${f.halfDouble ? " (½×, 2×)" : ""}`);
  }
  if (f.keyCompat) out.push(`fits ${f.keyCompat}`);
  if (f.energyMin !== null || f.energyMax !== null) {
    out.push(`energy ${f.energyMin ?? 1}-${f.energyMax ?? 10}`);
  }
  if (f.unanalysed) out.push("not analysed yet");
  return out;
}

/** Lower case without accents, so "Beyoncé" matches "beyonce". */
export function fold(s: string): string {
  return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

function bpmFits(bpm: number | null, f: LibraryFilter): boolean {
  if (f.bpmMin === null && f.bpmMax === null) return true;
  if (bpm === null) return false;
  const lo = f.bpmMin ?? 0;
  const hi = f.bpmMax ?? Infinity;
  const inRange = (b: number) => b >= lo - 0.005 && b <= hi + 0.005;
  return inRange(bpm) || (f.halfDouble && (inRange(bpm * 2) || inRange(bpm / 2)));
}

/** 1-10 energy from the row's 0-1 value (null if unknown). */
export function energy10(e: number | null): number | null {
  return e === null ? null : Math.max(1, Math.min(10, Math.round(e * 10)));
}

/** A row → its search text, built once per row (cached on a WeakMap: rows are replaced, never mutated). */
const haystacks = new WeakMap<LibraryRow, string>();
function haystack(r: LibraryRow): string {
  let h = haystacks.get(r);
  if (h === undefined) {
    h = fold([r.title, r.artist, r.album, r.genre, r.text, r.key ?? "", r.format].join(" \u0001 "));
    haystacks.set(r, h);
  }
  return h;
}

/** A predicate for the filter (prepare once, run on every row). */
export function matcher(f: LibraryFilter): (r: LibraryRow) => boolean {
  const words = fold(f.text).split(/\s+/).filter(Boolean);
  const key = f.keyCompat ? parseCamelot(f.keyCompat)?.code : null;
  const eMin = f.energyMin ?? 1;
  const eMax = f.energyMax ?? 10;
  const energyOn = f.energyMin !== null || f.energyMax !== null;
  return (r) => {
    if (f.unanalysed && r.status.state === "done") return false;
    if (!bpmFits(r.bpm, f)) return false;
    if (key && !(r.key && camelotAdjacent(key, r.key))) return false;
    if (energyOn) {
      const e = energy10(r.energy);
      if (e === null || e < eMin || e > eMax) return false;
    }
    if (words.length) {
      const h = haystack(r);
      for (const w of words) if (!h.includes(w)) return false;
    }
    return true;
  };
}

export function filterRows(rows: readonly LibraryRow[], f: LibraryFilter, only?: ReadonlySet<string> | null): LibraryRow[] {
  const m = matcher(f);
  return rows.filter((r) => (!only || only.has(r.hash)) && m(r));
}

// ------------------------------------------------------------------------------------------------ sorting

export type SortKey = "title" | "artist" | "bpm" | "key" | "energy" | "duration" | "genre" | "added" | "status";
export interface Sort {
  key: SortKey;
  dir: "asc" | "desc";
}

const STATE_ORDER: Record<AnalysisState, number> = { analysing: 0, queued: 1, failed: 2, new: 3, done: 4 };
const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });

/** Camelot position as a number (1A = 1, 1B = 1.5 … 12B = 12.5), so keys sort around the wheel. */
function keyRank(code: string | null): number | null {
  const p = parseCamelot(code);
  return p ? p.n + (p.letter === "B" ? 0.5 : 0) : null;
}

function value(r: LibraryRow, key: SortKey): string | number | null {
  switch (key) {
    case "title":
      return r.title || null;
    case "artist":
      return r.artist || null;
    case "genre":
      return r.genre || null;
    case "bpm":
      return r.bpm;
    case "key":
      return keyRank(r.key);
    case "energy":
      return r.energy;
    case "duration":
      return r.duration;
    case "added":
      return r.added_at;
    case "status":
      return STATE_ORDER[r.status.state];
  }
}

/** A sorted copy; empty values go last whichever the direction, ties keep artist then title order. */
export function sortRows(rows: readonly LibraryRow[], sort: Sort): LibraryRow[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  const keyed = rows.map((r) => ({ r, v: value(r, sort.key) }));
  keyed.sort((a, b) => {
    if (a.v === null || b.v === null) {
      if (a.v !== b.v) return a.v === null ? 1 : -1;
    } else {
      const c = typeof a.v === "number" && typeof b.v === "number" ? a.v - b.v : collator.compare(String(a.v), String(b.v));
      if (c) return c * sign;
    }
    return collator.compare(a.r.artist, b.r.artist) || collator.compare(a.r.title, b.r.title);
  });
  return keyed.map((k) => k.r);
}
