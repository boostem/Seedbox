/** Small presentational pieces shared by the set builder. */
import { AlertTriangle, AudioWaveform, Clock3, Minus } from "lucide-react";
import type { ReactNode } from "react";
import type { SetTrack } from "@/api/types";
import { Spinner, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtPitch, pitchTone } from "@/lib/format";

type State = SetTrack["analysis_state"];

const STATE_TEXT: Record<State, string> = {
  ready: "Analysed — waveform, cues and mix points ready",
  running: "Analysing audio…",
  queued: "Queued for analysis",
  missing: "Not analysed yet",
  error: "Analysis failed",
};

/** Tiny analysis-state indicator for set rows. */
export function AnalysisIcon({ state, className }: { state: State; className?: string }) {
  const icon =
    state === "ready" ? (
      <AudioWaveform size={13} className="text-fg-faint" />
    ) : state === "running" ? (
      <Spinner size={12} className="text-accent-hover" />
    ) : state === "queued" ? (
      <Clock3 size={13} className="animate-pulse-soft text-fg-subtle" />
    ) : state === "error" ? (
      <AlertTriangle size={13} className="text-danger" />
    ) : (
      <Minus size={13} className="text-fg-faint" />
    );
  return (
    <Tooltip content={STATE_TEXT[state]}>
      <span className={cn("grid size-4 place-items-center", className)} aria-label={STATE_TEXT[state]}>
        {icon}
      </span>
    </Tooltip>
  );
}

export function analysisStateText(state: State | undefined): string {
  return STATE_TEXT[state ?? "missing"];
}

const CHIP_BG: Record<string, string> = { "text-warn": "bg-warn/12", "text-danger": "bg-danger/12" };

/** Signed pitch % in a chip tinted by `pitchTone`. */
export function PitchChip({ pct, className }: { pct: number; className?: string }) {
  const tone = pitchTone(pct);
  return (
    <span
      className={cn(
        "inline-flex h-[18px] shrink-0 items-center rounded-xs px-1 text-xs font-medium num",
        tone,
        CHIP_BG[tone] ?? "bg-raised",
        className,
      )}
    >
      {fmtPitch(pct)}
    </span>
  );
}

/** Label/value tile for the transition cheat sheet and the set stats. */
export function Stat({ label, children, sub, className, onClick }: { label: ReactNode; children: ReactNode; sub?: ReactNode; className?: string; onClick?: () => void }) {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag
      type={onClick ? "button" : undefined}
      onClick={onClick}
      className={cn("flex min-w-0 flex-col items-start gap-0.5 text-left", onClick && "rounded-sm hover:bg-raised/60", className)}
    >
      <span className="label-caps">{label}</span>
      <span className="text-lg leading-6 font-semibold text-fg">{children}</span>
      {sub && <span className="w-full truncate text-2xs text-fg-subtle" title={typeof sub === "string" ? sub : undefined}>{sub}</span>}
    </Tag>
  );
}
