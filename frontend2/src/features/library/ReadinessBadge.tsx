/**
 * A track that can't go into a set or mix yet, said where it is listed (#100): "Waiting to be analysed", "Analysing
 * (stems 40%)", "Analysis failed – retry" (with "Retry analysis" and "Use anyway"), or "Used anyway". Nothing for a
 * ready track. It follows the job queue live, so it goes away (and the track becomes usable) without a reload.
 */
import { AlertTriangle, CheckCheck, Clock, RotateCw, Undo2 } from "lucide-react";
import { type MouseEvent, useState } from "react";
import type { LibraryRow } from "@/api/types";
import { Badge, type BadgeTone, IconButton, Spinner, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useJobs } from "@/store/jobs";
import { retryAnalysis, setUseAnyway } from "@/store/library";
import { toast } from "@/store/toasts";
import { liveJobs, type Readiness, readiness, type ReadinessTone } from "./readiness";

const TONE: Record<ReadinessTone, BadgeTone> = { busy: "accent", waiting: "neutral", failed: "danger", anyway: "warn" };

/** The track's readiness, following the queue. */
export function useReadiness(row: LibraryRow): Readiness {
  const live = useJobs((s) => liveJobs(s, row.hash));
  return readiness(row, live);
}

export function ReadinessBadge({ row, size = "xs", className }: { row: LibraryRow; size?: "xs" | "sm"; className?: string }) {
  const r = useReadiness(row);
  const [busy, setBusy] = useState<"retry" | "anyway" | null>(null);
  if (!r.label || !r.tone) return null;
  const run = (what: "retry" | "anyway", fn: () => Promise<void>) => async (e: MouseEvent) => {
    e.stopPropagation();
    setBusy(what);
    try {
      await fn();
    } catch {
      toast.error(what === "retry" ? "Couldn't queue the analysis again. Is the engine running?" : "Couldn't change “use anyway”", { key: "analysis.retry", title: "Analysis" });
    } finally {
      setBusy(null);
    }
  };
  const retry = run("retry", () => retryAnalysis(row));
  return (
    <span className={cn("inline-flex shrink-0 items-center gap-0.5", className)} data-readiness={r.tone}>
      <Tooltip content={r.reason}>
        <Badge tone={TONE[r.tone]} size={size} icon={r.tone === "waiting" ? Clock : r.tone === "failed" || r.tone === "anyway" ? AlertTriangle : undefined}>
          {r.tone === "busy" && <Spinner size={10} />}
          {r.label}
        </Badge>
      </Tooltip>
      {r.tone === "failed" && (
        <>
          <IconButton size="xs" icon={RotateCw} label="Retry analysis" tooltip="Retry analysis: queue it again" loading={busy === "retry"} disabled={busy !== null} onClick={(e) => void retry(e)} />
          <IconButton
            size="xs"
            icon={CheckCheck}
            label="Use anyway"
            tooltip="Use anyway: let it go into sets and mixes all the same, mixing on what was found (EQ for missing stems, a plan grid for missing beats)"
            loading={busy === "anyway"}
            disabled={busy !== null}
            onClick={(e) => void run("anyway", () => setUseAnyway(row.hash, true))(e)}
          />
        </>
      )}
      {r.tone === "anyway" && (
        <IconButton size="xs" icon={Undo2} label="Stop using it anyway" tooltip="Stop using it anyway: keep it out of sets and mixes until its analysis is fixed" loading={busy === "anyway"} disabled={busy !== null} onClick={(e) => void run("anyway", () => setUseAnyway(row.hash, false))(e)} />
      )}
    </span>
  );
}
