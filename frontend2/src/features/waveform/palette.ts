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
  /** music colours (DESIGN2.md "Colour roles"): what is in the track, never good or bad */
  section: Record<SectionKind, string>;
  phrase: string;
  cueMix: string;
  stem: { drums: string; bass: string; vocals: string; other: string };
  font: string;
}

let cached: WavePalette | null = null;

/** The design tokens as canvas colours. No fallbacks: the tokens live in src/index.css only. */
export function wavePalette(): WavePalette {
  if (cached) return cached;
  const v = (n: string) => cssVar(`--color-${n}`);
  cached = {
    bg: v("bg"),
    panel: v("panel"),
    surface: v("surface"),
    raised: v("raised"),
    line: v("line"),
    lineStrong: v("line-strong"),
    fg: v("fg"),
    fgMuted: v("fg-muted"),
    fgSubtle: v("fg-subtle"),
    fgFaint: v("fg-faint"),
    accent: v("accent"),
    ok: v("ok"),
    warn: v("warn"),
    danger: v("danger"),
    info: v("info"),
    low: v("wave-low"),
    mid: v("wave-mid"),
    high: v("wave-high"),
    playhead: v("playhead"),
    section: {
      intro: v("section-intro"),
      build: v("section-build"),
      drop: v("section-drop"),
      breakdown: v("section-breakdown"),
      body: v("section-body"),
      outro: v("section-outro"),
    },
    phrase: v("phrase"),
    cueMix: v("cue-mix"),
    stem: { drums: v("stem-drums"), bass: v("stem-bass"), vocals: v("stem-vocals"), other: v("stem-other") },
    font: cssVar("--font-sans") || "-apple-system, BlinkMacSystemFont, sans-serif",
  };
  return cached;
}

/** Section colours (bands under the waveform). Music colours, not status: a drop is not an error. */
export function sectionColor(kind: SectionKind, p: WavePalette = wavePalette()): string {
  return p.section[kind] ?? p.section.body;
}

export const SECTION_LABEL: Record<SectionKind, string> = {
  intro: "Intro",
  build: "Build",
  drop: "Drop",
  breakdown: "Break",
  body: "Body",
  outro: "Outro",
};

/** Intro/outro edge colour, by what plays in it: drums only, vocals, or other music (the stem colours). */
export function edgeColor(e: Edge, p: WavePalette = wavePalette()): string {
  return e.drums_only ? p.stem.drums : e.has_vocals ? p.stem.vocals : p.stem.other;
}

export function edgeWords(e: Edge): string {
  return e.drums_only ? "drums only" : e.has_vocals ? "vocals" : "music";
}

/** Cue colour by label ("mix in (16-bar intro)", "drop 2", "breakdown", "first downbeat", "mix out …"): mix cues
 * have their own colour, the others take the colour of the section they start. */
export function cueColor(label: string, p: WavePalette = wavePalette()): string {
  const l = label.toLowerCase();
  if (l.startsWith("mix")) return p.cueMix;
  for (const kind of ["drop", "breakdown", "build", "intro", "outro"] as const) if (l.startsWith(kind)) return p.section[kind];
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
