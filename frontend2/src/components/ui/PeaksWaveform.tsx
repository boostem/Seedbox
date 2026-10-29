import { type PointerEvent, useEffect, useMemo, useRef } from "react";
import { player, usePlayer } from "@/audio/player";
import { cn } from "@/lib/cn";

export interface PeaksWaveformProps {
  /** ~200 values 0-1 (PreviewStatus.peaks). */
  peaks: number[] | null | undefined;
  /** Shows the played part and the playhead while the global player plays this id. They move from a
   * requestAnimationFrame loop that writes to the SVG, not from props: no React render per frame (PERFORMANCE.md). */
  trackId?: string | null;
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
export function PeaksWaveform({ peaks, trackId, height = 28, color = "var(--color-fg-faint)", playedColor = "var(--color-accent)", onSeek, loading, className, bars }: PeaksWaveformProps) {
  const ref = useRef<SVGSVGElement>(null);
  const values = useMemo(() => resample(peaks ?? [], Math.min(bars ?? peaks?.length ?? 0, 240)), [peaks, bars]);
  const n = values.length;
  const clipId = useMemo(() => `pk${Math.random().toString(36).slice(2, 9)}`, []);
  const clipRef = useRef<SVGRectElement>(null);
  const playedRef = useRef<SVGGElement>(null);
  const headRef = useRef<SVGRectElement>(null);
  const w = n * 3;

  useEffect(() => {
    if (!trackId || !n) return;
    let raf = 0;
    const draw = () => {
      const st = usePlayer.getState();
      const on = st.currentId === trackId && st.status !== "idle" && st.status !== "error" && st.duration > 0;
      const x = on ? Math.max(0, Math.min(1, player.time() / st.duration)) * w : 0;
      playedRef.current?.setAttribute("visibility", on ? "visible" : "hidden");
      headRef.current?.setAttribute("visibility", on ? "visible" : "hidden");
      clipRef.current?.setAttribute("width", String(x));
      headRef.current?.setAttribute("x", String(x - 0.5));
      return on && st.status === "playing";
    };
    const loop = () => {
      raf = draw() ? requestAnimationFrame(loop) : 0;
    };
    const kick = () => {
      if (!raf) raf = requestAnimationFrame(loop);
    };
    loop();
    const un = usePlayer.subscribe((st, prev) => {
      if (st.currentId === trackId || prev.currentId === trackId) kick();
    });
    return () => {
      un();
      cancelAnimationFrame(raf);
    };
  }, [trackId, n, w]);
  const bar = (fill: string) =>
    values.map((v, i) => {
      const h = Math.max(1.2, v * height);
      return <rect key={i} x={i * 3} y={(height - h) / 2} width={2} height={h} rx={0.6} fill={fill} />;
    });

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
      {trackId && (
        <>
          <clipPath id={clipId}>
            <rect ref={clipRef} x={0} y={0} width={0} height={height} />
          </clipPath>
          <g ref={playedRef} visibility="hidden" clipPath={`url(#${clipId})`}>{bar(playedColor)}</g>
          <rect ref={headRef} visibility="hidden" x={0} y={0} width={1} height={height} fill="var(--color-playhead)" opacity={0.9} />
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
