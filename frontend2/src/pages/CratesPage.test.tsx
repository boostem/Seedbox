// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CrateLink, LongSetResult, SetSourceRef } from "@/api/types";

const sets = vi.hoisted(() => ({ list: vi.fn(), arcs: vi.fn(), build: vi.fn(), bridges: vi.fn(), buildBridge: vi.fn(), explainBuild: vi.fn(), create: vi.fn(), applyOrder: vi.fn() }));
const library = vi.hoisted(() => ({ crates: vi.fn() }));
const navigate = vi.hoisted(() => vi.fn());
vi.mock("@/api/client", () => ({ api: { sets, library } }));
vi.mock("@/lib/router", () => ({ navigate }));
const { CratesPage, linksToShow } = await import("./CratesPage");

const ARCS = [{ name: "peak_time", label: "Peak time", description: "", points: [[0, 0.5], [1, 1]] }];

function built(hashes: string[], over: Partial<LongSetResult> = {}): LongSetResult {
  return {
    order: hashes.map((hash, position) => ({ position, hash, title: `Song ${hash}`, artist: "X", bpm: 124, key: "8A", energy: 0.5, rating: 5, shift: 0, pinned: false, target_energy: 0.5, target_bpm: 124, source: 0 })),
    transitions: hashes.slice(1).map((to, i) => ({ from: hashes[i], to, text: `step ${i}`, flags: [], cost: 1, score: i === 0 ? 40 : 90, gap: i === 0 })),
    cost: 5,
    cost_before: 9,
    arc: ARCS[0] as LongSetResult["arc"],
    tempo: null,
    unknown: { bpm: 0, key: 0, energy: 0, vibe: 0, vocals: 0, genre: 0 },
    settings: { arc: "peak_time", tempo_start: null, tempo_end: null, pins: [] },
    sources: [{ kind: "crate", id: 1, name: "Peak", tracks: 8, picked: 2 }],
    left_out: 6,
    moved: 1,
    ...over,
  };
}

function link(from_source: number, to_source: number, fit: CrateLink["fit"] = "good"): CrateLink {
  const t = { hash: "h", title: `T${from_source}`, artist: "", bpm: 124, key: "8A", energy: 0.5, rating: 5 };
  return { from_source, to_source, from: t, to: { ...t, title: `T${to_source}` }, cost: 1, text: "8A → 9A", flags: [], fit };
}

const BRIDGE = { hash: "s", title: "Bridge", artist: "Y", bpm: 125, key: "9A", energy: 0.5, rating: 5, shift: 0, intro_s: null, cost: 2, score_in: 80, score_out: 80, text: "in between" };
const found = (results: unknown[]) => ({ gap: {}, want: {}, results, candidates: results.length, unanalysed: 0 });

beforeEach(() => {
  vi.resetAllMocks();
  library.crates.mockResolvedValue([{ id: 1, name: "Peak", track_count: 8 }, { id: 2, name: "Warm", track_count: 6 }]);
  sets.list.mockResolvedValue([{ id: 5, name: "Friday", track_count: 3 }]);
  sets.arcs.mockResolvedValue(ARCS);
  sets.bridges.mockResolvedValue({ links: [] });
  sets.build.mockResolvedValue(built(["a", "b"]));
});
afterEach(cleanup);

async function tick(name: string) {
  await userEvent.click(await screen.findByText(name));
}

