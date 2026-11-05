// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import type { KeyStatus } from "@/api/types";
import { ui } from "@/store/ui";
import { KeyEditor } from "./KeyEditor";

const NAMES = { key: "A minor", camelot: "8A", open_key: "1m" };
const OURS: KeyStatus = {
  analysis: { ...NAMES, confidence: 0.72, best_guess: NAMES, source: "stems", beat_synchronous: true, tuning_cents: 0, changes: [] },
  effective: { ...NAMES, from: "analysis" },
  hints: [{ key: "3B", from: "tunebat" }],
};

// jsdom has no showModal/close
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute("open");
  };
});

afterEach(() => {
  cleanup();
  act(() => {
    ui.editKey(null);
    ui.setKeyNotation("camelot");
  });
  vi.restoreAllMocks();
});

describe("KeyEditor", () => {
  it("shows our key, how sure it is and the tag that disagrees", async () => {
    vi.spyOn(api.key, "status").mockResolvedValue(OURS);
    render(<KeyEditor />);
    act(() => ui.editKey("h"));
    await screen.findByText("72 %");
    expect(screen.getByText("Bass and other stems")).toBeTruthy();
    expect(screen.getByText("Tunebat").parentElement?.textContent).toContain("3B");
  });

  it("says when the analysis isn't sure", async () => {
    const analysis = { ...OURS.analysis!, key: null, camelot: null, open_key: null, confidence: 0.2 };
    vi.spyOn(api.key, "status").mockResolvedValue({ ...OURS, analysis, effective: { key: null, camelot: null, open_key: null, from: "unknown" } });
    render(<KeyEditor />);
    act(() => ui.editKey("h"));
    await screen.findByText("Not sure (A minor?)");
  });

  it("goes back to our analysis, and writes keys as Open Key on request", async () => {
    const mine: KeyStatus = { ...OURS, effective: { key: "Gb minor", camelot: "11A", open_key: "4m", from: "user" } };
    vi.spyOn(api.key, "status").mockResolvedValue(mine);
    const clear = vi.spyOn(api.key, "clearOverride").mockResolvedValue(OURS);
    vi.spyOn(api.library, "view").mockResolvedValue({ total: 0, items: [] });
    render(<KeyEditor />);
    act(() => ui.editKey("h"));
    fireEvent.click(await screen.findByRole("button", { name: "Use our analysis" }));
    await waitFor(() => expect(clear).toHaveBeenCalledWith("h"));
    fireEvent.click(screen.getByRole("switch"));
    await screen.findByText("8d"); // 3B
  });
});
