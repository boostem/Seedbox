/**
 * Version picker helpers: split "Favour - Extended Mix" / "Song (Chris Lake Remix)" into title + version label,
 * and group the results so every version of a song sits together (first-seen order, tunebat order inside).
 */
import type { Track } from "@/api/types";

const VERSION_WORDS =
  /\b(mix|edit|remix|version|dub|vip|rework|bootleg|remaster(ed)?|live|acoustic|instrumental|extended|radio|club|flip|reprise|mono|stereo|demo|cut|original|re-?recorded|sped up|slowed|unplugged|session|a cappella|acapella)\b/i;

export interface TitleParts {
  title: string;
  /** "Extended Mix", "Chris Lake Remix", "Remastered 2011"; null for the plain title. */
  version: string | null;
  /** "feat. Florence Arman" when present. */
  feat: string | null;
}

export function splitTitle(name: string): TitleParts {
  let title = name.trim();
  let version: string | null = null;
  let feat: string | null = null;

  const featM = /\s*[([]\s*((?:feat|ft|featuring|with)\.?\s[^)\]]+)[)\]]/i.exec(title);
  if (featM) {
    feat = featM[1].trim();
    title = (title.slice(0, featM.index) + title.slice(featM.index + featM[0].length)).trim();
  }
  // "Title - Extended Mix" (Spotify style)
  const dash = /\s[-–—]\s(.+)$/.exec(title);
  if (dash && VERSION_WORDS.test(dash[1])) {
    version = dash[1].trim();
    title = title.slice(0, dash.index).trim();
  }
  // "Title (Extended Mix)" / "Title [VIP]"
  const paren = /\s*[([]([^)\]]+)[)\]]\s*$/.exec(title);
  if (paren && VERSION_WORDS.test(paren[1])) {
    version = version ? `${paren[1].trim()} · ${version}` : paren[1].trim();
    title = title.slice(0, paren.index).trim();
  }
  return { title: title || name, version, feat };
}

function norm(s: string): string {
  return s
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

export function versionKey(t: Track): string {
  return `${norm(splitTitle(t.name).title)}|${norm(t.artists[0] ?? "")}`;
}

export interface ResultRow {
  track: Track;
  parts: TitleParts;
  /** group size (1 = unique) */
  groupSize: number;
  /** position in its group (0 = first / lead version) */
  groupIndex: number;
  /** the group's lead version (to highlight what differs) */
  lead: Track;
}

/** Rows in display order. `group` = keep versions of the same song together. */
export function buildRows(items: Track[], group: boolean): ResultRow[] {
  const keyed = items.map((t) => ({ t, k: versionKey(t), parts: splitTitle(t.name) }));
  const sizes = new Map<string, number>();
  for (const x of keyed) sizes.set(x.k, (sizes.get(x.k) ?? 0) + 1);
  if (!group) {
    const firstOf = new Map<string, Track>();
    const seen = new Map<string, number>();
    return keyed.map(({ t, k, parts }) => {
      if (!firstOf.has(k)) firstOf.set(k, t);
      const idx = seen.get(k) ?? 0;
      seen.set(k, idx + 1);
      return { track: t, parts, groupSize: sizes.get(k) ?? 1, groupIndex: idx, lead: firstOf.get(k)! };
    });
  }
  const order: string[] = [];
  const groups = new Map<string, typeof keyed>();
  for (const x of keyed) {
    if (!groups.has(x.k)) {
      groups.set(x.k, []);
      order.push(x.k);
    }
    groups.get(x.k)!.push(x);
  }
  const rows: ResultRow[] = [];
  for (const k of order) {
    const g = groups.get(k)!;
    g.forEach(({ t, parts }, i) => rows.push({ track: t, parts, groupSize: g.length, groupIndex: i, lead: g[0].t }));
  }
  return rows;
}

/** Which fields of `t` differ from its group's lead version (for highlighting). */
export function diffFromLead(t: Track, lead: Track) {
  return {
    title: t.name !== lead.name,
    album: t.album !== lead.album,
    date: t.release_date !== lead.release_date,
    duration: Math.abs(t.duration_s - lead.duration_s) >= 1.5,
    key: t.camelot !== lead.camelot,
    bpm: Math.abs(t.bpm - lead.bpm) >= 0.5,
    artists: t.artists.join("|") !== lead.artists.join("|"),
  };
}
