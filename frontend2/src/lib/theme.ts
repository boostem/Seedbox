/** Resolved design-token colours for <canvas>/SVG drawing (CSS vars don't work inside canvas fillStyle). */

/** Reads a CSS custom property's resolved value: cssVar("--color-wave-low") → "#2f6bff". */
export function cssVar(name: string, el: Element = document.documentElement): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

/** The smallest text anywhere, canvas and SVG included (DESIGN2.md "Type"). */
export const MIN_TEXT_PX = 10;

/** A font size in px, never under MIN_TEXT_PX: for canvas fonts and SVG text that scale with their box. */
export function textPx(px: number): number {
  return Math.max(MIN_TEXT_PX, px);
}
