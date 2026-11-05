// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { LANE_DEFAULTS, LANES } from "@/api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan } from "@/api/mixplan.generated";
import { type KeySelection, LaneStack } from "./LaneStack";

const WIDTH = 640;
const ROW = 60;
const kf = (beat: number, value: number): Keyframe => ({ beat, value, curve: "linear" });

function deck(): Deck {
  const lanes = Object.fromEntries(LANES.map((n) => [n, [kf(0, LANE_DEFAULTS[n])]])) as unknown as Lanes;
  return { track_id: "t", grid: [{ start_beat: 0, start_s: 0, bpm: 120 }], key_shift: 0, play_from_beat: 0, play_to_beat: 64, lanes: { ...lanes, volume: [kf(0, 0), kf(32, 1)] }, stem_lanes: null, sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "pre_eq" }, loops: [], fx: [], pinned: [] };
}

const plan = (): MixPlan => ({ version: 3, technique: "t", bars: 8, key_lock: true, window: { start_beat: 0, end_beat: 64 }, tempo: [kf(0, 120)], a: deck(), b: deck(), markers: [], notes: [], edited: false });

beforeAll(() => {
  Element.prototype.setPointerCapture = () => {};
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => WIDTH });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: WIDTH, height: ROW } as DOMRect);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});
afterEach(cleanup);

function setup(start = plan()) {
  const planRef = { current: start };
  const begin = vi.fn();
  const end = vi.fn();
  const edit = vi.fn((p: MixPlan) => {
    planRef.current = p;
  });
  const onSelect = vi.fn<(s: KeySelection | null) => void>();
  render(
    <LaneStack
      groups={[{ title: "Deck A", rows: [{ deck: "a", lane: "volume", label: "Fader" }, { deck: "a", lane: "gain_bass", label: "Bass" }] }]}
      rowHeight={ROW}
      revision={0}
      beat={() => 0}
      editing={{ planRef, begin, edit, end, selected: null, onSelect }}
    />,
  );
  return { planRef, begin, end, edit, onSelect, row: screen.getByTestId("lane-a-volume"), stem: screen.getByTestId("lane-a-gain_bass") };
}
const at = (x: number, y: number, extra = {}) => ({ clientX: x, clientY: y, button: 0, pointerId: 1, ...extra });
const TOP = 5; // value 1 in the row

describe("a lane row", () => {
  it("makes a drag one gesture with an edit on every move, snapped and in range", () => {
    const { begin, edit, end, planRef, row, onSelect } = setup();
    fireEvent.pointerDown(row, at(320, TOP));
    expect(begin).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenLastCalledWith({ deck: "a", lane: "volume", index: 1 });
    fireEvent.pointerMove(row, at(321, 30));
    fireEvent.pointerMove(row, at(321, 500));
    fireEvent.pointerUp(row, at(321, 500));
    expect(edit).toHaveBeenCalledTimes(2);
    expect(end).toHaveBeenCalledTimes(1);
    const moved = planRef.current.a.lanes.volume[1];
    expect(moved.beat % (1 / 16)).toBe(0);
    expect(moved.value).toBe(0);
    expect(planRef.current.edited).toBe(true);
    fireEvent.pointerMove(row, at(400, 30));
    expect(edit).toHaveBeenCalledTimes(2);
  });

  it("does nothing to the plan for a click beside the keyframes, and clears the selection", () => {
    const { begin, edit, row, onSelect } = setup();
    fireEvent.pointerDown(row, at(480, 30));
    fireEvent.pointerUp(row, at(480, 30));
    expect(begin).not.toHaveBeenCalled();
    expect(edit).not.toHaveBeenCalled();
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("adds a keyframe on a double-click and selects it, but not on top of one", () => {
    const { planRef, row, onSelect, edit } = setup();
    fireEvent.doubleClick(row, at(480, 30));
    expect(planRef.current.a.lanes.volume).toHaveLength(3);
    expect(onSelect).toHaveBeenLastCalledWith({ deck: "a", lane: "volume", index: 2 });
    fireEvent.doubleClick(row, at(320, TOP));
    expect(edit).toHaveBeenCalledTimes(1);
  });

  it("removes a keyframe on a right-click and leaves the menu alone elsewhere", () => {
    const { planRef, row } = setup();
    expect(fireEvent.contextMenu(row, at(320, TOP))).toBe(false);
    expect(planRef.current.a.lanes.volume).toHaveLength(1);
    expect(fireEvent.contextMenu(row, at(480, 30))).toBe(true);
  });

  it("changes a keyframe's curve on an Alt-click, without a drag", () => {
    const { planRef, row, begin } = setup();
    fireEvent.pointerDown(row, at(320, TOP, { altKey: true }));
    expect(planRef.current.a.lanes.volume[1].curve).toBe("ease");
    expect(begin).not.toHaveBeenCalled();
  });

  it("starts a stem lane on a deck that has none with a double-click", () => {
    const { planRef, stem } = setup();
    fireEvent.doubleClick(stem, at(320, 30));
    expect(planRef.current.a.stem_lanes?.gain_bass).toHaveLength(1);
    expect(planRef.current.a.stem_lanes?.gain_drums[0].value).toBe(1);
  });
});
