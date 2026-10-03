/** Resolved design-token colours for <canvas>/SVG drawing (CSS vars don't work inside canvas fillStyle). */

/** Reads a CSS custom property's resolved value: cssVar("--color-wave-low") → "#2f6bff". */
export function cssVar(name: string, el: Element = document.documentElement): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

export interface ThemeColors {
  bg: string;
  surface: string;
  line: string;
  fg: string;
  fgMuted: string;
  fgSubtle: string;
  accent: string;
  waveLow: string;
  waveMid: string;
  waveHigh: string;
  wavePlayed: string;
  playhead: string;
  ok: string;
  warn: string;
  danger: string;
}

let cached: ThemeColors | null = null;

/** All canvas-relevant token values (cached after first call). */
export function themeColors(): ThemeColors {
  if (cached) return cached;
  const v = (n: string) => cssVar(`--color-${n}`);
  cached = {
    bg: v("bg"),
    surface: v("surface"),
    line: v("line"),
    fg: v("fg"),
    fgMuted: v("fg-muted"),
    fgSubtle: v("fg-subtle"),
    accent: v("accent"),
    waveLow: v("wave-low"),
    waveMid: v("wave-mid"),
    waveHigh: v("wave-high"),
    wavePlayed: v("wave-played"),
    playhead: v("playhead"),
    ok: v("ok"),
    warn: v("warn"),
    danger: v("danger"),
  };
  return cached;
}
