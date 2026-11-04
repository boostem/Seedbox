import { AlertTriangle, RotateCw } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import type { LibraryRow } from "@/api/types";
import { Button } from "@/components/ui";
import { plural } from "@/lib/format";
import { toast } from "@/store/toasts";

/** The jobs to run again: every failed job of every track. */
export function failedJobs(rows: LibraryRow[]): { kind: string; hash: string }[] {
  const out: { kind: string; hash: string }[] = [];
  for (const r of rows) {
    for (const [kind, state] of Object.entries(r.status.jobs)) if (state === "failed") out.push({ kind, hash: r.hash });
  }
  return out;
}

/** Above the table when analyses failed: how many, that the tracks still work, and Retry. */
export function FailedAnalysisBar({ rows }: { rows: LibraryRow[] }) {
  const jobs = useMemo(() => failedJobs(rows), [rows]);
  const tracks = useMemo(() => new Set(jobs.map((j) => j.hash)).size, [jobs]);
  const [busy, setBusy] = useState(false);
  if (!jobs.length) return null;
  const retry = async () => {
    setBusy(true);
    try {
      for (const j of jobs) await api.jobs.submit(j.kind, { hash: j.hash }, { quiet: true });
    } catch {
      toast.error("Couldn't queue the analysis again. Is the engine running?", { key: "analysis.retry", title: "Analysis" });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex items-center gap-2.5 border-b border-line bg-warn/8 px-5 py-2 text-sm" role="status">
      <AlertTriangle size={15} className="shrink-0 text-warn" />
      <span className="min-w-0 flex-1 text-fg-muted">
        <span className="font-medium text-fg">{plural(tracks, "track")} couldn't be fully analysed.</span> They still play and mix on what was found. The file may be damaged: retry, or replace it.
      </span>
      <Button size="sm" icon={RotateCw} loading={busy} onClick={() => void retry()}>
        Retry
      </Button>
    </div>
  );
}
