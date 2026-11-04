/** Display formatting. Always render numbers inside `num` (tabular) elements. */

/** 194.9 → "3:15"; ≥1 h → "1:02:03"; null/NaN → "–:––". */
export function fmtTime(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s) || s < 0) return "–:––";
  const t = Math.floor(s);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

/** 128 → "128", 127.5 → "127.5", 0/invalid → "—". */
export function fmtBpm(bpm: number | null | undefined): string {
  if (!bpm || !Number.isFinite(bpm)) return "—";
  return Math.abs(bpm - Math.round(bpm)) < 0.05 ? String(Math.round(bpm)) : bpm.toFixed(1);
}

/** Pitch change, always signed: 1.53 → "+1.5%", -2.26 → "−2.3%", -0.04 → "±0.0%". */
export function fmtPitch(pct: number | null | undefined, digits = 1): string {
  if (pct == null || !Number.isFinite(pct)) return "—";
  const r = Number(pct.toFixed(digits));
  return `${r > 0 ? "+" : r < 0 ? "−" : "±"}${Math.abs(r).toFixed(digits)}%`;
}

/** Text colour class for a pitch change (how hard the deck has to be pushed): warns past ±4 %, danger past ±6 %. */
export function pitchTone(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return "text-fg-faint";
  const a = Math.abs(pct);
  if (a < 0.05) return "text-fg-subtle";
  if (a <= 4) return "text-fg-muted";
  if (a <= 6) return "text-warn";
  return "text-danger";
}

/** ISO date → "just now" / "5 min ago" / "3 h ago" / "yesterday" / "12 Sep" / "12 Sep 2024". */
export function fmtRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.round((now - t) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  if (s < 7 * 86400) return `${Math.round(s / 86400)} days ago`;
  const d = new Date(t);
  const sameYear = d.getFullYear() === new Date(now).getFullYear();
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", ...(sameYear ? {} : { year: "numeric" }) });
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** Energy rating 1-10 → stars 1-5 (rating ÷ 2 rounded up, as written to POPM). */
export function energyStars(rating: number | null | undefined): number {
  return rating ? Math.min(5, Math.max(1, Math.ceil(rating / 2))) : 0;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "/Users/x/Music/MusicReco/A - B" → "~/Music/MusicReco/A - B" (display only). */
export function tildify(path: string | null | undefined): string {
  if (!path) return "";
  return path.replace(/^\/Users\/[^/]+/, "~");
}

/** MB → "500 MB" / "1.5 GB". */
export function fmtMb(mb: number): string {
  return mb >= 1000 ? `${(mb / 1000).toFixed(mb % 1000 ? 1 : 0)} GB` : `${mb} MB`;
}

/** Bytes → "820 KB" / "82.1 MB" / "1.2 GB" (decimal units, as the OS's file managers show sizes). */
export function fmtBytes(bytes: number): string {
  if (bytes < 1e6) return `${Math.max(0, Math.round(bytes / 1e3))} KB`;
  if (bytes < 1e9) return `${(bytes / 1e6).toFixed(bytes < 1e7 ? 1 : 0)} MB`;
  return `${(bytes / 1e9).toFixed(1)} GB`;
}
