// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { LANE_DEFAULTS, LANES } from "@/api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan } from "@/api/mixplan.generated";
import { TransitionEditor } from "./TransitionEditor";

const kf = (beat: number, value: number): Keyframe => ({ beat, value, curve: "linear" });

function deck(id: string): Deck {
  const lanes = Object.fromEntries(LANES.map((n) => [n, [kf(0, LANE_DEFAULTS[n])]])) as unknown as Lanes;
  return { track_id: id, grid: [{ start_beat: 0, start_s: 0, bpm: 120 }], key_shift: 0, play_from_beat: 0, play_to_beat: 64, lanes: { ...lanes, volume: [kf(0, 1), kf(64, 0)] }, stem_lanes: null, sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "pre_eq" }, loops: [], fx: [], pinned: [] };
}
const plan = (over: Partial<MixPlan> = {}): MixPlan => ({ version: 3, technique: "long blend", bars: 16, key_lock: true, window: { start_beat: -8, end_beat: 72 }, tempo: [kf(0, 120)], a: deck("h1"), b: deck("h2"), markers: [], notes: [], edited: false, ...over });

const h = vi.hoisted(() => ({
  live: vi.fn(),
  save: vi.fn(async () => ({ saved: true })),
  reset: vi.fn(async () => ({ reset: true })),
  stems: { a: false, b: true },
  schedule: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  apiUrl: (p: string) => p,
  api: {
    mixes: { livePlan: h.live, savePlan: h.save, resetPlan: h.reset },
    key: { status: async () => ({ effective: { camelot: "8A" } }) },
  },
}));
vi.mock("@/audio/player", () => ({ player: { stop: vi.fn() } }));
vi.mock("@/audio/engine", () => ({
  LiveEngine: class {
    state = { status: "paused", decks: { a: "full", b: "full" }, error: null };
    subscribe() {
      return () => {};
    }
    load = vi.fn(async () => {});
    schedule = h.schedule;
    hasStems(id: "a" | "b") {
      return h.stems[id];
    }
    close = vi.fn(async () => {});
    beat = () => 0;
    scrub = vi.fn();
    endScrub = vi.fn();
    seek = vi.fn();
    setLoop = vi.fn();
    play = vi.fn(async () => {});
    pause = vi.fn();
  },
}));
vi.mock("@/features/waveform/waveData", () => ({
  loadWave: async () => ({ pps: 10, n: 2000, duration: 200, low: new Float32Array(2000).fill(1), mid: new Float32Array(2000).fill(1), high: new Float32Array(2000).fill(1) }),
  useStemWave: () => ({ state: "none", wave: null, retry: () => {} }),
}));
vi.mock("@/features/waveform/palette", () => ({
  alpha: (c: string) => c,
  wavePalette: () => ({ low: "l", mid: "m", high: "h", accent: "a", bg: "b", fg: "f", panel: "p", playhead: "x", font: "sans", stem: { drums: "d", bass: "b", vocals: "v", other: "o" } }),
}));

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 640 });
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});
beforeEach(() => {
  localStorage.clear();
  h.live.mockReset();
  h.live.mockResolvedValue({ plan: plan(), user_edited: false, auto: null });
  h.save.mockClear();
  h.reset.mockClear();
  h.schedule.mockClear();
});
afterEach(cleanup);

async function open() {
  render(<TransitionEditor from="h1" to="h2" />);
  await screen.findByTestId("plan-timeline");
}
const lastPlan = (): MixPlan => h.schedule.mock.calls[h.schedule.mock.calls.length - 1][0];
const button = (name: RegExp | string) => screen.getByRole("button", { name });

