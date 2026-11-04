/**
 * Local music imports (#40): the latest `library.import` status (dropped files, Rekordbox/iTunes XML, watch folders)
 * and a counter that ticks when an import finishes, so the library page knows to reload. Finished imports toast.
 */
import { create } from "zustand";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { ImportStatus } from "@/api/types";
import { plural } from "@/lib/format";
import { toast } from "@/store/toasts";

interface ImportsState {
  status: ImportStatus;
  /** Bumped after every finished import. */
  generation: number;
  set: (status: ImportStatus) => void;
}

export const useImports = create<ImportsState>()((set) => ({
  status: { state: "idle" },
  generation: 0,
  set: (status) => set((s) => ({ status, generation: status.state === "done" || status.state === "failed" ? s.generation + 1 : s.generation })),
}));

/** What a finished import did, in a few words ("3 tracks added, 2 crates"). */
export function importSummary(s: ImportStatus): string {
  const parts: string[] = [];
  if (s.kind === "xml") parts.push(`${plural(s.tracks ?? 0, "track")} in ${plural(s.crates ?? 0, "crate")}`);
  if (s.added) parts.push(`${plural(s.added, "track")} added`);
  if (s.updated && !s.added && s.kind !== "xml") parts.push(`${s.updated} already in the library`);
  if (s.removed) parts.push(`${plural(s.removed, "file")} gone`);
  if (s.missing) parts.push(`${s.missing} not found`);
  if (s.error_count) parts.push(`${s.error_count} unreadable`);
  return parts.join(", ") || "Nothing new";
}

/** What to do about a failed import. */
export function importFix(s: ImportStatus): string {
  if (s.kind === "xml") return "Export it again from Rekordbox (File → Export Collection in xml format) or iTunes/Music (File → Library → Export Library), then choose that file.";
  if (s.kind === "watch") return "Check that the folder is still there and that the drive is connected, in Settings → Watch folders.";
  return "Check that the files are still there and are audio (MP3, AAC, FLAC, WAV, AIFF or OGG/Opus), then drop them again.";
}

function notify(s: ImportStatus) {
  const title = s.kind === "xml" ? "Playlists imported" : s.kind === "watch" ? "Watch folders" : "Import";
  if (s.state === "failed") toast.error(s.error ?? "Import failed", { key: "library.import", title });
  else if (s.state === "done" && (s.kind !== "watch" || s.added || s.removed)) {
    toast[s.error_count ? "warning" : "success"](importSummary(s), { key: "library.import", title });
  }
}

let started = false;
/** Follow `library.import` events (once, from <App>). */
export function followImports(): () => void {
  if (started) return () => {};
  started = true;
  void api.library.importStatus({ quiet: true }).then((s) => useImports.getState().set(s), () => {});
  const off = onServerEvent((ev) => {
    if (ev.type !== "library.import") return;
    const s = ev.data as ImportStatus;
    useImports.getState().set(s);
    notify(s);
  });
  return () => {
    started = false;
    off();
  };
}
