import { cn } from "@/lib/cn";

export type ProgressTone = "accent" | "ok" | "warn" | "danger" | "info" | "neutral";

const FILL: Record<ProgressTone, string> = {
  accent: "bg-accent",
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
  info: "bg-info",
  neutral: "bg-fg-muted",
};

export interface ProgressBarProps {
  /** 0-1; null/undefined = indeterminate (animated). */
  value?: number | null;
  tone?: ProgressTone;
  size?: "xs" | "sm" | "md";
  className?: string;
  label?: string;
}

export function ProgressBar({ value, tone = "accent", size = "sm", className, label }: ProgressBarProps) {
  const indeterminate = value == null;
  const pct = indeterminate ? 0 : Math.max(0, Math.min(1, value)) * 100;
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : Math.round(pct)}
      className={cn("relative w-full overflow-hidden rounded-full bg-line-strong/70", size === "xs" ? "h-0.5" : size === "sm" ? "h-1" : "h-1.5", className)}
    >
      {indeterminate ? (
        <div className={cn("absolute inset-y-0 w-2/5 rounded-full animate-indeterminate", FILL[tone])} />
      ) : (
        <div className={cn("h-full rounded-full transition-[width] duration-300 ease-out", FILL[tone])} style={{ width: `${pct}%` }} />
      )}
    </div>
  );
}
