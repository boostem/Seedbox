// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { LANE_DEFAULTS, LANES } from "@/api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan } from "@/api/mixplan.generated";
import { PlanTimeline } from "./PlanTimeline";

const WIDTH = 640;
const kf = (beat: number, value: number): Keyframe => ({ beat, value, curve: "linear" });

function deck(): Deck {
  const lanes = Object.fromEntries(LANES.map((n) => [n, [kf(0, LANE_DEFAULTS[n])]])) as unknown as Lanes;
  return {
    track_id: "t",
    grid: [{ start_beat: 0, start_s: 0, bpm: 120 }],
    key_shift: 0,
    play_from_beat: 0,
    play_to_beat: 64,
    lanes: { ...lanes, volume: [kf(0, 0), kf(32, 1)] },
    stem_lanes: null,
    sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "pre_eq" },
    loops: [],
    fx: [],
    pinned: [],
  };
}

const plan = (): MixPlan => ({ version: 3, technique: "t", bars: 8, key_lock: true, window: { start_beat: 0, end_beat: 64 }, tempo: [kf(0, 120)], a: deck(), b: deck(), markers: [], notes: [], edited: false });

beforeAll(() => {
  Element.prototype.setPointerCapture = () => {};
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => WIDTH });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: WIDTH, height: 192 } as DOMRect);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});
afterEach(cleanup);

function setup() {
  const engine = { beat: vi.fn(() => 0), scrub: vi.fn(), endScrub: vi.fn(), seek: vi.fn(), setLoop: vi.fn() };
  const onLoop = vi.fn();
  const planRef = { current: plan() };
  render(<PlanTimeline planRef={planRef} engine={engine} waves={{ a: null, b: null }} loop={null} onLoop={onLoop} />);
  return { engine, onLoop, box: screen.getByTestId("plan-timeline") };
}
const at = (x: number, y: number) => ({ clientX: x, clientY: y, button: 0, pointerId: 1 });
const WAVE_Y = 60; // inside deck A, between the volume lane's points
const RULER_Y = 10;

describe("the plan timeline", () => {
  it("scrubs as the pointer drags over the waveforms, to the beat under it, and lets go", () => {
    const { engine, box } = setup();
    fireEvent.pointerDown(box, at(160, WAVE_Y));
    expect(engine.scrub).toHaveBeenLastCalledWith(16);
    fireEvent.pointerMove(box, at(320, WAVE_Y));
    expect(engine.scrub).toHaveBeenLastCalledWith(32);
    expect(engine.endScrub).not.toHaveBeenCalled();
    fireEvent.pointerUp(box, at(320, WAVE_Y));
    expect(engine.endScrub).toHaveBeenCalledTimes(1);
    fireEvent.pointerMove(box, at(400, WAVE_Y));
    expect(engine.scrub).toHaveBeenCalledTimes(2);
  });

  it("selects a loop on whole beats on the ruler; a click there clears it", () => {
    const { engine, onLoop, box } = setup();
    fireEvent.pointerDown(box, at(101, RULER_Y));
    fireEvent.pointerMove(box, at(203, RULER_Y));
    fireEvent.pointerUp(box, at(203, RULER_Y));
    expect(onLoop).toHaveBeenLastCalledWith([10, 20]);
    expect(engine.scrub).not.toHaveBeenCalled();
    fireEvent.pointerDown(box, at(101, RULER_Y));
    fireEvent.pointerUp(box, at(101, RULER_Y));
    expect(onLoop).toHaveBeenLastCalledWith(null);
  });
});
