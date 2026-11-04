import { cn } from "@/lib/cn";
import { waveBars } from "./WaveArt";

/** A block standing in for text or a control until it loads: size it like what it replaces. `still`: no pulse, for
 * what waits rather than what is being worked on. */
export function Skeleton({ still, className }: { still?: boolean; className?: string }) {
  return <span aria-hidden className={cn("block h-3 rounded-xs bg-raised", !still && "animate-pulse-soft", className)} />;
}

export interface WaveSkeletonProps {
  seed?: number;
  bars?: number;
  /** `active` is being analysed now, `waiting` hasn't started, `failed` stopped. */
  tone?: "active" | "waiting" | "failed";
  className?: string;
}

const FILL = { active: "fill-accent/40 animate-pulse-soft", waiting: "fill-fg-faint/50", failed: "fill-danger/35" };

/** A waveform's outline where a waveform will be: stretches to its box. */
export function WaveSkeleton({ seed = 1, bars = 120, tone = "waiting", className }: WaveSkeletonProps) {
  return (
    <svg viewBox={`0 0 ${bars * 4} 100`} preserveAspectRatio="none" aria-hidden className={cn("block h-full w-full", FILL[tone], className)}>
      {waveBars(seed, bars).map((v, i) => {
        const h = Math.max(3, v * 100);
        return <rect key={i} x={i * 4 + 0.5} y={(100 - h) / 2} width={3} height={h} />;
      })}
    </svg>
  );
}
