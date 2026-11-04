/**
 * The analysis queue indicator (#73), in the top bar: what is analysing, how far the stems are and when the queue
 * will be done. Click for the details. Hidden while there is nothing to do.
 */
import { Layers, Loader2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ProgressBar } from "@/components/ui";
import { cn } from "@/lib/cn";
import { queuedByKind, queueEta, type RunningJob, useJobs } from "@/store/jobs";
import { useLibrary } from "@/store/library";

/** "~40 s", "~3 min", "~1 h 20 min". */
export function fmtEta(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "";
  if (seconds < 60) return `~${Math.max(5, Math.ceil(seconds / 5) * 5)} s`;
  const min = Math.ceil(seconds / 60);
  if (min < 60) return `~${min} min`;
  return `~${Math.floor(min / 60)} h${min % 60 ? ` ${min % 60} min` : ""}`;
}

const KIND_LABEL: Record<string, string> = { waveform: "Waveform", beats: "Beats", key: "Key", stems: "Stems", energy: "Energy", sections: "Sections", vocals: "Vocals", embedding: "Vibe" };
const label = (kind: string) => KIND_LABEL[kind] ?? kind;

/** Re-render once a second while `on` (the ETA counts down between job events). */
function useTick(on: boolean): number {
  const [t, setT] = useState(0);
  useEffect(() => {
    if (!on) return;
    const id = setInterval(() => setT((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [on]);
  return t;
}

export function AnalysisQueue() {
  const running = useJobs((s) => s.running);
  const queued = useJobs((s) => s.queued);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const runningList = Object.values(running);
  const queuedCount = Object.keys(queued).length;
  const busy = runningList.length + queuedCount > 0;
  useTick(busy);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  if (!busy) return null;
  const s = useJobs.getState();
  const eta = queueEta(s, performance.now());
  const stems = runningList.find((j) => j.kind === "stems");
  return (
    <div ref={box} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex h-7 items-center gap-2 rounded-sm px-2 text-xs text-fg-muted transition-colors hover:bg-hover hover:text-fg"
        aria-label="Analysis queue"
      >
        <Loader2 size={13} className={cn("text-accent", runningList.length > 0 && "animate-spin")} />
        <span className="num">
          {runningList.length > 0 ? `Analysing ${runningList.length}` : "Waiting"}
          {queuedCount > 0 && <span className="text-fg-subtle"> · {queuedCount} queued</span>}
        </span>
        {stems && (
          <span className="flex items-center gap-1" title="Stem separation">
            <Layers size={12} className="text-fg-subtle" />
            <span className="num">{Math.round(stems.progress * 100)}%</span>
          </span>
        )}
        {eta !== null && <span className="num text-fg-subtle">{fmtEta(eta)}</span>}
      </button>
      {open && <QueuePanel running={runningList} queued={queued} eta={eta} />}
    </div>
  );
}

function QueuePanel({ running, queued, eta }: { running: RunningJob[]; queued: Record<string, string>; eta: number | null }) {
  const rows = useLibrary((s) => s.rows);
  const byHash = useMemo(() => new Map((rows ?? []).map((r) => [r.hash, r])), [rows]);
  // stems first (the long ones), then the ones furthest along; a light pool of 10 workers would be a long list
  const shownJobs = [...running].sort((a, b) => Number(b.kind === "stems") - Number(a.kind === "stems") || b.progress - a.progress).slice(0, 8);
  const counts = Object.entries(queuedByKind(queued)).sort((a, b) => b[1] - a[1]);
  const name = (j: RunningJob) => {
    const r = j.hash ? byHash.get(j.hash) : undefined;
    return r ? `${r.artist ? `${r.artist} – ` : ""}${r.title}` : j.hash ? j.hash.slice(0, 10) : "";
  };
  return (
    <div className="absolute top-9 right-0 z-dropdown w-96 rounded-md border border-line-strong bg-raised p-3 shadow-lg animate-route-in" role="dialog" aria-label="Analysis queue">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="text-sm font-semibold">Analysis</span>
        <span className="text-xs text-fg-subtle">{eta !== null ? `done in ${fmtEta(eta)}` : ""}</span>
      </div>
      <div className="flex flex-col gap-2">
        {shownJobs.map((j) => (
          <div key={j.id} className="flex flex-col gap-1">
            <div className="flex items-baseline gap-2 text-xs">
              <span className={cn("w-16 shrink-0 font-medium", j.kind === "stems" ? "text-accent" : "text-fg-muted")}>{label(j.kind)}</span>
              <span className="min-w-0 flex-1 truncate text-fg">{name(j)}</span>
              <span className="num text-fg-subtle">{Math.round(j.progress * 100)}%</span>
            </div>
            <ProgressBar value={j.progress} label={`${label(j.kind)} progress`} />
          </div>
        ))}
        {running.length > shownJobs.length && <div className="text-xs text-fg-subtle">and {running.length - shownJobs.length} more</div>}
        {running.length === 0 && <div className="text-xs text-fg-subtle">Waiting for a worker (or for enough free memory)…</div>}
      </div>
      {counts.length > 0 && (
        <div className="mt-3 border-t border-line pt-2 text-xs text-fg-muted">
          Queued: {counts.map(([k, n]) => `${label(k)} ${n}`).join(" · ")}
        </div>
      )}
    </div>
  );
}
