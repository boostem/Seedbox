/**
 * Low-power mode's cap on waveform drawing (#85). The animation loops still run on requestAnimationFrame and ask
 * `mayDraw()` each frame: skipping costs nothing, and a browser with nothing new to paint doesn't paint.
 *
 * The answer is the same for every view within a frame, so they draw in the same frames: with one gate per view, views
 * that started a frame apart would take turns and the screen would still change 60 times a second.
 */
let period = 0;
let slot = -1;
let open = -1; // the frame that opened this slot: the only one that draws in it

/** The time of the current frame: the same for every callback in it, unlike performance.now(). */
function frameTime(): number {
  const t = document.timeline?.currentTime;
  return typeof t === "number" ? t : performance.now();
}

/** At most `fps` draws a second from now on; null: as often as the screen refreshes. */
export function setFrameCap(fps: number | null): void {
  period = fps ? 1000 / fps : 0;
  slot = open = -1;
}

/** Should a loop draw in this animation frame? */
export function mayDraw(now: number = frameTime()): boolean {
  if (!period) return true;
  const s = Math.floor(now / period);
  if (s !== slot) {
    slot = s;
    open = now;
  }
  return now === open;
}
