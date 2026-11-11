// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ServerEvent, TunebatCrate, TunebatCrateJob, TunebatRec, TunebatTrack } from "@/api/types";

const tunebat = vi.hoisted(() => ({ search: vi.fn(), crate: vi.fn(), downloadCrate: vi.fn(), cancelCrateDownload: vi.fn() }));
const listeners = vi.hoisted(() => new Set<(ev: ServerEvent) => void>());
vi.mock("@/api/client", () => ({ api: { tunebat }, ApiError: class extends Error {} }));
vi.mock("@/api/events", () => ({
  onServerEvent: (fn: (ev: ServerEvent) => void) => {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
}));
const { DiscoverPage, defaultTicks } = await import("./DiscoverPage");

const track = (id: string, name: string, camelot = "8A", bpm = 124): TunebatTrack & { key: string; energy: number } => ({
  id, name, artists: ["Artist"], album: "Album", duration: 200, bpm, camelot, cover_url: null, key: "A minor", energy: 0.8,
});
const rec = (id: string, name: string, related: boolean, in_library = false): TunebatRec => ({
  track: track(id, name), score: related ? 90 : 30, move: related ? "+1" : "clash", chips: [], related, reason: "", in_library,
});
const CRATE: TunebatCrate = {
  seed: { ...track("seed", "Seed Song"), in_library: false },
  folder: "Artist - Seed Song",
  tolerance: 0.1,
  recs: [rec("near", "Near", true), rec("owned", "Owned", true, true), rec("far", "Far", false)],
};

function emit(type: string, data: unknown) {
  act(() => listeners.forEach((fn) => fn({ type, data })));
}

beforeEach(() => {
  vi.resetAllMocks();
  listeners.clear();
  localStorage.clear();
  tunebat.search.mockResolvedValue({ total: 1, items: [track("seed", "Seed Song")] });
  tunebat.crate.mockResolvedValue(CRATE);
});
afterEach(cleanup);

const ticked = (name: string) => screen.getByTitle(`Tick ${name}`).getAttribute("aria-checked");

describe("defaultTicks", () => {
  it("ticks the seed and the related ones the library hasn't, or every one with tickAll", () => {
    expect([...defaultTicks(CRATE, false)].sort()).toEqual(["near", "seed"]);
    expect([...defaultTicks(CRATE, true)].sort()).toEqual(["far", "near", "seed"]);
    expect([...defaultTicks({ ...CRATE, seed: { ...CRATE.seed, in_library: true } }, false)]).toEqual(["near"]);
  });
});

describe("DiscoverPage", () => {
  async function openCrate() {
    const user = userEvent.setup();
    render(<DiscoverPage />);
    await user.type(screen.getByRole("textbox", { name: "Search tunebat" }), "seed{Enter}");
    await user.click(await screen.findByRole("button", { name: "Open" }));
    await screen.findByText("Artist - Seed Song");
    return user;
  }

  it("searches, opens a song with its recommendations and ticks the related ones", async () => {
    await openCrate();
    expect(tunebat.search).toHaveBeenCalledWith("seed", expect.anything());
    expect(tunebat.crate).toHaveBeenCalledWith("seed", expect.anything());
    expect(ticked("Seed Song")).toBe("true");
    expect(ticked("Near")).toBe("true");
    expect(ticked("Owned")).toBe("false");
    expect(ticked("Far")).toBe("false");
    expect(screen.getByRole("button", { name: /Download 2 tracks/ })).toBeTruthy();
  });

  it("ticks every recommendation when told to, and remembers it", async () => {
    const user = await openCrate();
    await user.click(screen.getByRole("switch"));
    expect(ticked("Far")).toBe("true");
    expect(localStorage.getItem("seedbox2.tunebat.tickAll")).toBe("1");
  });

  it("downloads the ticked tracks and follows the download", async () => {
    const user = await openCrate();
    await user.click(screen.getByTitle("Tick Far"));
    const job: TunebatCrateJob = {
      id: "j1", seed_id: "seed", seed: "Artist - Seed Song", folder: "Artist - Seed Song", state: "queued", error: null,
      items: ["seed", "near", "far"].map((id) => ({ id, name: id, artists: ["Artist"], text: "queued", status: null, reason: null })),
    };
    tunebat.downloadCrate.mockResolvedValue(job);
    await user.click(screen.getByRole("button", { name: /Download 3 tracks/ }));
    expect(tunebat.downloadCrate).toHaveBeenCalledWith("seed", ["seed", "near", "far"], expect.anything());
    emit("tunebat.crate", { ...job, state: "running", items: job.items.map((i) => (i.id === "far" ? { ...i, status: "skipped", text: "skipped" } : i)) });
    expect(screen.getByText("Downloading 1 of 3…")).toBeTruthy();
    expect(screen.getByText("Not found")).toBeTruthy();
    emit("tunebat.crate", { ...job, state: "done", items: job.items.map((i) => ({ ...i, status: i.id === "far" ? "skipped" : "downloaded" })) });
    expect(screen.getByText(/2 of 3 in the crate/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Crates" })).toBeTruthy();
  });

  it("says when tunebat doesn't answer", async () => {
    tunebat.crate.mockRejectedValue(new Error("Tunebat isn't answering."));
    const user = userEvent.setup();
    render(<DiscoverPage />);
    await user.type(screen.getByRole("textbox", { name: "Search tunebat" }), "seed{Enter}");
    await user.click(await screen.findByRole("button", { name: "Open" }));
    expect(await screen.findByText(/Tunebat isn't answering/)).toBeTruthy();
  });
});
