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
  /** RGB mode: lows, mids and highs mixed per column by their balance */
  rgbLow: string;
  rgbMid: string;
  rgbHigh: string;
  playhead: string;
  /** music colours (DESIGN2.md "Colour roles"): what is in the track, never good or bad */
  section: Record<SectionKind, string>;
  phrase: string;
  cueMix: string;
  /** hot cue slots A-H */
  cueSlots: string[];
  /** transition styles: the overlap region's colour by the plan's technique */
  style: Record<StyleKind, string>;
  stem: { drums: string; bass: string; vocals: string; other: string };
  font: string;
}

export type StyleKind = "blend" | "swap" | "filter" | "echo" | "cut";

/** How many hot cue slots have a colour (A-H). */
export const CUE_SLOTS = 8;

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
    rgbLow: v("wave-rgb-low"),
    rgbMid: v("wave-rgb-mid"),
    rgbHigh: v("wave-rgb-high"),
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
    cueSlots: Array.from({ length: CUE_SLOTS }, (_, i) => v(`cue-${i + 1}`)),
    style: { blend: v("style-blend"), swap: v("style-swap"), filter: v("style-filter"), echo: v("style-echo"), cut: v("style-cut") },
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

/** Cue colour: a hot cue's slot colour; else by label ("mix in (16-bar intro)", "drop 2", "breakdown", "first
 * downbeat", "mix out …"): mix cues have their own colour, the others take the colour of the section they start. */
export function cueColor(label: string, p: WavePalette = wavePalette(), slot?: number | null): string {
  if (slot != null && slot >= 0) return p.cueSlots[slot % CUE_SLOTS];
  const l = label.toLowerCase();
  if (l.startsWith("mix")) return p.cueMix;
  for (const kind of ["drop", "breakdown", "build", "intro", "outro"] as const) if (l.startsWith(kind)) return p.section[kind];
  return p.fgMuted;
}

/** A hot cue slot's letter: 0 → "A". */
export const slotLetter = (slot: number): string => String.fromCharCode(65 + (slot % CUE_SLOTS));

/** The style a plan's technique ("long blend", "bass_swap", "filter sweep", "echo out", "quick cut", …) belongs to. */
export function styleOf(technique: string | null | undefined): StyleKind {
  const t = (technique ?? "").toLowerCase();
  if (t.includes("echo")) return "echo";
  if (t.includes("cut")) return "cut";
  if (t.includes("filter")) return "filter";
  if (t.includes("swap")) return "swap";
  return "blend";
}

/** The overlap region's colour for a plan's technique. A music colour: it says how the tracks mix, not how well. */
export function styleColor(technique: string | null | undefined, p: WavePalette = wavePalette()): string {
  return p.style[styleOf(technique)];
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

const rgbCache = new Map<string, Float32Array>();

/** "#rrggbb" / "rgb(r, g, b)" → [r, g, b] in 0-1 (for shader uniforms); black when it can't be read. */
export function rgbOf(color: string): Float32Array {
  let out = rgbCache.get(color);
  if (out) return out;
  const c = color.trim();
  out = new Float32Array(3);
  if (c.startsWith("#")) {
    const hex = c.length === 4 ? c.slice(1).replace(/./g, (x) => x + x) : c.slice(1, 7);
    const n = parseInt(hex, 16);
    if (Number.isFinite(n)) out.set([((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255]);
  } else {
    const m = c.match(/rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/);
    if (m) out.set([+m[1] / 255, +m[2] / 255, +m[3] / 255]);
  }
  rgbCache.set(color, out);
  return out;
}
