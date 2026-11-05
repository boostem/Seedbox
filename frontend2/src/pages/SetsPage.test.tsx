// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { SetOrderResult, SetTimelineData } from "@/api/types";

const sets = vi.hoisted(() => ({ exportStatus: vi.fn(() => Promise.resolve(null)), list: vi.fn(), arcs: vi.fn(), order: vi.fn(), solveOrder: vi.fn(), applyOrder: vi.fn(), timeline: vi.fn(), bridge: vi.fn(), bridgeOnline: vi.fn(), insertBridge: vi.fn(), create: vi.fn() }));
const assistant = vi.hoisted(() => ({ status: vi.fn(), run: vi.fn(), cancel: vi.fn(), dryRun: vi.fn() }));
const library = vi.hoisted(() => ({ crates: vi.fn() }));
const navigate = vi.hoisted(() => vi.fn());
vi.mock("@/api/client", () => ({ api: { sets, assistant, library, tunebat: { download: vi.fn() }, mixes: { livePlan: vi.fn() } }, apiUrl: (p: string) => p, openEventStream: vi.fn() }));
vi.mock("@/features/mix/TransitionEditor", () => ({ TransitionEditor: ({ from, to }: { from: string; to: string }) => <div>Editor {from} to {to}</div> }));
vi.mock("@/features/waveform/waveData", () => ({ loadWave: () => new Promise(() => {}) }));
vi.mock("@/lib/router", () => ({ navigate }));
const { SetsPage } = await import("./SetsPage");

const ARCS = [
  { name: "warm_up", label: "Warm-up", description: "", points: [[0, 0], [1, 1]] },
  { name: "peak_time", label: "Peak time", description: "", points: [[0, 0.5], [1, 1]] },
];

function result(hashes: string[], over: Partial<SetOrderResult> = {}): SetOrderResult {
  return {
    order: hashes.map((hash, position) => ({ position, hash, title: `Song ${hash}`, artist: "X", bpm: 124, key: "8A", energy: position / 9, rating: position + 1, shift: 0, pinned: false, target_energy: 0.5, target_bpm: 124 })),
    transitions: hashes.slice(1).map((to, i) => ({ from: hashes[i], to, text: `energy +1, 8A → 9A, step ${i}`, flags: [], cost: 1, score: 80, gap: false })),
    cost: 5,
    cost_before: 9,
    arc: ARCS[1] as SetOrderResult["arc"],
    tempo: { start: 122, end: 126, auto: true },
    unknown: { bpm: 0, key: 0, energy: 0, vibe: 0, vocals: 0, genre: 0 },
    settings: { arc: null, tempo_start: null, tempo_end: null, pins: [] },
    ...over,
  };
}

/** Every pair has a plan unless it's listed in `planning`. */
function timeline(hashes: string[], planning: string[] = []): SetTimelineData {
  return {
    tracks: hashes.map((hash) => ({ hash, duration_s: 100, sections: [], curve: [], bar_s: 2 })),
    transitions: hashes.slice(1).map((to, i) => {
      const from = hashes[i];
      return planning.includes(`${from}/${to}`) ? { from, to, state: "pending" as const } : { from, to, state: "ready" as const, technique: "long blend", bars: 8, seconds: 20, a_in_s: 80, a_out_s: 100, b_in_s: 0, b_out_s: 20 };
    }),
  };
}

const box = { left: 0, top: 0, width: 260, height: 200, right: 260, bottom: 200, x: 0, y: 0, toJSON: () => ({}) };
const LANE_Y = 100; // inside the first lane

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    disconnect() {}
    unobserve() {}
  } as unknown as typeof ResizeObserver;
  HTMLCanvasElement.prototype.getContext = (() => null) as never;
});

beforeEach(() => {
  vi.resetAllMocks();
  sets.timeline.mockImplementation((_id: number, hashes: string[]) => Promise.resolve(timeline(hashes)));
  sets.list.mockResolvedValue([{ id: 1, name: "Friday", track_count: 3 }]);
  sets.arcs.mockResolvedValue(ARCS);
  sets.order.mockResolvedValue(result(["a", "b", "c"]));
  assistant.status.mockResolvedValue({ enabled: false, model: "claude-sonnet-5-5", models: [], has_key: false, keychain: true });
  library.crates.mockResolvedValue([{ id: 7, name: "Deep", folder: null, source: "local", track_count: 4, updated_at: "" }]);
});

