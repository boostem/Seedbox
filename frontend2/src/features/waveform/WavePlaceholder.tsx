import { AlertTriangle, RotateCw } from "lucide-react";
import { Button, Spinner, WaveSkeleton } from "@/components/ui";
import { cn } from "@/lib/cn";

export type WavePlaceholderState = "loading" | "queued" | "analysing" | "failed";

export interface WavePlaceholderProps {
  state: WavePlaceholderState;
  /** What failed and how to fix it (`failed`), or a better word than the default. */
  message?: string;
  onRetry?: () => void;
  seed?: number;
  className?: string;
}

const LABEL: Record<WavePlaceholderState, string> = {
  loading: "Loading the waveform",
  queued: "Waiting for its turn to be analysed",
  analysing: "Analysing: the waveform, cues and mix points appear in a moment",
  failed: "Couldn't analyse this track",
};

/** What a waveform view shows until it can draw: the outline of a waveform, with what is happening to it on top. */
export function WavePlaceholder({ state, message, onRetry, seed, className }: WavePlaceholderProps) {
  const failed = state === "failed";
  return (
    <div className={cn("relative h-40 overflow-hidden rounded-sm bg-bg/60", className)} role={failed ? "alert" : "status"}>
      <WaveSkeleton seed={seed} tone={failed ? "failed" : state === "analysing" ? "active" : "waiting"} />
      <div className="absolute inset-0 grid place-items-center p-3">
        <div className={cn("flex max-w-full items-center gap-2 rounded-sm border border-line bg-panel/90 px-2.5 py-1.5 text-xs", failed ? "text-danger" : "text-fg-muted")}>
          {failed ? <AlertTriangle size={14} className="shrink-0" /> : <Spinner size={13} className={cn("shrink-0", state === "analysing" ? "text-accent-hover" : "text-fg-faint")} />}
          <span className="select-text">{message ?? LABEL[state]}</span>
          {onRetry && (
            <Button size="sm" icon={RotateCw} onClick={onRetry}>
              Retry
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
