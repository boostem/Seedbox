// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseHash } from "@/lib/router";
import { RAIL } from "@/components/shell/nav";

const api = vi.hoisted(() => ({
  status: vi.fn(),
  next: vi.fn(),
  rate: vi.fn(),
  summary: vi.fn(),
  saveExport: vi.fn(),
  import: vi.fn(),
  audioUrl: (id: string, slot: string) => `/api/listen/trials/${id}/${slot}`,
}));
const play = vi.hoisted(() => vi.fn());

vi.mock("@/api/client", () => ({
  api: { listen: api, reveal: vi.fn() },
  ApiError: class extends Error {
    get isNotFound() {
      return true;
    }
  },
  resolveMediaUrl: (u: string) => Promise.resolve(u),
}));
vi.mock("@/audio/player", async () => {
  const { create } = await import("zustand");
  return { usePlayer: create(() => ({ currentId: null, status: "idle" })), player: { play, stop: vi.fn(), playPause: vi.fn(), time: () => 0 } };
});

const { ListenPage } = await import("./ListenPage");

const STATUS = { folder: "/data/listening", pairs: 2, renders: 4, comparisons: 2, ratings: 0, progress: { done: 0, total: 2 } };
const EMPTY_SUMMARY = { ratings: 0, raters: 0, versions: [], styles: [], matchups: [], slot_a: { wins: 0, trials: 0, win_rate: null, ci_low: 0, ci_high: 1 } };
const trial = (id: string) => ({ id, pair: "House into techno", progress: { done: 0, total: 2 } });

beforeEach(() => {
  localStorage.clear();
  window.location.hash = "#/listen";
  vi.clearAllMocks();
  api.status.mockResolvedValue(STATUS);
  api.next.mockReset().mockResolvedValueOnce(trial("t1")).mockResolvedValue(trial("t2"));
  api.rate.mockResolvedValue({ ok: true, ratings: 1 });
  api.summary.mockResolvedValue(EMPTY_SUMMARY);
});
afterEach(cleanup);

/** jsdom's File has no text(). */
function jsonFile(name: string, text: string): File {
  const f = new File([text], name, { type: "application/json" });
  Object.defineProperty(f, "text", { value: () => Promise.resolve(text) });
  return f;
}

const hear = (name: RegExp) => userEvent.click(screen.getByRole("button", { name }));

describe("#/listen", () => {
  it("is a route, and hidden from the rail", () => {
    expect(parseHash("#/listen")).toEqual({ name: "listen" });
    expect(RAIL.map((e) => e.route)).not.toContain("listen");
  });

  it("won't take a choice until both versions were heard, then saves what was picked and deals the next", async () => {
    render(<ListenPage />);
    await screen.findByText("House into techno");
    const a = screen.getByRole("button", { name: "A is better" });
    expect(a.hasAttribute("disabled")).toBe(true);

    await hear(/Version A/);
    expect(a.hasAttribute("disabled")).toBe(true);
    await hear(/Version B/);
    expect(play).toHaveBeenLastCalledWith("listen:t1:b", "/api/listen/trials/t1/b", expect.objectContaining({ subtitle: "Version B" }));
    expect(screen.getByRole("button", { name: "Save and next" }).hasAttribute("disabled")).toBe(true);

    await userEvent.click(screen.getByRole("button", { name: "B is better" }));
    await userEvent.click(within(screen.getByRole("radiogroup", { name: "Smoothness, version A" })).getByRole("radio", { name: "4" }));
    await userEvent.type(screen.getByPlaceholderText("What did you hear?"), "clean");
    await userEvent.type(screen.getByPlaceholderText("Anonymous"), "Sam");
    await userEvent.click(screen.getByRole("button", { name: "Save and next" }));

    await waitFor(() => expect(api.next).toHaveBeenCalledTimes(2));
    expect(api.rate).toHaveBeenCalledWith("t1", { choice: "b", scores: { a: { smoothness: 4 } }, comment: "clean", rater: "Sam" }, { quiet: true });
    expect(api.next).toHaveBeenLastCalledWith("Sam", { quiet: true }); // the name is used from then on
    // a fresh form for the next trial
    await waitFor(() => expect(screen.getByRole("button", { name: "B is better" }).getAttribute("aria-pressed")).toBe("false"));
    expect((screen.getByPlaceholderText("What did you hear?") as HTMLTextAreaElement).value).toBe("");
  });

  it("explains how to get renders when there is nothing to compare", async () => {
    api.next.mockReset();
    const { ApiError } = await import("@/api/client");
    api.next.mockRejectedValue(new ApiError(404, "none"));
    render(<ListenPage />);
    expect(await screen.findByText("Nothing to compare yet")).toBeTruthy();
    expect(screen.getByText("python -m seedbox2.eval listen")).toBeTruthy();
  });

  it("names engines only on the results tab, with the interval next to the rate", async () => {
    api.summary.mockResolvedValue({
      ...EMPTY_SUMMARY,
      ratings: 10,
      raters: 2,
      versions: [{ name: "seedbox2-2.0.0", wins: 9, trials: 10, win_rate: 0.9, ci_low: 0.6, ci_high: 0.98, scores: { dj: 4.25 } }],
      slot_a: { wins: 5, trials: 10, win_rate: 0.5, ci_low: 0.24, ci_high: 0.76 },
    });
    render(<ListenPage />);
    await screen.findByText("House into techno");
    expect(screen.queryByText("seedbox2-2.0.0")).toBeNull();
    expect(api.summary).not.toHaveBeenCalled();

    window.location.hash = "#/listen?tab=results";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    expect(await screen.findByText("seedbox2-2.0.0")).toBeTruthy();
    expect(screen.getByText("60%–98%")).toBeTruthy();
    expect(screen.getByText("9 of 10")).toBeTruthy();
    expect(screen.getByText("Better than a coin flip")).toBeTruthy();
    expect(screen.getByText("4.3")).toBeTruthy();
  });

  it("imports a file and rejects one that isn't JSON without calling the server", async () => {
    window.location.hash = "#/listen?tab=results";
    api.import.mockResolvedValue({ added: 2, duplicates: 1, ratings: 3 });
    render(<ListenPage />);
    const input = await screen.findByLabelText("Ratings file to import");

    await userEvent.upload(input, jsonFile("x.json", "not json"));
    await waitFor(() => expect(api.summary).toHaveBeenCalled());
    expect(api.import).not.toHaveBeenCalled();

    await userEvent.upload(input, jsonFile("y.json", JSON.stringify({ format: "f" })));
    await waitFor(() => expect(api.import).toHaveBeenCalledWith({ format: "f" }));
    await waitFor(() => expect(api.summary).toHaveBeenCalledTimes(2));
  });
});