describe("CratesPage", () => {
  it("builds from what's ticked, in the order it was ticked", async () => {
    render(<CratesPage />);
    await tick("Warm");
    await tick("Peak");
    await userEvent.click(screen.getByRole("button", { name: "Build the set" }));
    await screen.findByText("Song a");
    expect(sets.build.mock.calls[0][0].sources).toEqual([{ kind: "crate", id: 2 }, { kind: "crate", id: 1 }]);
    expect(sets.build.mock.calls[0][0].keep_order).toBe(true);
    expect(screen.getByText(/6 left out for the length/)).toBeTruthy();
    expect(screen.getByText(/1 moved out of their crate/)).toBeTruthy();
  });

  it("can't build from nothing", async () => {
    render(<CratesPage />);
    await screen.findByText("Peak");
    expect((screen.getByRole("button", { name: "Build the set" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("won't build to a length it can't do, and sends one it can", async () => {
    render(<CratesPage />);
    await tick("Peak");
    const build = screen.getByRole("button", { name: "Build the set" }) as HTMLButtonElement;
    await userEvent.type(screen.getByLabelText("Longest"), "500");
    expect(build.disabled).toBe(true);
    await userEvent.clear(screen.getByLabelText("Longest"));
    await userEvent.type(screen.getByLabelText("Longest"), "40");
    await userEvent.click(build);
    await screen.findByText("Song a");
    expect(sets.build.mock.calls[0][0].max_tracks).toBe(40);
  });

  it("moves a crate earlier, which changes what's sent", async () => {
    render(<CratesPage />);
    await tick("Peak");
    await tick("Warm");
    await userEvent.click(screen.getByRole("button", { name: "Play Warm earlier" }));
    await userEvent.click(screen.getByRole("button", { name: "Build the set" }));
    await screen.findByText("Song a");
    expect(sets.build.mock.calls[0][0].sources[0]).toEqual({ kind: "crate", id: 2 });
  });

  it("offers the whole library, and asks for no links with one crate", async () => {
    render(<CratesPage />);
    await tick("Whole library");
    await tick("Peak");
    await userEvent.click(screen.getByRole("button", { name: "Build the set" }));
    await screen.findByText("Song a");
    expect(sets.build.mock.calls[0][0].sources[0]).toEqual({ kind: "library", id: null });
    expect(sets.bridges).not.toHaveBeenCalled();
  });

  it("shows how two crates link", async () => {
    sets.bridges.mockResolvedValue({ links: [link(0, 1)] });
    render(<CratesPage />);
    await tick("Peak");
    await tick("Warm");
    expect(await screen.findByText("Peak → Warm")).toBeTruthy();
    expect(screen.getByText("Mixes well")).toBeTruthy();
  });

  it("finds a bridge for a gap and puts it in", async () => {
    sets.buildBridge.mockResolvedValue(found([BRIDGE]));
    const after = built(["a", "s", "b"], { sources: undefined, moved: undefined, left_out: undefined });
    after.transitions = after.transitions.map((t) => ({ ...t, gap: false }));
    sets.explainBuild.mockResolvedValue(after);
    render(<CratesPage />);
    await tick("Peak");
    await userEvent.click(screen.getByRole("button", { name: "Build the set" }));
    await userEvent.click(await screen.findByRole("button", { name: "Find a bridge" }));
    expect(sets.buildBridge.mock.calls[0].slice(0, 2)).toEqual([["a", "b"], 0]);
    await userEvent.click(await screen.findByRole("button", { name: "Add here" }));
    await screen.findByText("Song s");
    expect(sets.explainBuild.mock.calls[0][0]).toEqual(["a", "s", "b"]);
    expect(screen.queryByRole("button", { name: "Find a bridge" })).toBeNull();
  });

  it("says so when nothing fills a gap", async () => {
    sets.buildBridge.mockResolvedValue(found([]));
    render(<CratesPage />);
    await tick("Peak");
    await userEvent.click(screen.getByRole("button", { name: "Build the set" }));
    await userEvent.click(await screen.findByRole("button", { name: "Find a bridge" }));
    expect(await screen.findByText(/Nothing in the library goes smoothly/)).toBeTruthy();
  });

  it("saves the set with its arc and goes to it", async () => {
    sets.create.mockResolvedValue({ id: 9, name: "Peak" });
    sets.applyOrder.mockResolvedValue({});
    render(<CratesPage />);
    await tick("Peak");
    await userEvent.click(screen.getByRole("button", { name: "Build the set" }));
    await userEvent.click(await screen.findByRole("button", { name: "Save as a set" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ name: "sets" }));
    expect(sets.create).toHaveBeenCalledWith("Peak", ["a", "b"]);
    expect(sets.applyOrder.mock.calls[0][0]).toBe(9);
    expect(sets.applyOrder.mock.calls[0][2].arc).toBe("peak_time");
  });

  it("says when the crates can't be loaded", async () => {
    library.crates.mockRejectedValue(new Error("database is locked"));
    render(<CratesPage />);
    expect(await screen.findByText("database is locked")).toBeTruthy();
  });
});

describe("linksToShow", () => {
  const crates: SetSourceRef[] = [{ kind: "crate", id: 1 }, { kind: "library", id: null }, { kind: "crate", id: 2 }, { kind: "crate", id: 3 }];

  it("puts each crate into the next first, stepping over the library", () => {
    const out = linksToShow([link(2, 3), link(3, 0), link(0, 2)], crates);
    expect(out.map((l) => [l.from_source, l.to_source])).toEqual([[0, 2], [2, 3], [3, 0]]);
  });

  it("only offers other links that mix well", () => {
    expect(linksToShow([link(3, 0, "poor")], crates)).toEqual([]);
    expect(linksToShow([link(0, 2, "poor")], crates)).toHaveLength(1); // the next one shows whatever it is
  });
});
