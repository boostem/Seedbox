// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { SetOrderTrack, SetTimelineData } from "@/api/types";
import { layoutTimeline } from "./layout";
import { SetPlayback } from "./playback";
import { SetTimeline, type SetTimelineProps } from "./SetTimeline";

vi.mock("@/features/waveform/waveData", () => ({ loadWave: () => new Promise(() => {}) }));

const order = (hashes: string[]): SetOrderTrack[] =>
  hashes.map((hash, position) => ({ position, hash, title: hash, artist: "", bpm: 120, key: "8A", energy: position / 2, rating: 5, shift: 0, pinned: false, target_energy: 0.5, target_bpm: null }));

const data: SetTimelineData = {
  tracks: ["a", "b", "c"].map((hash) => ({ hash, duration_s: 100, sections: [], curve: [], bar_s: 2 })),
  transitions: [
    { from: "a", to: "b", state: "ready", seconds: 20, a_in_s: 80, a_out_s: 100, b_in_s: 0, b_out_s: 20 },
    { from: "b", to: "c", state: "ready", seconds: 20, a_in_s: 80, a_out_s: 100, b_in_s: 0, b_out_s: 20 },
  ],
};

const layout = layoutTimeline(order(["a", "b", "c"]), [], data); // 260 s long
const LANE_Y = 100;

function setup(over: Partial<SetTimelineProps> = {}) {
  const playback = new SetPlayback({ engine: vi.fn(), plan: vi.fn() });
  playback.setLayout(layout);
  const fns = { onArc: vi.fn(), onReorder: vi.fn(), onOpen: vi.fn() };
  const props = { layout, arc: [[0, 0], [1, 1]] as [number, number][], drawing: false, reorderable: true, pinned: new Set<number>(), open: null, playback, ...over };
  render(<SetTimeline {...props} {...fns} />);
  const line = screen.getByTestId("set-timeline");
  line.getBoundingClientRect = () => ({ left: 0, top: 0, width: 260, height: 200, right: 260, bottom: 200, x: 0, y: 0, toJSON: () => ({}) }); // 1 px = 1 s
  return { ...fns, playback, line };
}

const drag = (line: HTMLElement, from: number, to: number, y = LANE_Y) => {
  fireEvent.pointerDown(line, { pointerId: 1, clientX: from, clientY: y });
  fireEvent.pointerMove(line, { pointerId: 1, clientX: to, clientY: y });
  fireEvent.pointerUp(line, { pointerId: 1, clientX: to, clientY: y });
};

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    disconnect() {}
    unobserve() {}
  } as unknown as typeof ResizeObserver;
  HTMLCanvasElement.prototype.getContext = (() => null) as never;
});
afterEach(cleanup);

describe("SetTimeline", () => {
  it("reorders a dragged track", () => {
    const { line, onReorder, onOpen } = setup();
    drag(line, 30, 255);
    expect((onReorder.mock.calls[0][0] as SetOrderTrack[]).map((t) => t.hash)).toEqual(["b", "c", "a"]);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("keeps a pinned track where it is: dragging it scrolls instead", () => {
    const { line, onReorder } = setup({ pinned: new Set([0]) });
    drag(line, 30, 255);
    expect(onReorder).not.toHaveBeenCalled();
  });

  it("doesn't reorder when reordering is off, or for a drop where it was", () => {
    const off = setup({ reorderable: false });
    drag(off.line, 30, 255);
    expect(off.onReorder).not.toHaveBeenCalled();
    cleanup();
    const same = setup();
    drag(same.line, 30, 40);
    expect(same.onReorder).not.toHaveBeenCalled();
  });

  it("opens a clicked transition", () => {
    const { line, onOpen, playback } = setup();
    drag(line, 90, 90);
    expect(onOpen).toHaveBeenCalledOnce();
    expect(onOpen.mock.calls[0][0].from).toBe("a");
    expect(playback.time()).toBe(0);
  });

  it("puts the playhead where you click a track or the ruler", () => {
    const { line, onOpen, playback } = setup();
    drag(line, 40, 40);
    expect(playback.time()).toBeCloseTo(40);
    drag(line, 90, 90, 30); // the ruler, over a transition
    expect(playback.time()).toBeCloseTo(90);
    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.getByTestId("set-clock").textContent).toContain("1:30");
  });

  it("zooms with ctrl and the wheel, around the pointer, and scrolls", () => {
    const { line, playback } = setup();
    fireEvent.wheel(line, { ctrlKey: true, deltaY: -100, clientX: 130 });
    // zoomed in: the same pixel is still 130 s, but 40 px off is now less than 40 s
    drag(line, 170, 170);
    expect(playback.time()).toBeGreaterThan(130);
    expect(playback.time()).toBeLessThan(170);
    const before = playback.time();
    fireEvent.wheel(line, { deltaX: 40, deltaY: 0, clientX: 130 });
    drag(line, 170, 170);
    expect(playback.time()).toBeGreaterThan(before);
  });

  it("draws the arc only in draw mode, and tells on letting go", () => {
    const { line, onArc, onReorder } = setup({ drawing: true });
    fireEvent.pointerDown(line, { pointerId: 1, clientX: 0, clientY: 200 });
    fireEvent.pointerMove(line, { pointerId: 1, clientX: 260, clientY: 44 });
    expect(onArc).not.toHaveBeenCalled(); // no render per move
    fireEvent.pointerUp(line, { pointerId: 1 });
    expect(onReorder).not.toHaveBeenCalled();
    const points = onArc.mock.calls[0][0] as [number, number][];
    expect(points[0]).toEqual([0, 0]);
    expect(points[points.length - 1]).toEqual([1, 1]);
  });

  it("has nothing to play with one track", () => {
    const one = layoutTimeline(order(["a"]), [], { tracks: data.tracks.slice(0, 1), transitions: [] });
    setup({ layout: one });
    expect((screen.getByRole("button", { name: "Play" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
