import { describe, expect, it } from "vitest";
import { arcPath, arcPoints, paint, toGrid, toPoints } from "./arc";

describe("drawing an arc", () => {
  it("samples a template onto the grid", () => {
    const grid = toGrid([[0, 0], [0.5, 1], [1, 0]], 5);
    expect(grid).toEqual([0, 0.5, 1, 0.5, 0]);
  });

  it("holds the ends flat when the curve doesn't reach them", () => {
    expect(toGrid([[0.25, 0.2], [0.75, 0.8]], 5)).toEqual([0.2, 0.2, 0.5, 0.8, 0.8]);
  });

  it("paints every time between two pointer events, so a fast drag leaves no gap", () => {
    const flat = new Array(5).fill(0.5);
    expect(paint(flat, { t: 0, v: 0 }, { t: 1, v: 1 })).toEqual([0, 0.25, 0.5, 0.75, 1]);
  });

  it("paints backwards as well as forwards", () => {
    const flat = new Array(5).fill(0.5);
    expect(paint(flat, { t: 1, v: 1 }, { t: 0.5, v: 0 })).toEqual([0.5, 0.5, 0, 0.5, 1]);
  });

  it("paints one time for a click, and leaves the rest", () => {
    const flat = new Array(5).fill(0.5);
    expect(paint(flat, null, { t: 0.25, v: 1 })).toEqual([0.5, 1, 0.5, 0.5, 0.5]);
  });

  it("keeps a stroke that leaves the box inside it", () => {
    const flat = new Array(3).fill(0.5);
    expect(paint(flat, null, { t: 1.4, v: -2 })).toEqual([0.5, 0.5, 0]);
  });

  it("turns levels into the points the API takes", () => {
    expect(toPoints([0, 1 / 3, 1])).toEqual([[0, 0], [0.5, 0.333], [1, 1]]);
  });

  it("makes a path with level 1 at the top", () => {
    expect(arcPath([[0, 1], [1, 0]], 100, 50)).toBe("M0.0 0.0 L100.0 50.0");
  });

  it("finds the points of a template, a drawn curve or the default", () => {
    const templates = [{ name: "peak_time", points: [[0, 0.5], [1, 0.5]] as [number, number][] }, { name: "flat", points: [[0, 1], [1, 1]] as [number, number][] }];
    expect(arcPoints("flat", templates)).toEqual([[0, 1], [1, 1]]);
    expect(arcPoints(null, templates)).toEqual([[0, 0.5], [1, 0.5]]);
    expect(arcPoints([[0, 0], [1, 1]], templates)).toEqual([[0, 0], [1, 1]]);
  });
});
