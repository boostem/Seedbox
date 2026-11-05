// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JobRow, ServerEvent, SetExportStatus } from "@/api/types";

const sets = vi.hoisted(() => ({ exportStatus: vi.fn(), export: vi.fn() }));
const jobs = vi.hoisted(() => ({ cancel: vi.fn() }));
const reveal = vi.hoisted(() => vi.fn());
const toastError = vi.hoisted(() => vi.fn());
vi.mock("@/store/toasts", () => ({ toast: { error: toastError } }));
const listeners = vi.hoisted(() => new Set<(ev: ServerEvent) => void>());
vi.mock("@/api/client", () => ({ api: { sets, jobs, reveal } }));
vi.mock("@/api/events", () => ({
  onServerEvent: (fn: (ev: ServerEvent) => void) => {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
}));
const { ExportPanel } = await import("./ExportPanel");

const row = (over: Partial<JobRow> = {}): JobRow => ({
  id: "j1", kind: "set_export", state: "running", priority: 300, payload: {}, progress: 0, error: null, created_at: "", updated_at: "", ...over,
});
const status = (job: JobRow | null): SetExportStatus => ({
  formats: [{ id: "wav", label: "WAV" }, { id: "mp3", label: "MP3 320" }],
  folder: "/music/Seedbox Mixes",
  targets: { streaming: -14 },
  job,
});
const emit = (type: string, data: unknown) => act(() => listeners.forEach((fn) => fn({ type, data } as ServerEvent)));

beforeEach(() => {
  vi.clearAllMocks();
  listeners.clear();
});
afterEach(cleanup);

describe("ExportPanel", () => {
  it("starts the format chosen, and shows what the job says with the time left", async () => {
    sets.exportStatus.mockResolvedValue(status(null));
    sets.export.mockResolvedValue(row({ state: "queued" }));
    render(<ExportPanel setId={7} />);
    await userEvent.selectOptions(await screen.findByLabelText("Format"), "wav");
    await userEvent.click(screen.getByRole("button", { name: "Export" }));
    expect(sets.export).toHaveBeenCalledWith(7, "wav");
    expect(await screen.findByText("Waiting for its turn")).toBeTruthy();
    emit("job.progress", { id: "j1", kind: "set_export", progress: 0.4, message: "Rendering track 3 of 9 · about 4 min left" });
    expect(screen.getByText("Rendering track 3 of 9 · about 4 min left")).toBeTruthy();
    emit("job.progress", { id: "other", kind: "stems", progress: 0.9, message: "not this" });
    expect(screen.queryByText("not this")).toBeNull();
  });

  it("picks up a job that was already running, and cancels it", async () => {
    sets.exportStatus.mockResolvedValue(status(row({ progress: 0.5 })));
    jobs.cancel.mockResolvedValue(row({ state: "cancelled" }));
    render(<ExportPanel setId={7} />);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(jobs.cancel).toHaveBeenCalledWith("j1");
    await waitFor(() => expect(screen.getByRole("button", { name: "Export" })).toBeTruthy());
  });

  it("says where the mix went and what it measures", async () => {
    sets.exportStatus.mockResolvedValueOnce(status(row()));
    render(<ExportPanel setId={7} />);
    await screen.findByRole("button", { name: "Cancel" });
    const result = { path: "/music/Seedbox Mixes/Set.mp3", tracklist: "", cue: "", format: "mp3", duration_s: 3725, target: "streaming", measured: { integrated_lufs: -14.02, lra_lu: 4.2, true_peak_dbtp: -1.61 } };
    sets.exportStatus.mockResolvedValue(status(row({ state: "done", progress: 1, result })));
    emit("job.done", { id: "j1", kind: "set_export" });
    expect(await screen.findByText("/music/Seedbox Mixes/Set.mp3")).toBeTruthy();
    expect(screen.getByText("-14.0 LUFS")).toBeTruthy();
    expect(screen.getByText("1:02:05")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Show the file" }));
    expect(reveal).toHaveBeenCalledWith(result.path);
  });

  it("says why it failed, and that trying again won't start over", async () => {
    sets.exportStatus.mockResolvedValue(status(row({ state: "failed", error: "not enough disk space" })));
    render(<ExportPanel setId={7} />);
    expect((await screen.findByRole("alert")).textContent).toContain("not enough disk space");
    expect(screen.getByRole("alert").textContent).toContain("last finished track");
  });

  it("shows a refusal, such as a transition with no plan yet", async () => {
    sets.exportStatus.mockResolvedValue(status(null));
    sets.export.mockRejectedValue(new Error("2 transition(s) have no plan yet"));
    render(<ExportPanel setId={7} />);
    await userEvent.click(await screen.findByRole("button", { name: "Export" }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("2 transition(s) have no plan yet"));
    expect(screen.getByRole("button", { name: "Export" })).toBeTruthy();
  });
});
