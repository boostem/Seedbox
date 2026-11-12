// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LibraryBridges, OnlineBridges, ServerEvent } from "@/api/types";

const sets = vi.hoisted(() => ({ bridge: vi.fn(), bridgeOnline: vi.fn(), insertBridge: vi.fn() }));
const tunebat = vi.hoisted(() => ({ download: vi.fn(), search: vi.fn() }));
const listeners = vi.hoisted(() => new Set<(ev: ServerEvent) => void>());
vi.mock("@/api/client", () => ({ api: { sets, tunebat } }));
vi.mock("@/api/events", () => ({
  onServerEvent: (fn: (ev: ServerEvent) => void) => {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
}));
const { BridgePanel } = await import("./BridgePanel");

const GAP = { from: "a", to: "b", text: "8A → 2A", score: 40, cost: 3.6 };
const LIBRARY: LibraryBridges = {
  gap: GAP,
  want: { bpm: 124, keys: ["9A"], energy: [0.4, 0.6], text: "a 124 BPM 9A track with a long intro and a similar vibe" },
  results: [{ hash: "h1", title: "Fits", artist: "X", bpm: 124, key: "9A", energy: 0.5, rating: 5, shift: 0, intro_s: 40, cost: 1, score_in: 90, score_out: 85, text: "124 BPM, 8A → 9A → 10A, same vibe" }],
  candidates: 4,
  unanalysed: 2,
};
const ONLINE: OnlineBridges = {
  gap: GAP,
  seeds: [{ hash: "a", id: "s" }, { hash: "b", id: null }],
  results: [{ id: "t1", name: "Online", artists: ["Y"], bpm: 124, camelot: "9A", shift: 0, cover_url: null, from: ["from"], cost: 2, score_in: 80, score_out: 75, text: "124 BPM" }],
};

function emit(type: string, data: unknown) {
  act(() => listeners.forEach((fn) => fn({ type, data })));
}

beforeEach(() => {
  vi.resetAllMocks();
  listeners.clear();
  sets.bridge.mockResolvedValue(LIBRARY);
  sets.bridgeOnline.mockResolvedValue(ONLINE);
  tunebat.download.mockResolvedValue({ id: "t1", state: "queued", error: null });
});
afterEach(cleanup);

describe("BridgePanel", () => {
  it("says what to look for and what fits, and what waits for analysis", async () => {
    render(<BridgePanel setId={1} position={2} onInserted={vi.fn()} />);
    expect(await screen.findByText("Fits")).toBeTruthy();
    expect(sets.bridge).toHaveBeenCalledWith(1, 2, expect.anything());
    expect(screen.getByText(/Look for a 124 BPM 9A track/)).toBeTruthy();
    expect(screen.getByText("2 more tracks aren't ready to mix yet.")).toBeTruthy();
  });

  it("adds a track between the two neighbours", async () => {
    const result = { order: [] };
    sets.insertBridge.mockResolvedValue(result);
    const onInserted = vi.fn();
    render(<BridgePanel setId={1} position={2} onInserted={onInserted} />);
    await userEvent.click(await screen.findByRole("button", { name: "Add here" }));
    expect(sets.insertBridge).toHaveBeenCalledWith(1, { position: 2, hash: "h1", from: "a", to: "b" });
    await waitFor(() => expect(onInserted).toHaveBeenCalledWith(result));
  });

  it("only asks tunebat when told to, and says who it doesn't know", async () => {
    render(<BridgePanel setId={1} position={2} onInserted={vi.fn()} />);
    await screen.findByText("Fits");
    expect(sets.bridgeOnline).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Look on tunebat" }));
    expect(await screen.findByText("Online")).toBeTruthy();
    expect(screen.getByText("Tunebat doesn't know one of the two tracks.")).toBeTruthy();
  });

  it("shows when tunebat doesn't answer", async () => {
    sets.bridgeOnline.mockRejectedValue(new Error("Tunebat isn't answering."));
    render(<BridgePanel setId={1} position={2} onInserted={vi.fn()} />);
    await screen.findByText("Fits");
    await userEvent.click(screen.getByRole("button", { name: "Look on tunebat" }));
    expect(await screen.findByText("Tunebat isn't answering.")).toBeTruthy();
  });

  it("downloads a recommendation and looks again once it's analysed", async () => {
    render(<BridgePanel setId={1} position={2} onInserted={vi.fn()} />);
    await screen.findByText("Fits");
    await userEvent.click(screen.getByRole("button", { name: "Look on tunebat" }));
    await userEvent.click(await screen.findByRole("button", { name: "Add" }));
    expect(tunebat.download).toHaveBeenCalledWith("t1", expect.anything());
    expect(sets.bridge).toHaveBeenCalledTimes(1);

    emit("job.done", { kind: "key" }); // nothing downloaded yet: not ours
    expect(sets.bridge).toHaveBeenCalledTimes(1);

    emit("tunebat.download", { id: "t1", state: "done", error: null });
    expect(screen.getByText("In your library")).toBeTruthy();
    emit("job.done", { kind: "waveform" });
    expect(sets.bridge).toHaveBeenCalledTimes(1);
    emit("job.done", { kind: "key" });
    await waitFor(() => expect(sets.bridge).toHaveBeenCalledTimes(2));
  });
});
