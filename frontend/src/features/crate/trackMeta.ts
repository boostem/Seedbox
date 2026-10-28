/** Small, pure helpers shared by the search, crate and downloads screens. */
import type { Track } from "@/api/types";

/**
 * Tempo change needed to bring `bpm` to `seedBpm`, half/double-time aware (same as mixing.py `_pitch`):
 * the factor (1, 2 or ½) that lands closest to the seed wins.
 */
export function pitchVsSeed(seedBpm: number, bpm: number): { pct: number; factor: 1 | 2 | 0.5 } | null {
  if (!(seedBpm > 0) || !(bpm > 0)) return null;
  let factor: 1 | 2 | 0.5 = 1;
  for (const f of [2, 0.5] as const) if (Math.abs(bpm * f - seedBpm) < Math.abs(bpm * factor - seedBpm)) factor = f;
  return { pct: ((bpm * factor - seedBpm) / seedBpm) * 100, factor };
}

/** tunebat track page (their slug format is cosmetic — the ID at the end is what resolves). */
export function tunebatUrl(t: Pick<Track, "id" | "name" | "artists">): string {
  const slug = `${t.name} ${t.artists.join(" ")}`
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^A-Za-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `https://tunebat.com/Info/${slug || "track"}/${t.id}`;
}

/** True when the text is (or contains) a Spotify/tunebat track link or a bare 22-char Spotify ID. */
export function looksLikeTrackRef(term: string): boolean {
  const s = term.trim();
  if (/^[A-Za-z0-9]{22}$/.test(s)) return true;
  return /(open\.spotify\.com\/(intl-[a-z]+\/)?track\/|spotify:track:|tunebat\.com\/Info\/)/i.test(s) && /[A-Za-z0-9]{22}/.test(s);
}

/** 0-1 → "80". */
export const pct01 = (x: number | null | undefined) => (x == null || !Number.isFinite(x) ? "—" : String(Math.round(x * 100)));

/** "2026-04-01" → "1 Apr 2026"; "2005" → "2005". */
export function fmtReleaseDate(d: string | null | undefined): string {
  if (!d) return "—";
  if (/^\d{4}$/.test(d)) return d;
  const m = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(d);
  if (!m) return d;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const mon = months[Number(m[2]) - 1] ?? m[2];
  return m[3] ? `${Number(m[3])} ${mon} ${m[1]}` : `${mon} ${m[1]}`;
}
