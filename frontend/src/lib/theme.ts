/** Resolved design-token colours for <canvas>/SVG drawing (CSS vars don't work inside canvas fillStyle). */

/** Reads a CSS custom property's resolved value: cssVar("--color-wave-low") → "#2f6bff". */
export function cssVar(name: string, el: Element = document.documentElement): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}
