import { describe, expect, it } from "vitest";
import { cueLabelRows, placeCueLabel } from "./draw";

describe("cue label rows", () => {
  it("keeps cue labels off an INTRO label at the same x", () => {
    // "INTRO 16 · …" at the top of the band (drawEdges: y + 3 .. y + 16), cue label rows start at y + 7
    const rows = cueLabelRows([{ x0: 1, x1: 120, y0: 3, y1: 16 }], 0);
    expect(placeCueLabel(rows, 0, 110)).toBe(1);
  });

  it("uses the first row when the edge label is elsewhere", () => {
    const rows = cueLabelRows([{ x0: 600, x1: 700, y0: 3, y1: 16 }], 0);
    expect(placeCueLabel(rows, 0, 110)).toBe(0);
    expect(placeCueLabel(rows, 560, 620)).toBe(1); // reaches into the OUTRO label
  });

  it("drops a label when both rows are taken", () => {
    const rows = cueLabelRows([], 0);
    expect(placeCueLabel(rows, 0, 100)).toBe(0);
    expect(placeCueLabel(rows, 50, 150)).toBe(1);
    expect(placeCueLabel(rows, 90, 200)).toBe(-1);
    expect(placeCueLabel(rows, 160, 200)).toBe(0);
  });

  it("ignores boxes below the label rows", () => {
    expect(cueLabelRows([{ x0: 0, x1: 100, y0: 40, y1: 52 }], 0)).toEqual([[], []]);
  });
});
