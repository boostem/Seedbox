import { useId } from "react";
import { cn } from "@/lib/cn";

/** Bar heights (0.1-1) of a made-up track: an arrangement that swells, with a beat on top. The same seed draws the
 * same waveform, so a screen always looks the same. */
export function waveBars(seed: number, count: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < count; i++) {
    const x = i / Math.max(1, count - 1);
    const arc = Math.sin(Math.PI * x) ** 0.7;
    const swell = 0.55 + 0.45 * Math.sin(x * 9 + seed * 1.7);
    const beat = 0.6 + 0.4 * Math.abs(Math.sin(i * 0.9 + seed));
    const grain = 0.8 + 0.2 * Math.sin(i * 2.3 + seed * 3.1);
    out.push(Math.max(0.1, Math.min(1, arc * swell * beat * grain)));
  }
  return out;
}

/** A small number from a string, to give each screen its own waveform. */
export function seedOf(text: string): number {
  let n = 0;
  for (let i = 0; i < text.length; i++) n = (n * 31 + text.charCodeAt(i)) % 997;
  return n;
}

export interface WaveArtProps {
  seed?: number;
  /** `danger`: the same waveform in the error colour. */
  tone?: "default" | "danger";
  className?: string;
}

const BARS = 64;
const W = BARS * 5;
const H = 88;

/** The illustration of an empty state or the first run: the app's three-band waveform, fading out at both ends. */
export function WaveArt({ seed = 1, tone = "default", className }: WaveArtProps) {
  const id = useId();
  const bands = tone === "danger"
    ? [{ part: 1, fill: "var(--color-danger)", opacity: 0.35 }, { part: 0.5, fill: "var(--color-danger)", opacity: 0.7 }]
    : [{ part: 1, fill: "var(--color-wave-low)", opacity: 0.7 }, { part: 0.6, fill: "var(--color-wave-mid)", opacity: 0.75 }, { part: 0.25, fill: "var(--color-wave-high)", opacity: 0.8 }];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} aria-hidden className={cn("block h-22 w-80 max-w-full", className)}>
      <defs>
        <linearGradient id={`${id}f`}>
          <stop offset="0" stopColor="white" stopOpacity="0" />
          <stop offset="0.3" stopColor="white" />
          <stop offset="0.7" stopColor="white" />
          <stop offset="1" stopColor="white" stopOpacity="0" />
        </linearGradient>
        <mask id={`${id}m`}>
          <rect width={W} height={H} fill={`url(#${id}f)`} />
        </mask>
      </defs>
      <g mask={`url(#${id}m)`}>
        {bands.map((b) => (
          <g key={b.part} fill={b.fill} opacity={b.opacity}>
            {waveBars(seed, BARS).map((v, i) => {
              const h = Math.max(1.5, v * b.part * H);
              return <rect key={i} x={i * 5 + 0.75} y={(H - h) / 2} width={3.5} height={h} rx={1} />;
            })}
          </g>
        ))}
      </g>
    </svg>
  );
}
