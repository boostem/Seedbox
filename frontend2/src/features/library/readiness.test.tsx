// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import type { LibraryRow } from "@/api/types";
import { useJobs } from "@/store/jobs";
import { unusableRows, useLibrary } from "@/store/library";
import { ReadinessBadge } from "./ReadinessBadge";
import { liveJobs, readiness, refusedMessage } from "./readiness";

let n = 0;
function row(p: Partial<LibraryRow> & { state?: LibraryRow["status"]["state"] } = {}): LibraryRow {
  const { state = "done", ...rest } = p;
  n += 1;
  const base = { hash: `h${n}`, duration: 200, title: `Track ${n}`, artist: "Artist", album: "", source: "local", added_at: "2026-11-01T00:00:00Z", genre: "", text: "", artwork: false, format: "mp3" };
  const values = { bpm: null, bpm_from: null, key: null, key_from: null, key_hint: null, key_hint_from: null, energy: null, energy_from: null };
  const status = { state, ready: state === "done", missing: state === "done" ? [] : ["beats"], use_anyway: false, usable: state === "done", waveform: state === "done", stems: false as const, stems_progress: null, analyses: [], jobs: {} };
  return { ...base, ...values, status, ...rest };
}

const status = (r: LibraryRow, s: Partial<LibraryRow["status"]>): LibraryRow => ({ ...r, status: { ...r.status, ...s } });
const failed = () => status(row({ state: "failed", title: "Broken" }), { missing: ["beats", "stems"], jobs: { beats: "failed" } });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useJobs.setState({ running: {}, queued: {}, queuedHash: {} });
  useLibrary.setState({ rows: null });
});

describe("readiness", () => {
  it("says nothing of a ready track", () => {
    expect(readiness(row())).toEqual({ usable: true, label: null, tone: null, reason: null, failed: false });
  });

  it("names each state, with what is missing", () => {
    expect(readiness(status(row({ state: "queued" }), { missing: ["key", "stems"] }))).toMatchObject({ usable: false, label: "Waiting to be analysed", reason: expect.stringContaining("Missing: key, stems.") });
    expect(readiness(row({ state: "analysing" }))).toMatchObject({ usable: false, label: "Analysing" });
    expect(readiness(status(row({ state: "analysing" }), { stems: "running", stems_progress: 0.4 })).label).toBe("Analysing (stems 40%)");
    expect(readiness(failed())).toMatchObject({ usable: false, label: "Analysis failed – retry", failed: true });
    expect(readiness(row({ state: "new" })).label).toBe("Not analysed yet");
  });

  it("follows the job queue before the row is read again", () => {
    const r = row({ state: "new" });
    const jobs = { running: { j1: { id: "j1", kind: "stems", hash: r.hash, progress: 0.42, startedAt: 0, seenStart: true } }, queuedHash: {} };
    expect(liveJobs(jobs, r.hash)).toBe("stems:42");
    expect(readiness(r, liveJobs(jobs, r.hash)).label).toBe("Analysing (stems 42%)");
    expect(readiness(r, liveJobs({ running: {}, queuedHash: { q: r.hash } }, r.hash)).label).toBe("Waiting to be analysed");
  });

  it("uses a failed track anyway, until it is analysed again", () => {
    const anyway = status(failed(), { use_anyway: true, usable: true });
    expect(readiness(anyway)).toMatchObject({ usable: true, label: "Used anyway" });
    expect(readiness(anyway, "queued").usable).toBe(false);
  });

  it("explains a refusal", () => {
    expect(refusedMessage([failed()])).toBe("“Broken” isn't ready for sets and mixes (analysis failed). Missing: beat grid, stems.");
    expect(refusedMessage([row(), row(), row(), row()])).toMatch(/^4 tracks aren't ready .* and 1 more\./);
  });

  it("finds the tracks a set won't take", () => {
    const ok = row();
    const busy = row({ state: "analysing" });
    useLibrary.setState({ rows: [ok, busy] });
    expect(unusableRows([ok.hash, busy.hash, "unknown"])).toEqual([busy]);
  });
});

describe("ReadinessBadge", () => {
  it("isn't there for a ready track", () => {
    const { container } = render(<ReadinessBadge row={row()} />);
    expect(container.textContent).toBe("");
  });

  it("shows the stems' progress live, and goes once the track is ready", () => {
    const r = row({ state: "queued" });
    const { container, rerender } = render(<ReadinessBadge row={r} />);
    expect(container.textContent).toBe("Waiting to be analysed");
    act(() => useJobs.setState({ running: { j: { id: "j", kind: "stems", hash: r.hash, progress: 0.4, startedAt: 0, seenStart: true } } }));
    expect(container.textContent).toBe("Analysing (stems 40%)");
    act(() => useJobs.setState({ running: {} }));
    rerender(<ReadinessBadge row={status(r, { state: "done", ready: true, usable: true })} />);
    expect(container.textContent).toBe("");
  });

  it("retries a failed analysis or uses the track anyway", async () => {
    const r = failed();
    useLibrary.setState({ rows: [r] });
    const submit = vi.spyOn(api.jobs, "submit").mockResolvedValue({});
    const anyway = vi.spyOn(api.library, "useAnyway").mockResolvedValue(status(r, { use_anyway: true, usable: true }));
    render(<ReadinessBadge row={r} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Retry analysis/ }));
    expect(submit.mock.calls.map((c) => [c[0], c[1]])).toEqual([["beats", { hash: r.hash }], ["stems", { hash: r.hash }]]);
    await user.click(screen.getByRole("button", { name: "Use anyway" }));
    expect(anyway).toHaveBeenCalledWith(r.hash, true);
    expect(useLibrary.getState().rows?.[0].status.usable).toBe(true);
  });
});
