/**
 * Design language v2 (DESIGN2.md) as a test: every size, colour, layer and duration comes from the tokens in
 * src/index.css. Fails on arbitrary pixel values, hex colours outside index.css, numeric z-index and duration classes,
 * and text under 10px (Tailwind sizes, canvas fonts and SVG text).
 */
import { describe, expect, it } from "vitest";
import { MIN_TEXT_PX } from "@/lib/theme";

// every source file as text (Vite's glob, so the test needs no Node types); tests are excluded
// index.css as text: vitest stubs CSS imports (even ?raw) to "", so read the file; the module name is built at run
// time so tsc doesn't look for Node's types
const fs = (await import(/* @vite-ignore */ ["node", "fs"].join(":"))) as { readFileSync(path: URL, encoding: "utf8"): string };
const css = fs.readFileSync(new URL("./index.css", import.meta.url), "utf8");
const FILES = Object.entries(import.meta.glob(["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}"], { query: "?raw", import: "default", eager: true }) as Record<string, string>);

/** Code without comments (ticket numbers like "#123" live there), keeping line numbers. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " ")).replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

/** Every match of `re` in the source files, as "path:line  match". */
function offences(re: RegExp, keep: (m: RegExpExecArray) => boolean = () => true): string[] {
  const out: string[] = [];
  for (const [file, raw] of FILES) {
    const text = code(raw);
    for (const m of text.matchAll(new RegExp(re.source, re.flags.includes("g") ? re.flags : re.flags + "g"))) {
      if (!keep(m)) continue;
      const line = text.slice(0, m.index).split("\n").length;
      out.push(`${file.slice(2)}:${line}  ${m[0]}`);
    }
  }
  return out;
}

describe("design tokens", () => {
  it("reads the source files", () => {
    expect(FILES.length).toBeGreaterThan(40);
  });

  it("has hex colours only in index.css", () => {
    expect(offences(/#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})\b/i)).toEqual([]);
  });

  it("has no arbitrary sizes, colours, shadows, layers or timings in class names", () => {
    // allowed: viewport/percent sizes (max-h-[85vh]), keywords (rounded-[inherit]), properties (transition-[width])
    const bad = /\d(?:px|rem|em|ms|s)\b|#|rgb|hsl|cubic|var\(/;
    const tokenOnly = /^(?:z|duration|delay|ease|leading|tracking|text|shadow|bg|border|fill|stroke|ring|outline)$/;
    const found = offences(/(?<![\w-])!?([a-z][\w-]*?)-\[([^\]\s"'`]+)\]/, (m) => tokenOnly.test(m[1].split(":").pop()!) || bad.test(m[2]));
    expect(found).toEqual([]);
  });

  it("uses the z-index and motion scales instead of numbers", () => {
    expect(offences(/(?<![\w-])(?:[\w-]+:)*(?:z-\d+|duration-\d+|delay-\d+)(?![\w-])/)).toEqual([]);
  });

  it("never sets text under 10px", () => {
    const sizes = [...css.matchAll(/--text-[\w-]+?:\s*([\d.]+)px;/g)].filter((m) => !m[0].includes("line-height")).map((m) => Number(m[1]));
    expect(sizes.length).toBeGreaterThan(4);
    expect(Math.min(...sizes)).toBeGreaterThanOrEqual(MIN_TEXT_PX);
    const small = (n: string) => Number(n) < MIN_TEXT_PX;
    // canvas: ctx.font = `600 9px ${…}` and the font(p, 9) helper in features/waveform/draw.ts
    expect(offences(/`\d+ ([\d.]+)px \$\{/, (m) => small(m[1]))).toEqual([]);
    expect(offences(/\bfont\(p, ([\d.]+)/, (m) => small(m[1]))).toEqual([]);
    // SVG <text fontSize={9}> and style={{ fontSize: 9 }}
    expect(offences(/fontSize(?:=\{|: )([\d.]+)\b/, (m) => small(m[1]))).toEqual([]);
  });
});
