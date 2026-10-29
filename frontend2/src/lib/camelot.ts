/**
 * Camelot wheel: parsing, moves and THE colour map (single source of truth for key colours in tables, badges,
 * the wheel, charts). 12 hues around the wheel following the Mixed-In-Key layout (1 = teal … 4 = yellow …
 * 7 = red … 10 = violet … 12 = sky blue). A (minor, inner ring) = deeper shade, B (major, outer ring) = lighter.
 */
import type { Move } from "@/api/types";

export type CamelotLetter = "A" | "B";
export interface CamelotCode {
  n: number; // 1-12
  letter: CamelotLetter;
  code: string; // canonical "1A"
}

const RE = /^\s*0?(\d{1,2})\s*([AB])\s*$/i;

export function parseCamelot(code: string | null | undefined): CamelotCode | null {
  const m = RE.exec(code ?? "");
  if (!m) return null;
  const n = Number(m[1]);
  if (n < 1 || n > 12) return null;
  const letter = m[2].toUpperCase() as CamelotLetter;
  return { n, letter, code: `${n}${letter}` };
}

/** Hue (HSL degrees) per wheel position. */
const CAMELOT_HUES: Record<number, number> = {
  1: 168, 2: 140, 3: 102, 4: 64, 5: 42, 6: 22, 7: 356, 8: 328, 9: 292, 10: 262, 11: 228, 12: 198,
};

export interface KeyColor {
  base: string; // the key's colour: text on dark, chart strokes, wheel segment
  bg: string; // tinted background for chips/badges
  border: string; // subtle outline
  solidFg: string; // text on a `base` filled background
}

// the neutral and the text-on-key colours are design tokens (src/index.css); the 24 key colours are computed below
const UNKNOWN: KeyColor = {
  base: "var(--color-key-unknown)",
  bg: "color-mix(in srgb, var(--color-key-unknown) 12%, transparent)",
  border: "color-mix(in srgb, var(--color-key-unknown) 28%, transparent)",
  solidFg: "var(--color-on-key)",
};

function hsl(h: number, s: number, l: number, a = 1): string {
  return a === 1 ? `hsl(${h}, ${s}%, ${l}%)` : `hsla(${h}, ${s}%, ${l}%, ${a})`;
}

function build(n: number, letter: CamelotLetter): KeyColor {
  const h = CAMELOT_HUES[n];
  // yellows/greens look heavier at the same lightness: lift less, desaturate slightly
  const warm = h > 40 && h < 110;
  const [s, l] = letter === "A" ? [warm ? 78 : 72, warm ? 52 : 60] : [warm ? 90 : 88, warm ? 68 : 75];
  return { base: hsl(h, s, l), bg: hsl(h, s, l, 0.14), border: hsl(h, s, l, 0.35), solidFg: "var(--color-on-key)" };
}

export const CAMELOT_CODES: string[] = Array.from({ length: 12 }, (_, i) => i + 1).flatMap((n) => [`${n}A`, `${n}B`]);

export const CAMELOT_COLORS: Record<string, KeyColor> = Object.fromEntries(
  CAMELOT_CODES.map((c) => [c, build(Number(c.slice(0, -1)), c.slice(-1) as CamelotLetter)]),
);

/** Colour set for a Camelot code ("1A", "01a", …); neutral grey for unknown/unparseable keys. */
export function keyColor(code: string | null | undefined): KeyColor {
  const p = parseCamelot(code);
  return p ? CAMELOT_COLORS[p.code] : UNKNOWN;
}

/** Writes --camelot-<code> (base) and --camelot-<code>-bg CSS vars on :root so CSS can use them too. */
export function installCamelotCssVars(root: HTMLElement = document.documentElement): void {
  for (const [code, c] of Object.entries(CAMELOT_COLORS)) {
    root.style.setProperty(`--camelot-${code}`, c.base);
    root.style.setProperty(`--camelot-${code}-bg`, c.bg);
  }
}

/** Camelot relationship a → b, identical to musicreco/mixing.py `_camelot_move`. */
export function camelotMove(a: string | null | undefined, b: string | null | undefined): Move {
  const p = parseCamelot(a);
  const q = parseCamelot(b);
  if (!p || !q) return "?";
  const d = (((q.n - p.n) % 12) + 12) % 12;
  if (p.letter === q.letter) {
    return ({ 0: "same", 1: "+1", 11: "-1", 2: "+2 boost", 7: "+7 boost" } as Record<number, Move>)[d] ?? "clash";
  }
  if (d === 0) return "relative";
  return d === 1 || d === 11 ? "diagonal" : "clash";
}

/** The pre-tick key rule: same, ±1 same letter, or relative (mixing.camelot_adjacent). */
export function camelotAdjacent(a: string, b: string): boolean {
  const m = camelotMove(a, b);
  return m === "same" || m === "+1" || m === "-1" || m === "relative";
}
