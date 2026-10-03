/** Canvas colours for waveforms, resolved once from the design tokens (CSS vars don't work in fillStyle). */
import type { Edge, SectionKind } from "@/api/types";
import { cssVar } from "@/lib/theme";

export interface WavePalette {
  bg: string;
  panel: string;
  surface: string;
  raised: string;
  line: string;
  lineStrong: string;
  fg: string;
  fgMuted: string;
  fgSubtle: string;
  fgFaint: string;
  accent: string;
  ok: string;
  warn: string;
  danger: string;
  info: string;
  low: string;
  mid: string;
  high: string;
  playhead: string;
  font: string;
}

let cached: WavePalette | null = null;

export function wavePalette(): WavePalette {
  if (cached) return cached;
  const v = (n: string, fallback: string) => cssVar(`--color-${n}`) || fallback;
  cached = {
    bg: v("bg", "#08090b"),
    panel: v("panel", "#0e0f12"),
    surface: v("surface", "#121418"),
    raised: v("raised", "#191c21"),
    line: v("line", "#22252b"),
    lineStrong: v("line-strong", "#30343c"),
    fg: v("fg", "#eceef2"),
    fgMuted: v("fg-muted", "#a4aab5"),
    fgSubtle: v("fg-subtle", "#6f7682"),
    fgFaint: v("fg-faint", "#474d57"),
    accent: v("accent", "#6f7dff"),
    ok: v("ok", "#3ddc97"),
    warn: v("warn", "#f5b841"),
    danger: v("danger", "#ff4d6a"),
    info: v("info", "#4cc9f0"),
    low: v("wave-low", "#2f6bff"),
    mid: v("wave-mid", "#ff9f1c"),
    high: v("wave-high", "#f4f6fa"),
    playhead: v("playhead", "#ffffff"),
    font: cssVar("--font-sans") || "-apple-system, BlinkMacSystemFont, sans-serif",
  };
  return cached;
}

/** Section colours (bands under the waveform). Token-based; CSS strings usable in canvas and style={}. */
export function sectionColor(kind: SectionKind, p: WavePalette = wavePalette()): string {
  switch (kind) {
    case "intro":
    case "outro":
      return p.info;
    case "build":
      return p.warn;
    case "drop":
      return p.danger;
    case "breakdown":
      return p.accent;
    default:
      return p.fgSubtle;
  }
}

export const SECTION_LABEL: Record<SectionKind, string> = {
  intro: "Intro",
  build: "Build",
  drop: "Drop",
  breakdown: "Break",
  body: "Body",
  outro: "Outro",
};

/** Intro/outro edge colour: drums-only = clean to blend (ok), vocals = careful (warn), else music (info). */
export function edgeColor(e: Edge, p: WavePalette = wavePalette()): string {
  return e.drums_only ? p.ok : e.has_vocals ? p.warn : p.info;
}

export function edgeWords(e: Edge): string {
  return e.drums_only ? "drums only" : e.has_vocals ? "vocals" : "music";
}

/** Cue colour by label ("mix in (16-bar intro)", "drop 2", "breakdown", "first downbeat", "mix out …"). */
export function cueColor(label: string, p: WavePalette = wavePalette()): string {
  const l = label.toLowerCase();
  if (l.startsWith("mix")) return p.ok;
  if (l.startsWith("drop")) return p.danger;
  if (l.startsWith("breakdown")) return p.accent;
  if (l.startsWith("build")) return p.warn;
  if (l.startsWith("outro") || l.startsWith("intro")) return p.info;
  return p.fgMuted;
}

/** "#rrggbb" / "rgb()" / "hsl()" + alpha → canvas colour string. */
export function alpha(color: string, a: number): string {
  const c = color.trim();
  if (c.startsWith("#")) {
    const hex = c.length === 4 ? c.slice(1).replace(/./g, (x) => x + x) : c.slice(1, 7);
    const n = parseInt(hex, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }
  if (c.startsWith("rgb(")) return c.replace("rgb(", "rgba(").replace(")", `, ${a})`);
  if (c.startsWith("hsl(")) return c.replace("hsl(", "hsla(").replace(")", `, ${a})`);
  return c;
}
