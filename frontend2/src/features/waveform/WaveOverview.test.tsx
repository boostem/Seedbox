// @vitest-environment jsdom
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { player, usePlayer } from "@/audio/player";
import { demoClip } from "@/lib/demo/analysis";
import * as draw from "./draw";
import { WaveOverview } from "./WaveOverview";

const initial = usePlayer.getState();
const clip = demoClip()!;
const nextFrame = () => new Promise((r) => requestAnimationFrame(r));
const WIDTH = 200;

beforeAll(() => {
  Element.prototype.setPointerCapture = () => {}; // jsdom has no pointer capture
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => WIDTH });
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ setTransform() {}, clearRect() {} } as unknown as RenderingContext);
});

let paints: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  paints = vi.spyOn(draw, "drawWave").mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ setTransform() {}, clearRect() {} } as unknown as RenderingContext);
  usePlayer.setState(initial, true);
});

const parts = (c: HTMLElement) => ({ canvas: c.querySelector("canvas")!, head: c.querySelector<HTMLElement>("canvas ~ div")! });

describe("WaveOverview as a preview clip", () => {
  it("draws the clip's three bands with the shared renderer", () => {
    expect(clip.duration).toBe(20);
    expect(clip.n).toBe(1000);
    render(<WaveOverview wave={clip} />);
    expect(paints.mock.calls[0][1].wave).toBe(clip);
    expect(paints.mock.calls[0][2]).toMatchObject({ start: 0, width: WIDTH, pxPerSec: WIDTH / 20 });
  });

  it("moves the playhead on the overlay, without a repaint", async () => {
    let now = 5;
    vi.spyOn(player, "time").mockImplementation(() => now);
    const { container } = render(<WaveOverview wave={clip} trackId="c" />);
    const { canvas, head } = parts(container);
    expect(head.style.display).toBe("none");
    act(() => usePlayer.setState({ currentId: "c", status: "paused", duration: 20, position: 5 }));
    await nextFrame();
    expect(head.style.display).toBe("");
    expect(head.style.transform).toBe("translateX(50px)");
    expect(canvas.style.maskImage).toContain("black 50px");

    const painted = paints.mock.calls.length;
    act(() => usePlayer.setState({ status: "playing" }));
    for (const t of [10, 15]) {
      now = t;
      await nextFrame();
      await nextFrame();
      expect(head.style.transform).toBe(`translateX(${t * 10}px)`);
    }
    expect(paints.mock.calls.length).toBe(painted);

    act(() => usePlayer.setState({ currentId: "other", status: "playing" }));
    await nextFrame();
    expect(head.style.display).toBe("none");
    expect(canvas.style.maskImage).toBe("");
  });

  it("doesn't render React while it plays", async () => {
    let commits = 0;
    vi.spyOn(player, "time").mockReturnValue(1);
    const Counted = () => {
      commits++;
      return <WaveOverview wave={clip} trackId="c" />;
    };
    render(<Counted />);
    const before = commits;
    act(() => usePlayer.setState({ currentId: "c", status: "playing", duration: 20 }));
    for (let i = 0; i < 5; i++) act(() => usePlayer.setState({ position: i }));
    await nextFrame();
    expect(commits).toBe(before);
  });

  it("clamps the playhead to the clip", async () => {
    vi.spyOn(player, "time").mockReturnValue(99);
    const { container } = render(<WaveOverview wave={clip} trackId="c" />);
    act(() => usePlayer.setState({ currentId: "c", status: "paused", duration: 99 }));
    await nextFrame();
    expect(parts(container).head.style.transform).toBe(`translateX(${WIDTH}px)`);
  });

  it("seeks on click and drag, clamped, not on hover", () => {
    const onSeek = vi.fn();
    const { container } = render(<WaveOverview wave={clip} onSeek={onSeek} />);
    const box = container.firstElementChild as HTMLElement;
    box.getBoundingClientRect = () => ({ left: 100, width: 200, top: 0, height: 28, right: 300, bottom: 28, x: 100, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(box, { clientX: 150, button: 0 });
    expect(onSeek).toHaveBeenLastCalledWith(5);
    fireEvent.pointerMove(box, { clientX: 400, buttons: 1 });
    expect(onSeek).toHaveBeenLastCalledWith(20);
    fireEvent.pointerMove(box, { clientX: 200, buttons: 0 });
    expect(onSeek).toHaveBeenCalledTimes(2);
  });
});
