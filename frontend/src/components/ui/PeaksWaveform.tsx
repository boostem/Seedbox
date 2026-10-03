import { type PointerEvent, useMemo, useRef } from "react";
import { cn } from "@/lib/cn";

export interface PeaksWaveformProps {
  /** ~200 values 0-1 (PreviewStatus.peaks). */
  peaks: number[] | null | undefined;
  /** Playhead 0-1 (null = not playing). */
  progress?: number | null;
  height?: number;
  /** Unplayed bar colour (CSS). */
  color?: string;
  /** Played bar colour (CSS). */
  playedColor?: string;
  /** Click/drag to seek (fraction 0-1). */
  onSeek?: (fraction: number) => void;
  /** Placeholder shimmer while the preview loads. */
  loading?: boolean;
  className?: string;
  /** Resample to this many bars (default: peaks.length, max 240). */
  bars?: number;
}

/**
 * Compact mirrored-bar waveform for 20 s preview clips (crate rows, mini player). For full-track 3-band
 * waveforms use the set builder's waveform feature.
 */
export function PeaksWaveform({ peaks, progress, height = 28, color = "var(--color-fg-faint)", playedColor = "var(--color-accent)", onSeek, loading, className, bars }: PeaksWaveformProps) {
  const ref = useRef<SVGSVGElement>(null);
  const values = useMemo(() => resample(peaks ?? [], Math.min(bars ?? peaks?.length ?? 0, 240)), [peaks, bars]);
  const n = values.length;
  const clipId = useMemo(() => `pk${Math.random().toString(36).slice(2, 9)}`, []);

  const seek = (e: PointerEvent<SVGSVGElement>) => {
    if (!onSeek || !ref.current) return;
    const r = ref.current.getBoundingClientRect();
    onSeek(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)));
  };

  if (!n) {
    return (
      <div style={{ height }} className={cn("flex w-full items-center", className)}>
        <div className={cn("h-px w-full", loading ? "animate-pulse-soft bg-fg-faint" : "bg-line-strong")} />
      </div>
    );
  }
  const w = n * 3;
  const p = progress == null ? null : Math.max(0, Math.min(1, progress));
  const bar = (fill: string) =>
    values.map((v, i) => {
      const h = Math.max(1.2, v * height);
      return <rect key={i} x={i * 3} y={(height - h) / 2} width={2} height={h} rx={0.6} fill={fill} />;
    });
  return (
    <svg
      ref={ref}
      viewBox={`0 0 ${w} ${height}`}
      preserveAspectRatio="none"
      style={{ height }}
      className={cn("block w-full", onSeek && "cursor-pointer", className)}
      onPointerDown={(e) => {
        if (!onSeek) return;
        e.currentTarget.setPointerCapture(e.pointerId);
        seek(e);
      }}
      onPointerMove={(e) => e.buttons === 1 && seek(e)}
    >
      <g>{bar(color)}</g>
      {p != null && (
        <>
          <clipPath id={clipId}>
            <rect x={0} y={0} width={p * w} height={height} />
          </clipPath>
          <g clipPath={`url(#${clipId})`}>{bar(playedColor)}</g>
          <rect x={p * w - 0.5} y={0} width={1} height={height} fill="var(--color-playhead)" opacity={0.9} />
        </>
      )}
    </svg>
  );
}

function resample(src: number[], n: number): number[] {
  if (!n || !src.length) return [];
  if (src.length === n) return src;
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const a = Math.floor((i * src.length) / n);
    const b = Math.max(a + 1, Math.floor(((i + 1) * src.length) / n));
    let m = 0;
    for (let j = a; j < b; j++) m = Math.max(m, src[j] ?? 0);
    out.push(m);
  }
  return out;
}
