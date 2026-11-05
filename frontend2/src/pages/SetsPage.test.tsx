// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SetOrderResult } from "@/api/types";

const sets = vi.hoisted(() => ({ list: vi.fn(), arcs: vi.fn(), order: vi.fn(), solveOrder: vi.fn(), applyOrder: vi.fn() }));
const navigate = vi.hoisted(() => vi.fn());
vi.mock("@/api/client", () => ({ api: { sets } }));
vi.mock("@/lib/router", () => ({ navigate }));
const { SetsPage } = await import("./SetsPage");

const ARCS = [
  { name: "warm_up", label: "Warm-up", description: "", points: [[0, 0], [1, 1]] },
  { name: "peak_time", label: "Peak time", description: "", points: [[0, 0.5], [1, 1]] },
];

function result(hashes: string[], over: Partial<SetOrderResult> = {}): SetOrderResult {
  return {
    order: hashes.map((hash, position) => ({ position, hash, title: `Song ${hash}`, artist: "X", bpm: 124, key: "8A", energy: position / 9, rating: position + 1, shift: 0, pinned: false, target_energy: 0.5, target_bpm: 124 })),
    transitions: hashes.slice(1).map((to, i) => ({ from: hashes[i], to, text: `energy +1, 8A → 9A, step ${i}`, flags: [], cost: 1 })),
    cost: 5,
    cost_before: 9,
    arc: ARCS[1] as SetOrderResult["arc"],
    tempo: { start: 122, end: 126, auto: true },
    unknown: { bpm: 0, key: 0, energy: 0, vibe: 0, vocals: 0, genre: 0 },
    settings: { arc: null, tempo_start: null, tempo_end: null, pins: [] },
    ...over,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  sets.list.mockResolvedValue([{ id: 1, name: "Friday", track_count: 3 }]);
  sets.arcs.mockResolvedValue(ARCS);
  sets.order.mockResolvedValue(result(["a", "b", "c"]));
});
afterEach(cleanup);

describe("SetsPage", () => {
  it("sends you to the library when there's no set", async () => {
    sets.list.mockResolvedValue([]);
    render(<SetsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Go to the library" }));
    expect(navigate).toHaveBeenCalledWith({ name: "library" });
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

  it("sends a drawn curve", async () => {
    sets.solveOrder.mockResolvedValue(result(["a", "b", "c"]));
    render(<SetsPage />);
    const curve = await screen.findByRole("img", { name: /Energy arc/ });
    curve.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(curve, { pointerId: 1, clientX: 0, clientY: 100 });
    fireEvent.pointerMove(curve, { pointerId: 1, clientX: 100, clientY: 0 });
    fireEvent.pointerUp(curve, { pointerId: 1 });
    expect(screen.getByRole("tab", { name: "Custom" }).getAttribute("aria-selected")).toBe("true");
    await userEvent.click(screen.getByRole("button", { name: "Order the set" }));
    await waitFor(() => expect(sets.solveOrder).toHaveBeenCalled());
    const sent = sets.solveOrder.mock.calls[0][1].arc as [number, number][];
    expect(sent[0]).toEqual([0, 0]);
    expect(sent[sent.length - 1]).toEqual([1, 1]);
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
});