const ON = { enabled: true, model: "claude-sonnet-5-5", models: [], has_key: true, keychain: true };
const draft = (hashes: string[], over: Partial<SetOrderResult> = {}) => ({
  name: "Sunset",
  reasoning: "Mellow first, peak at two thirds.",
  result: result(hashes, { settings: { arc: "sunset", tempo_start: 120, tempo_end: 124, pins: [{ position: 1, hash: hashes[1] }], note: "Mellow first, peak at two thirds." }, ...over }),
  steps: ["Ordering the set"],
  model: "claude-sonnet-5-5",
  usage: { input_tokens: 10, output_tokens: 5 },
});
afterEach(cleanup);

describe("SetsPage", () => {
  it("sends you to the library when there's no set", async () => {
    sets.list.mockResolvedValue([]);
    render(<SetsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Go to the library" }));
    expect(navigate).toHaveBeenCalledWith({ name: "library" });
  });

  it("offers a bridge on a gap of the saved order, and shows the set with it in", async () => {
    const gapped = result(["a", "b"]);
    gapped.transitions[0] = { ...gapped.transitions[0], score: 40, gap: true };
    sets.order.mockResolvedValue(gapped);
    sets.bridge.mockResolvedValue({
      gap: { from: "a", to: "b", text: "", score: 40, cost: 3.6 },
      want: { bpm: 124, keys: ["9A"], energy: null, text: "a 124 BPM 9A track" },
      results: [{ hash: "c", title: "Song c", artist: "", bpm: 124, key: "9A", energy: null, rating: null, shift: 0, intro_s: null, cost: 1, score_in: 90, score_out: 90, text: "" }],
      candidates: 1,
      unanalysed: 0,
    });
    sets.insertBridge.mockResolvedValue(result(["a", "c", "b"]));
    render(<SetsPage />);
    expect(await screen.findByText("40")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Find a bridge" }));
    await userEvent.click(await screen.findByRole("button", { name: "Add here" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Add here" })).toBeNull());
    expect(screen.getAllByText("Song c").length).toBe(1);
    expect(screen.queryByRole("button", { name: "Find a bridge" })).toBeNull();
  });

  it("doesn't offer a bridge on an order that isn't saved", async () => {
    const gapped = result(["a", "b"]);
    gapped.transitions[0] = { ...gapped.transitions[0], score: 40, gap: true };
    sets.order.mockResolvedValue(gapped);
    sets.solveOrder.mockResolvedValue(gapped);
    render(<SetsPage />);
    await screen.findByRole("button", { name: "Find a bridge" });
    await userEvent.click(screen.getByRole("button", { name: "Order the set" }));
    await screen.findByRole("button", { name: "Use this order" });
    expect(screen.queryByRole("button", { name: "Find a bridge" })).toBeNull();
  });

  it("shows the running order with why each track follows", async () => {
    render(<SetsPage />);
    expect(await screen.findByText("Song a")).toBeTruthy();
    expect(screen.getByText("energy +1, 8A → 9A, step 0")).toBeTruthy();
  });

  it("proposes an order without saving, and saves it when asked", async () => {
    sets.solveOrder.mockResolvedValue(result(["c", "a", "b"]));
    sets.applyOrder.mockResolvedValue(result(["c", "a", "b"]));
    render(<SetsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Order the set" }));
    expect(await screen.findByText("Proposed order")).toBeTruthy();
    expect(sets.applyOrder).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Use this order" }));
    await waitFor(() => expect(sets.applyOrder).toHaveBeenCalledWith(1, ["c", "a", "b"], { arc: null, pins: [], tempo_start: null, tempo_end: null }));
    expect(await screen.findByText("Running order")).toBeTruthy();
  });

  it("goes back to the saved order on request", async () => {
    sets.solveOrder.mockResolvedValue(result(["c", "a", "b"]));
    render(<SetsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Order the set" }));
    await userEvent.click(await screen.findByRole("button", { name: "Keep the saved order" }));
    expect(await screen.findByText("Running order")).toBeTruthy();
    expect(sets.applyOrder).not.toHaveBeenCalled();
  });

  it("sends the chosen arc, the tempo journey and the pins", async () => {
    sets.solveOrder.mockResolvedValue(result(["a", "c", "b"]));
    render(<SetsPage />);
    await userEvent.click(await screen.findByRole("tab", { name: "Warm-up" }));
    await userEvent.type(screen.getByLabelText("Tempo from"), "120");
    await userEvent.type(screen.getByLabelText("Tempo to"), "128");
    await userEvent.click(screen.getByRole("button", { name: "Pin Song a at 1" }));
    expect(screen.getByRole("button", { name: "Unpin Song a" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Order the set" }));
    await waitFor(() => expect(sets.solveOrder).toHaveBeenCalledWith(1, { arc: "warm_up", tempo_start: 120, tempo_end: 128, pins: [{ position: 0, hash: "a" }] }));
  });

  it("sends a curve drawn on the timeline", async () => {
    sets.solveOrder.mockResolvedValue(result(["a", "b", "c"]));
    render(<SetsPage />);
    const line = await screen.findByTestId("set-timeline");
    await waitFor(() => expect(sets.timeline).toHaveBeenCalled());
    await screen.findByRole("img", { name: /3 tracks/ });
    line.getBoundingClientRect = () => box;
    await userEvent.click(screen.getByRole("button", { name: "Draw the arc" }));
    fireEvent.pointerDown(line, { pointerId: 1, clientX: 0, clientY: 200 });
    fireEvent.pointerMove(line, { pointerId: 1, clientX: 260, clientY: 50 });
    fireEvent.pointerUp(line, { pointerId: 1 });
    expect(screen.getByRole("tab", { name: "Custom" }).getAttribute("aria-selected")).toBe("true");
    await userEvent.click(screen.getByRole("button", { name: "Order the set" }));
    await waitFor(() => expect(sets.solveOrder).toHaveBeenCalled());
    const sent = sets.solveOrder.mock.calls[0][1].arc as [number, number][];
    expect(sent[0]).toEqual([0, 0]);
    expect(sent[sent.length - 1]).toEqual([1, 1]);
  });

  it("saves a track dragged on the timeline, and shows it moved at once", async () => {
    let finish: (r: SetOrderResult) => void = () => {};
    sets.applyOrder.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    render(<SetsPage />);
    const line = await screen.findByTestId("set-timeline");
    await waitFor(() => expect(sets.timeline).toHaveBeenCalled());
    line.getBoundingClientRect = () => box;
    await screen.findByText("Song a");
    // 260 px is 260 s of set: track a is 0-100 s on the first lane, c is at the end
    fireEvent.pointerDown(line, { pointerId: 1, clientX: 30, clientY: LANE_Y });
    fireEvent.pointerMove(line, { pointerId: 1, clientX: 255, clientY: LANE_Y });
    fireEvent.pointerUp(line, { pointerId: 1 });
    expect(sets.applyOrder).toHaveBeenCalledWith(1, ["b", "c", "a"], expect.objectContaining({ pins: [] }));
    await waitFor(() => expect(sets.timeline).toHaveBeenLastCalledWith(1, ["b", "c", "a"], false, expect.anything()));
    const rows = () => screen.getAllByText(/^Song /).map((e) => e.textContent);
    expect(rows()).toEqual(["Song b", "Song c", "Song a"]);
    finish(result(["b", "c", "a"]));
    await screen.findByText("energy +1, 8A → 9A, step 0");
  });

  it("goes back when saving the new order fails", async () => {
    sets.applyOrder.mockRejectedValue(new Error("no"));
    render(<SetsPage />);
    const line = await screen.findByTestId("set-timeline");
    await waitFor(() => expect(sets.timeline).toHaveBeenCalled());
    line.getBoundingClientRect = () => box;
    await screen.findByText("Song a");
    fireEvent.pointerDown(line, { pointerId: 1, clientX: 30, clientY: LANE_Y });
    fireEvent.pointerMove(line, { pointerId: 1, clientX: 255, clientY: LANE_Y });
    fireEvent.pointerUp(line, { pointerId: 1 });
    await waitFor(() => expect(screen.getAllByText(/^Song /).map((e) => e.textContent)).toEqual(["Song a", "Song b", "Song c"]));
  });

  it("opens the editor from a transition, and says when it's still planning", async () => {
    sets.timeline.mockResolvedValue(timeline(["a", "b", "c"], ["b/c"]));
    render(<SetsPage />);
    await screen.findByText("Song a");
    await waitFor(() => expect(sets.timeline).toHaveBeenCalled());
    await screen.findByText(/Planning 1 transition/);
    await userEvent.click(screen.getByRole("button", { name: "Edit the transition into Song c" }));
    expect(screen.queryByText("Editor b to c")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Edit the transition into Song b" }));
    expect(await screen.findByText("Editor a to b")).toBeTruthy();
    const asked = sets.timeline.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Close the transition" }));
    expect(screen.queryByText("Editor a to b")).toBeNull();
    expect(sets.timeline.mock.calls.length).toBe(asked + 1); // what the editor saved may have moved things
  });

  it("doesn't let you drag while an order is only proposed", async () => {
    sets.solveOrder.mockResolvedValue(result(["c", "a", "b"]));
    render(<SetsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Order the set" }));
    await screen.findByText("Proposed order");
    const line = screen.getByTestId("set-timeline");
    line.getBoundingClientRect = () => box;
    fireEvent.pointerDown(line, { pointerId: 1, clientX: 30, clientY: LANE_Y });
    fireEvent.pointerMove(line, { pointerId: 1, clientX: 255, clientY: LANE_Y });
    fireEvent.pointerUp(line, { pointerId: 1 });
    expect(sets.applyOrder).not.toHaveBeenCalled();
  });

  it("says what isn't analysed yet", async () => {
    sets.order.mockResolvedValue(result(["a", "b"], { unknown: { bpm: 1, key: 2, energy: 0, vibe: 0, vocals: 2, genre: 2 } }));
    render(<SetsPage />);
    expect(await screen.findByText(/Not analysed yet: key \(2\), tempo \(1\)/)).toBeTruthy();
  });

  it("comes back to the saved settings", async () => {
    sets.order.mockResolvedValue(result(["a", "b"], { settings: { arc: "warm_up", tempo_start: 120, tempo_end: 125, pins: [{ position: 1, hash: "b" }] } }));
    render(<SetsPage />);
    expect(((await screen.findByLabelText("Tempo from")) as HTMLInputElement).value).toBe("120");
    expect(screen.getByRole("tab", { name: "Warm-up" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("button", { name: "Unpin Song b" })).toBeTruthy();
  });

  it("flags a clash", async () => {
    const r = result(["a", "b"]);
    r.transitions[0].flags = ["key_clash"];
    sets.order.mockResolvedValue(r);
    render(<SetsPage />);
    expect(await screen.findByLabelText("Check this one")).toBeTruthy();
  });

  it("shows no assistant while it's off", async () => {
    render(<SetsPage />);
    await screen.findByText("Song a");
    expect(screen.queryByLabelText("Describe the set")).toBeNull();
    expect(screen.queryByText("Set assistant")).toBeNull();
  });

  it("sends you to Settings when it's on without a key", async () => {
    assistant.status.mockResolvedValue({ ...ON, has_key: false });
    render(<SetsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Open Settings" }));
    expect(navigate).toHaveBeenCalledWith({ name: "settings" });
  });

  it("makes a draft with new tracks into a set of its own, with its reasoning, and only when asked", async () => {
    assistant.status.mockResolvedValue(ON);
    assistant.run.mockResolvedValue(draft(["x", "y", "z"]));
    sets.create.mockResolvedValue({ id: 2, name: "Sunset", track_count: 3 });
    sets.applyOrder.mockResolvedValue(result(["x", "y", "z"]));
    render(<SetsPage />);
    await userEvent.type(await screen.findByLabelText("Describe the set"), "a sunset set");
    await userEvent.click(screen.getByRole("button", { name: "Build a draft" }));
    expect(await screen.findByText("Draft: Sunset")).toBeTruthy();
    expect(assistant.run).toHaveBeenCalledWith({ prompt: "a sunset set", set_id: undefined, crate_ids: [] }, { quiet: true });
    expect(screen.getByRole("complementary", { name: "The assistant's reasoning" }).textContent).toContain("Mellow first");
    expect(sets.create).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Order again" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Keep as a new set" }));
    await waitFor(() => expect(sets.create).toHaveBeenCalledWith("Sunset", ["x", "y", "z"]));
    await waitFor(() =>
      expect(sets.applyOrder).toHaveBeenCalledWith(2, ["x", "y", "z"], { arc: "sunset", tempo_start: 120, tempo_end: 124, pins: [{ position: 1, hash: "y" }], note: "Mellow first, peak at two thirds." }),
    );
  });

  it("offers a draft of the open set's own tracks as a new order", async () => {
    assistant.status.mockResolvedValue(ON);
    assistant.run.mockResolvedValue(draft(["c", "a", "b"]));
    render(<SetsPage />);
    await userEvent.type(await screen.findByLabelText("Describe the set"), "reorder it");
    await userEvent.click(screen.getByText("This set (Friday)"));
    await userEvent.click(screen.getByText("Deep"));
    await userEvent.click(screen.getByRole("button", { name: "Build a draft" }));
    expect(await screen.findByText("Proposed order")).toBeTruthy();
    expect(assistant.run).toHaveBeenCalledWith({ prompt: "reorder it", set_id: 1, crate_ids: [7] }, { quiet: true });
    expect(screen.getByRole("button", { name: "Use this order" })).toBeTruthy();
  });

  it("goes back to the set as it was when a draft is discarded", async () => {
    assistant.status.mockResolvedValue(ON);
    assistant.run.mockResolvedValue(draft(["x", "y"]));
    render(<SetsPage />);
    await userEvent.type(await screen.findByLabelText("Describe the set"), "a set");
    await userEvent.click(screen.getByRole("button", { name: "Build a draft" }));
    await userEvent.click(await screen.findByRole("button", { name: "Discard this draft" }));
    expect(await screen.findByText("Running order")).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "The assistant's reasoning" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Peak time" })).toBeTruthy();
    expect(((screen.getByLabelText("Tempo from")) as HTMLInputElement).value).toBe("");
  });

  it("says why there's no draft, in place", async () => {
    assistant.status.mockResolvedValue(ON);
    assistant.run.mockRejectedValue(new Error("Anthropic rejected the API key: check it in Settings"));
    render(<SetsPage />);
    await userEvent.type(await screen.findByLabelText("Describe the set"), "a set");
    await userEvent.click(screen.getByRole("button", { name: "Build a draft" }));
    expect((await screen.findByRole("alert")).textContent).toContain("rejected");
  });

  it("offers the assistant when there's no set yet", async () => {
    assistant.status.mockResolvedValue(ON);
    sets.list.mockResolvedValue([]);
    assistant.run.mockResolvedValue(draft(["x", "y"]));
    render(<SetsPage />);
    await userEvent.type(await screen.findByLabelText("Describe the set"), "a set");
    await userEvent.click(screen.getByRole("button", { name: "Build a draft" }));
    expect(await screen.findByText("Draft: Sunset")).toBeTruthy();
  });

  it("keeps a saved set's reasoning until it's dismissed", async () => {
    sets.order.mockResolvedValue(result(["a", "b"], { settings: { arc: null, tempo_start: null, tempo_end: null, pins: [], note: "Because." } }));
    sets.applyOrder.mockResolvedValue(result(["a", "b"]));
    render(<SetsPage />);
    expect((await screen.findByRole("complementary", { name: "The assistant's reasoning" })).textContent).toContain("Because.");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss the assistant's reasoning" }));
    expect(screen.queryByRole("complementary", { name: "The assistant's reasoning" })).toBeNull();
    await waitFor(() => expect(sets.applyOrder).toHaveBeenCalledWith(1, ["a", "b"], { arc: null, tempo_start: null, tempo_end: null, pins: [] }));
  });

  it("explains a transition in plain words when asked", async () => {
    const r = result(["a", "b"]);
    r.transitions[0].why = ["8A into 9A is next door on the Camelot wheel, so the two harmonise."];
    sets.order.mockResolvedValue(r);
    render(<SetsPage />);
    expect(await screen.findByText("Song a")).toBeTruthy();
    expect(screen.queryByText(/next door/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Why does Song b follow Song a?" }));
    expect(screen.getByText(/next door/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Why does Song b follow Song a?" }));
    expect(screen.queryByText(/next door/)).toBeNull();
  });
});