describe("the transition editor", () => {
  it("opens on Simple, with the plan as a ribbon and the checks", async () => {
    await open();
    expect(screen.getByRole("tab", { name: "Simple" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("plan-ribbon")).toBeTruthy();
    expect(screen.getByText("One bassline at a time")).toBeTruthy();
    expect(screen.getByText("Key and tempo")).toBeTruthy();
    expect(screen.getByText("One vocal at a time")).toBeTruthy();
    expect(screen.getByText("Even loudness")).toBeTruthy();
    expect(button("Undo").hasAttribute("disabled")).toBe(true);
    expect(button("Reset to auto").hasAttribute("disabled")).toBe(true);
  });

  it("a style is heard at once, is one undo step, and comes back with redo", async () => {
    await open();
    fireEvent.click(screen.getByRole("radio", { name: "Echo out" }));
    expect(lastPlan().technique).toBe("echo out");
    expect(lastPlan().bars).toBe(4);
    expect(screen.getByRole("radio", { name: "Echo out" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(button("Undo"));
    expect(lastPlan().technique).toBe("long blend");
    fireEvent.click(button("Redo"));
    expect(lastPlan().technique).toBe("echo out");
  });

  it("a slider held down is one undo step, and its value shows", async () => {
    await open();
    const fx = screen.getByRole("slider", { name: "FX" });
    fireEvent.change(fx, { target: { value: "0.2" } });
    fireEvent.change(fx, { target: { value: "0.9" } });
    fireEvent.pointerUp(fx);
    expect(screen.getByText("90%")).toBeTruthy();
    fireEvent.click(button("Undo"));
    expect(button("Undo").hasAttribute("disabled")).toBe(true);
    expect(lastPlan().technique).toBe("long blend");
  });

  it("Ctrl+Z and Ctrl+Shift+Z undo and redo", async () => {
    await open();
    fireEvent.click(screen.getByRole("radio", { name: "Bass swap" }));
    fireEvent.keyDown(document.body, { key: "z", ctrlKey: true });
    expect(lastPlan().technique).toBe("long blend");
    fireEvent.keyDown(document.body, { key: "z", ctrlKey: true, shiftKey: true });
    expect(lastPlan().technique).toBe("bass swap");
  });

  it("writes stem lanes for the deck with stems and EQ for the one without", async () => {
    await open();
    fireEvent.click(screen.getByRole("radio", { name: "Bass swap" }));
    expect(lastPlan().a.stem_lanes).toBeNull();
    expect(lastPlan().b.stem_lanes).not.toBeNull();
  });

  it("Advanced: stem rows for a deck with stems, EQ rows for one without, and the choice is remembered", async () => {
    await open();
    fireEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    for (const lane of ["volume", "gain_drums", "gain_bass", "gain_vocals", "gain_other"]) expect(screen.getByTestId(`lane-b-${lane}`)).toBeTruthy();
    for (const lane of ["volume", "eq_low", "eq_mid", "eq_high"]) expect(screen.getByTestId(`lane-a-${lane}`)).toBeTruthy();
    expect(screen.queryByTestId("lane-a-gain_bass")).toBeNull();
    expect(screen.queryByTestId("lane-b-filter")).toBeNull();
    expect(screen.getByText(/Deck A: no stems/)).toBeTruthy();
    cleanup();
    await open();
    expect(screen.getByRole("tab", { name: "Advanced" }).getAttribute("aria-selected")).toBe("true");
  });

  it("Manual: every lane of both decks", async () => {
    localStorage.setItem("seedbox2.mix.view", "manual");
    await open();
    for (const lane of LANES) {
      expect(screen.getByTestId(`lane-a-${lane}`)).toBeTruthy();
      expect(screen.getByTestId(`lane-b-${lane}`)).toBeTruthy();
    }
    expect(screen.getByTestId("lane-b-gain_vocals")).toBeTruthy();
    expect(screen.queryByTestId("lane-a-gain_vocals")).toBeNull();
  });

  it("Manual: a keyframe in numbers", async () => {
    localStorage.setItem("seedbox2.mix.view", "manual");
    await open();
    const row = screen.getByTestId("lane-a-volume");
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 640, height: 40 } as DOMRect);
    Element.prototype.setPointerCapture = () => {};
    // the fader's keyframe at beat 0, value 1: x = 8 / 80 * 640, at the top of the row
    fireEvent.pointerDown(row, { clientX: 64, clientY: 5, button: 0, pointerId: 1 });
    fireEvent.pointerUp(row, { clientX: 64, clientY: 5, button: 0, pointerId: 1 });
    const inspector = await screen.findByTestId("keyframe-inspector");
    const value = inspector.querySelectorAll("input")[1];
    fireEvent.change(value, { target: { value: "0.25" } });
    fireEvent.blur(value);
    expect(lastPlan().a.lanes.volume[0].value).toBe(0.25);
    fireEvent.change(screen.getByRole("combobox", { name: "Curve" }), { target: { value: "hold" } });
    expect(lastPlan().a.lanes.volume[0].curve).toBe("hold");
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(lastPlan().a.lanes.volume).toHaveLength(1);
  });

  it("a check's Fix button is one edit, and undo takes it back", async () => {
    await open();
    await screen.findByText(/^\d+\.\d s$/);
    const fix = await waitFor(() => {
      const found = screen.getAllByRole("button", { name: "Fix" });
      expect(found.length).toBeGreaterThan(0);
      return found[0];
    });
    fireEvent.click(fix);
    expect(lastPlan().edited).toBe(true);
    fireEvent.click(button("Undo"));
    expect(lastPlan().edited).toBe(false);
  });

  it("saves an edit a moment later, and only the last of a run of them", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await open();
      fireEvent.click(screen.getByRole("radio", { name: "Bass swap" }));
      fireEvent.click(screen.getByRole("radio", { name: "Echo out" }));
      expect(h.save).not.toHaveBeenCalled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(700);
      });
      expect(h.save).toHaveBeenCalledTimes(1);
      expect((h.save.mock.calls[0] as unknown[])[2]).toMatchObject({ technique: "echo out" });
      expect(screen.getByRole("status").textContent).toBe("Saved");
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens on a saved edit, and resets to what it started from on the server too", async () => {
    h.live.mockResolvedValue({ plan: plan({ technique: "bass swap", edited: true }), user_edited: true, auto: plan() });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await open();
      expect(lastPlan().technique).toBe("bass swap");
      fireEvent.click(button("Reset to auto"));
      expect(lastPlan().technique).toBe("long blend");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(700);
      });
      expect(h.reset).toHaveBeenCalledTimes(1);
      expect(h.save).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("says when an edit couldn't be saved", async () => {
    h.save.mockRejectedValueOnce(new Error("this plan can't be saved"));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await open();
      fireEvent.click(screen.getByRole("radio", { name: "Bass swap" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(700);
      });
      expect(screen.getByRole("status").textContent).toBe("Couldn't save");
    } finally {
      vi.useRealTimers();
    }
  });

  it("A: Original on a plan a person made, as an import is", async () => {
    h.live.mockResolvedValue({ plan: plan({ edited: true }), user_edited: true, auto: null });
    await open();
    expect(button("A: Original")).toBeTruthy();
    expect(button("Reset to original")).toBeTruthy();
  });
});
