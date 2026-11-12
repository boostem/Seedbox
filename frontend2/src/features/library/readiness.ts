/**
 * Whether a track can go into a set or mix (#100), in words: only usable tracks do (`status.usable`: ready, or failed
 * and "use anyway"), and the API refuses the others (#99). Everywhere a track is listed (the library, crates, sets,
 * Discover's downloads) an unusable one is dimmed with one of these badges, and can't be added to a set or mix.
 *
 * Live: the jobs store says what is running for a track before its row is read again, and how far its stems are.
 */
import type { LibraryRow } from "@/api/types";
import type { JobsState } from "@/store/jobs";

export type ReadinessTone = "busy" | "waiting" | "failed" | "anyway";

export interface Readiness {
  /** May go into a set or mix */
  usable: boolean;
  /** The badge, or null for a ready track */
  label: string | null;
  tone: ReadinessTone | null;
  /** Why it can't be added and what it lacks (or, used anyway, what it mixes without) */
  reason: string | null;
  /** Its analysis failed: offer "Retry analysis" and "Use anyway" */
  failed: boolean;
}

/** What the queue is doing for one track, as one comparable string (a cheap store selector): "" nothing, "queued",
 * "running", or "stems:NN" while its stems are separating (NN percent). */
export function liveJobs(s: Pick<JobsState, "running" | "queuedHash">, hash: string): string {
  let running = false;
  let stems: number | null = null;
  for (const j of Object.values(s.running)) {
    if (j.hash !== hash) continue;
    running = true;
    if (j.kind === "stems") stems = j.progress;
  }
  if (stems !== null) return `stems:${Math.round(stems * 100)}`;
  if (running) return "running";
  return Object.values(s.queuedHash).includes(hash) ? "queued" : "";
}

const NAMES: Record<string, string> = { beats: "beat grid", key: "key", energy: "energy", sections: "sections", vocals: "vocals", waveform: "waveform", stems: "stems" };

export function missingText(missing: string[]): string {
  return missing.map((m) => NAMES[m] ?? m).join(", ");
}

export function readiness(row: LibraryRow, live = ""): Readiness {
  const s = row.status;
  const missing = s.missing.length ? ` Missing: ${missingText(s.missing)}.` : "";
  const stemsLive = live.startsWith("stems:") ? Number(live.slice(6)) : null;
  const stems = stemsLive ?? (s.stems === "running" && s.stems_progress !== null ? Math.round(s.stems_progress * 100) : null);
  const analysing = live === "running" || stemsLive !== null || (live !== "queued" && s.state === "analysing");
  const queued = !analysing && (live === "queued" || s.state === "queued");
  // ready, even while something is analysed again (a new model version): it stays usable meanwhile
  if (s.ready) return { usable: true, label: null, tone: null, reason: null, failed: false };
  // "use anyway" counts for a failed track only: not once it is queued or analysing again
  if (s.usable && !analysing && !queued)
    return { usable: true, label: "Used anyway", tone: "anyway", reason: `Its analysis failed; you chose to use it anyway, so it mixes on what was found.${missing}`, failed: true };
  if (analysing) {
    const label = stems !== null ? `Analysing (stems ${stems}%)` : "Analysing";
    return { usable: false, label, tone: "busy", reason: `Not ready for sets and mixes: it's being analysed.${missing}`, failed: false };
  }
  if (queued) return { usable: false, label: "Waiting to be analysed", tone: "waiting", reason: `Not ready for sets and mixes: it's waiting to be analysed.${missing}`, failed: false };
  if (s.state === "failed")
    return { usable: false, label: "Analysis failed – retry", tone: "failed", reason: `Not ready for sets and mixes: its analysis failed. Retry it, or use it anyway.${missing}`, failed: true };
  return { usable: false, label: "Not analysed yet", tone: "waiting", reason: `Not ready for sets and mixes: it hasn't been analysed.${missing}`, failed: false };
}

/** The toast when tracks were kept out of a set: which, and why. */
export function refusedMessage(rows: LibraryRow[]): string {
  const name = (r: LibraryRow) => `“${r.title || r.hash.slice(0, 8)}”`;
  if (rows.length === 1) {
    const r = readiness(rows[0]);
    return `${name(rows[0])} isn't ready for sets and mixes (${(r.label ?? "not ready").replace(" – retry", "").toLowerCase()}).${rows[0].status.missing.length ? ` Missing: ${missingText(rows[0].status.missing)}.` : ""}`;
  }
  const names = rows.slice(0, 3).map(name).join(", ") + (rows.length > 3 ? ` and ${rows.length - 3} more` : "");
  return `${rows.length} tracks aren't ready for sets and mixes: ${names}. They can go in once they're analysed (or, if it failed, used anyway).`;
}
