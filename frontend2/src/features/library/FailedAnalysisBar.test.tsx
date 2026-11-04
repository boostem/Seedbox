// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import type { LibraryRow } from "@/api/types";
import { FailedAnalysisBar, failedJobs } from "./FailedAnalysisBar";

const row = (hash: string, jobs: LibraryRow["status"]["jobs"]) => ({ hash, status: { jobs } }) as LibraryRow;
const ROWS = [row("a", { waveform: "failed", beats: "running" }), row("b", {}), row("c", { key: "failed" })];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("FailedAnalysisBar", () => {
  it("finds the failed jobs", () => {
    expect(failedJobs(ROWS)).toEqual([{ kind: "waveform", hash: "a" }, { kind: "key", hash: "c" }]);
  });

  it("isn't there when nothing failed", () => {
    const { container } = render(<FailedAnalysisBar rows={[row("b", {})]} />);
    expect(container.textContent).toBe("");
  });

  it("queues the failed jobs again", async () => {
    const submit = vi.spyOn(api.jobs, "submit").mockResolvedValue({});
    render(<FailedAnalysisBar rows={ROWS} />);
    expect(screen.getByRole("status").textContent).toContain("2 tracks couldn't be fully analysed");
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(submit.mock.calls.map((c) => [c[0], c[1]])).toEqual([["waveform", { hash: "a" }], ["key", { hash: "c" }]]);
  });
});
