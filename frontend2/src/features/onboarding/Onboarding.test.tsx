// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/api/client";
import type { ModelInfo, ModelsStatus, TunebatTrack } from "@/api/types";
import { useModels } from "@/store/models";
import { Onboarding } from "./Onboarding";
import { TunebatTry } from "./TunebatTry";

const model = (over: Partial<ModelInfo>): ModelInfo =>
  ({ name: "m", title: "Beat This!", version: "1", size: 1_000_000, licence: "MIT", attribution: "x", homepage: "https://x", used_for: "Beats", fallback: "heuristics", state: "missing", installed: false, done: 0, error: null, ...over }) as ModelInfo;
const status = (m: ModelInfo) => ({ dir: "/m", models: [m], missing: m.installed ? [] : [m], offline: false, total_bytes: m.size, installed_bytes: 0, settings: { accelerator: false, auto_download: true } }) as unknown as ModelsStatus;

const track: TunebatTrack = { id: "t", name: "Song", artists: ["Artist"], album: "A", duration: 200, bpm: 124, camelot: "8A", cover_url: null };

beforeEach(() => {
  vi.spyOn(api.library, "folders").mockResolvedValue({ folders: [{ path: "/music", exists: true }], scanning: null } as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useModels.setState({ status: null });
});

describe("Onboarding", () => {
  it("walks from the music to the models to what analysis does", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.models, "status").mockResolvedValue(status(model({})));
    const all = vi.spyOn(api.models, "downloadMissing").mockResolvedValue(status(model({ state: "downloading" })));
    render(<Onboarding />);
    expect(screen.getByRole("heading", { name: "Where is your music?" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await screen.findByText("Beat This!");
    await user.click(screen.getByRole("button", { name: /Download 1(\.0)? MB/ }));
    expect(all).toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: "Continue while they download" }));

    expect(screen.getByRole("heading", { name: "What analysis does" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open my library" })).toBeTruthy();
  });

  it("offers Retry and the fix for a failed download, and lets you skip", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.models, "status").mockResolvedValue(status(model({ state: "failed", error: "connection lost" })));
    const retry = vi.spyOn(api.models, "download").mockResolvedValue(model({ state: "downloading" }));
    render(<Onboarding />);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect((await screen.findByRole("alert")).textContent).toContain("connection lost. Check the connection");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledWith("m");
    await user.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(screen.getByRole("heading", { name: "What analysis does" })).toBeTruthy();
  });
});

describe("TunebatTry", () => {
  it("adds a track from a search", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.tunebat, "search").mockResolvedValue({ total: 1, items: [track] });
    const download = vi.spyOn(api.tunebat, "download").mockResolvedValue({ id: "t", state: "queued", error: null });
    render(<TunebatTry />);
    await user.type(screen.getByRole("textbox", { name: "Search tunebat" }), "song{Enter}");
    await screen.findByText("Song");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(download).toHaveBeenCalledWith("t", { quiet: true });
    expect(screen.getByText("Waiting…")).toBeTruthy();
  });

  it("says what went wrong with a search, and retries", async () => {
    const user = userEvent.setup();
    const search = vi.spyOn(api.tunebat, "search").mockRejectedValueOnce(new ApiError(502, "Tunebat isn't answering."));
    search.mockResolvedValueOnce({ total: 0, items: [] });
    render(<TunebatTry />);
    await user.type(screen.getByRole("textbox", { name: "Search tunebat" }), "song{Enter}");
    expect((await screen.findByRole("alert")).textContent).toContain("Tunebat isn't answering.");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Nothing found for “song”")).toBeTruthy();
  });

  it("offers Retry on a failed download", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.tunebat, "search").mockResolvedValue({ total: 1, items: [track] });
    vi.spyOn(api.tunebat, "download").mockRejectedValue(new ApiError(404, "search for the track first"));
    render(<TunebatTry />);
    await user.type(screen.getByRole("textbox", { name: "Search tunebat" }), "song{Enter}");
    await screen.findByText("Song");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect((await screen.findByRole("alert")).textContent).toContain("search for the track first");
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
});
