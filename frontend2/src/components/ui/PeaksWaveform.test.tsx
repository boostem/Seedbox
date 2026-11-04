// @vitest-environment jsdom
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { usePlayer } from "@/audio/player";
import { PeaksWaveform } from "./PeaksWaveform";

const initial = usePlayer.getState();
afterEach(() => {
  cleanup();
  usePlayer.setState(initial, true);
});

beforeAll(() => {
  // jsdom has no pointer capture
  Element.prototype.setPointerCapture = () => {};
});

const nextFrame = () => new Promise((r) => requestAnimationFrame(r));
const PEAKS = Array.from({ length: 200 }, (_, i) => (i % 10) / 10);

describe("PeaksWaveform", () => {
  it("renders one bar per peak", () => {
    const { container } = render(<PeaksWaveform peaks={PEAKS} />);
    expect(container.querySelectorAll("svg > g > rect")).toHaveLength(200);
    expect(container.querySelector("svg")?.getAttribute("viewBox")).toBe("0 0 600 28");
  });

  it("resamples to `bars`, taking each bucket's max", () => {
    const { container } = render(<PeaksWaveform peaks={[0.1, 0.5, 0.2, 1]} bars={2} height={10} />);
    const heights = [...container.querySelectorAll("svg > g > rect")].map((r) => r.getAttribute("height"));
    expect(heights).toEqual(["5", "10"]);
  });

  it("caps at 240 bars", () => {
    const { container } = render(<PeaksWaveform peaks={Array(500).fill(0.5)} />);
    expect(container.querySelectorAll("svg > g > rect")).toHaveLength(240);
  });

  it("renders a flat line without peaks", () => {
    const { container } = render(<PeaksWaveform peaks={null} loading />);
    expect(container.querySelector("svg")).toBeNull();
    expect(container.querySelector(".animate-pulse-soft")).not.toBeNull();
  });

  it("seeks to the clicked fraction, clamped", () => {
    const onSeek = vi.fn();
    const { container } = render(<PeaksWaveform peaks={PEAKS} onSeek={onSeek} />);
    const svg = container.querySelector("svg")!;
    svg.getBoundingClientRect = () => ({ left: 100, width: 200, top: 0, height: 28, right: 300, bottom: 28, x: 100, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(svg, { clientX: 150 });
    expect(onSeek).toHaveBeenLastCalledWith(0.25);
    fireEvent.pointerMove(svg, { clientX: 400, buttons: 1 });
    expect(onSeek).toHaveBeenLastCalledWith(1);
    fireEvent.pointerMove(svg, { clientX: 200, buttons: 0 }); // hover without a button: no seek
    expect(onSeek).toHaveBeenCalledTimes(2);
  });

  it("shows the played part and playhead while the player holds this track", async () => {
    const { container } = render(<PeaksWaveform peaks={PEAKS} trackId="t1" />);
    const head = container.querySelector("svg > rect")!;
    const clip = container.querySelector("clipPath > rect")!;
    expect(head.getAttribute("visibility")).toBe("hidden");
    act(() => usePlayer.setState({ currentId: "t1", status: "paused", duration: 10, position: 5 }));
    await nextFrame(); // drawn on the next animation frame
    expect(head.getAttribute("visibility")).toBe("visible");
    expect(clip.getAttribute("width")).toBe("300");
    act(() => usePlayer.setState({ currentId: "other" }));
    await nextFrame();
    expect(head.getAttribute("visibility")).toBe("hidden");
  });
});
