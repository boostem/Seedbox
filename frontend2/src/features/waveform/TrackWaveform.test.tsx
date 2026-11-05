// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { PlayheadSource } from "@/audio/playhead";
import { demoAnalysis } from "@/lib/demo/analysis";
import * as draw from "./draw";
import { TrackWaveform } from "./TrackWaveform";
import { waveSettings } from "./settings";

const WIDTH = 1000;
const analysis = demoAnalysis();
const nextFrame = () => act(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));
const anyContext = () => new Proxy({}, { get: (_, k) => (k === "measureText" ? () => ({ width: 10 }) : () => {}), set: () => true });

/** A source whose time and state the test moves. */
function fakeSource(time: number | null, playing = false) {
  const listeners = new Set<() => void>();
  const s: { t: number | null; on: boolean; source: PlayheadSource; change: (t: number | null, on?: boolean) => void } = {
    t: time,
    on: playing,
    source: {
      time: () => s.t,
      playing: () => s.on,
      subscribe: (l: () => void) => {
        listeners.add(l);
        return () => listeners.delete(l);
      },
    } as PlayheadSource,
    change(t: number | null, on = s.on) {
      s.t = t;
      s.on = on;
      act(() => listeners.forEach((l) => l()));
    },
  };
  return s;
}

beforeAll(() => {
  Element.prototype.setPointerCapture = () => {};
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => WIDTH });
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});

let paints: ReturnType<typeof vi.spyOn>;
/** The view of the main waveform's last paint (the overview shows the whole track at 1000 / duration px/s). */
const lastView = (): draw.View => paints.mock.calls.map((c: unknown[]) => c[2] as draw.View).filter((v: draw.View) => v.pxPerSec > WIDTH / analysis.duration_s + 1e-6).pop();

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => anyContext() as never);
  paints = vi.spyOn(draw, "drawWave").mockImplementation(() => {});
  waveSettings.setFollow("centred");
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("TrackWaveform as a deck", () => {
  it("waits with the whole track in view and no position while nothing is loaded", () => {
    const s = fakeSource(null);
    render(<TrackWaveform analysis={analysis} trackId="t" source={s.source} deck />);
    expect(screen.getByText("Bar –")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Centred" })).toBeTruthy();
    const v = paints.mock.calls.at(-1)![2] as draw.View;
    expect(v.start).toBe(0);
    expect(v.pxPerSec).toBeCloseTo(WIDTH / analysis.duration_s);
  });

  it("zooms in and centres the playhead when the track loads, past the start of the track too", async () => {
    const s = fakeSource(0.2);
    render(<TrackWaveform analysis={analysis} trackId="t" source={s.source} deck />);
    await nextFrame();
    const v = lastView();
    expect(v.pxPerSec).toBeCloseTo(WIDTH / (analysis.bar_s * 16));
    expect(v.start).toBeLessThan(0);
    expect(draw.xOf(v, 0.2)).toBeCloseTo(WIDTH / 2, 0);
  });

  it("keeps the playhead on one column while the view scrolls under it, in whole columns", async () => {
    const s = fakeSource(60, true);
    render(<TrackWaveform analysis={analysis} trackId="t" source={s.source} deck />);
    const starts = new Set<number>();
    const t0 = performance.now();
    for (let i = 0; i < 6; i++) {
      s.t = 60 + (performance.now() - t0) / 1000;
      await nextFrame();
      const v = lastView();
      starts.add(v.start);
      const column = v.start * v.pxPerSec;
      expect(Math.abs(column - Math.round(column))).toBeLessThan(1e-6);
      expect(Math.abs(draw.xOf(v, s.t!) - WIDTH / 2)).toBeLessThan(30);
    }
    expect(starts.size).toBeGreaterThan(1);
  });

  it("says where the playhead is in the bar and how far the next phrase is", async () => {
    const s = fakeSource(analysis.first_downbeat_s + analysis.bar_s * 3 + 0.01);
    render(<TrackWaveform analysis={analysis} trackId="t" source={s.source} deck />);
    expect(screen.getByText("Bar 4.1")).toBeTruthy();
    expect(screen.getByTitle("Bars.beats to the next phrase").textContent).toMatch(/phrase in \d+\.\d$/i);
    s.change(null);
    await nextFrame();
    expect(screen.getByText("Bar –")).toBeTruthy();
  });

  it("stays where it was when the track stops", async () => {
    const s = fakeSource(60);
    render(<TrackWaveform analysis={analysis} trackId="t" source={s.source} deck />);
    await nextFrame();
    const before = lastView();
    s.change(null);
    await nextFrame();
    expect(lastView().start).toBeCloseTo(before.start);
  });

  it("lets a drag take the view, and a click bring it back", async () => {
    const s = fakeSource(60);
    const onSeek = vi.fn();
    const { container } = render(<TrackWaveform analysis={analysis} trackId="t" source={s.source} deck onSeek={onSeek} />);
    await nextFrame();
    const before = lastView().start;
    const main = container.querySelector<HTMLElement>(".cursor-crosshair")!;
    fireEvent.pointerDown(main, { button: 0, clientX: 500, pointerId: 1 });
    fireEvent.pointerMove(main, { clientX: 700, pointerId: 1 });
    fireEvent.pointerUp(main, { clientX: 700, pointerId: 1 });
    await nextFrame();
    const dragged = lastView();
    expect(dragged.start).toBeCloseTo(before - 200 / dragged.pxPerSec, 1);
    fireEvent.pointerDown(main, { button: 0, clientX: 500, pointerId: 1 });
    fireEvent.pointerUp(main, { clientX: 500, pointerId: 1 });
    expect(onSeek).toHaveBeenCalled();
    s.change(61);
    await nextFrame();
    expect(draw.xOf(lastView(), 61)).toBeCloseTo(WIDTH / 2, 0);
  });
});

describe("TrackWaveform in page mode", () => {
  it("is what a view without `deck` does: the view stays and turns a page when the playhead leaves it", async () => {
    const s = fakeSource(10, true);
    render(<TrackWaveform analysis={analysis} trackId="t" source={s.source} />);
    expect(screen.queryByRole("tab", { name: "Centred" })).toBeNull();
    expect(screen.queryByText("Bar –")).toBeNull();
    await nextFrame();
    const v = paints.mock.calls.at(-1)![2] as draw.View;
    expect(v.start).toBe(0);
  });

  it("can be forced on a deck, which then has no switch", async () => {
    const s = fakeSource(60);
    render(<TrackWaveform analysis={analysis} trackId="t" source={s.source} deck follow="page" />);
    await nextFrame();
    expect(screen.queryByRole("tab", { name: "Centred" })).toBeNull();
    expect(screen.getByText(/^Bar \d/)).toBeTruthy();
    expect((paints.mock.calls.at(-1)![2] as draw.View).start).toBe(0);
  });
});
