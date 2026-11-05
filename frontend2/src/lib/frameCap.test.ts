import { afterEach, describe, expect, it } from "vitest";
import { mayDraw, setFrameCap } from "./frameCap";

/** How many of a second's frames at `hz` may draw. */
function drawn(hz: number): number {
  let draws = 0;
  for (let i = 0; i < hz; i++) if (mayDraw(1010 + (i * 1000) / hz)) draws++;
  return draws;
}

describe("the frame cap", () => {
  afterEach(() => setFrameCap(null));

  it("lets every frame through without a cap", () => {
    expect(drawn(60)).toBe(60);
    expect(drawn(120)).toBe(120);
  });

  it("holds a 60 Hz and a 120 Hz screen to 30 draws a second", () => {
    setFrameCap(30);
    expect(drawn(60)).toBeGreaterThanOrEqual(30); // a second that starts mid-slot touches 31 of them
    expect(drawn(60)).toBeLessThanOrEqual(31);
    setFrameCap(30);
    expect(drawn(120)).toBeGreaterThanOrEqual(30);
    expect(drawn(120)).toBeLessThanOrEqual(31);
  });

  it("gives every view in a frame the same answer, so they draw together", () => {
    setFrameCap(30);
    expect([mayDraw(1010), mayDraw(1010)]).toEqual([true, true]);
    expect([mayDraw(1026.7), mayDraw(1026.7)]).toEqual([false, false]);
    expect([mayDraw(1043.4), mayDraw(1043.4)]).toEqual([true, true]);
  });

  it("doesn't leave a view waiting when nothing else asked at the start of the slot", () => {
    setFrameCap(30);
    expect(mayDraw(1020)).toBe(true); // the first frame anyone asks in opens the slot
    expect(mayDraw(1033.3)).toBe(false);
    expect(mayDraw(1033.4)).toBe(true);
  });

  it("takes effect at once when the cap changes", () => {
    setFrameCap(30);
    expect(mayDraw(1010)).toBe(true);
    expect(mayDraw(1026.7)).toBe(false);
    setFrameCap(null);
    expect(mayDraw(1026.7)).toBe(true);
  });
});
