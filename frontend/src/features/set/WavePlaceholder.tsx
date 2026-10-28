/** Stand-in for a waveform that can't be drawn yet (analysis loading, queued, running or failed). */
import { AlertTriangle } from "lucide-react";
import { Spinner } from "@/components/ui";
import { cn } from "@/lib/cn";
import type { AnalysisView } from "./useAnalysis";

export function WavePlaceholder({ view, height = 190 }: { view: AnalysisView; height?: number }) {
  const msg =
    view.status === "pending"
      ? view.state === "running"
        ? "Analysing audio — waveform, cues and mix points appear in a moment…"
        : "Queued for analysis — waveform, cues and mix points appear when it's done."
      : view.status === "error"
        ? view.message
        : "Loading analysis…";
  return (
    <div style={{ height }} className={cn("grid place-items-center rounded-sm border border-dashed", view.status === "error" ? "border-danger/40 bg-danger/[0.04]" : "border-line-strong bg-bg/60")}>
      <div className={cn("flex items-center gap-2 text-sm", view.status === "error" ? "text-danger" : "text-fg-subtle")}>
        {view.status === "error" ? <AlertTriangle size={15} /> : <Spinner size={14} className={view.status === "pending" && view.state === "queued" ? "text-fg-faint" : "text-accent-hover"} />}
        <span className="select-text">{msg}</span>
      </div>
    </div>
  );
}

/** What the mix view shows while it can't draw both decks: the analysis still missing (a ready transition), else the
 * failed or running one, else "queued". */
export function mixPlaceholderView(ready: boolean, va: AnalysisView, vb: AnalysisView): AnalysisView {
  if (ready) return va.status !== "ready" ? va : vb;
  if (va.status === "error" || vb.status === "error") return { status: "error", message: "No mix points: an analysis failed for one of these tracks." };
  if (va.status === "pending" && va.state === "running") return va;
  if (vb.status === "pending" && vb.state === "running") return vb;
  return { status: "pending", state: "queued" };
}
