// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useJobs } from "@/store/jobs";
import { AnalysisQueue, NOT_READY_HREF, queueLabel } from "./AnalysisQueue";

const job = (id: string, kind: string, hash: string | null, progress = 0) => ({ id, kind, hash, progress, startedAt: 0, seenStart: true });

beforeEach(() => {
  useJobs.setState({ running: {}, queued: {}, queuedHash: {} });
  window.location.hash = "#/sets";
});
afterEach(cleanup);

describe("queueLabel", () => {
  it("counts tracks, and says stems when they are all that's left", () => {
    expect(queueLabel(1, false, 2, 5)).toBe("Analysing 1 track");
    expect(queueLabel(24, false, 2, 100)).toBe("Analysing 24 tracks");
    expect(queueLabel(3, true, 1, 2)).toBe("Stems · 3 tracks");
    expect(queueLabel(0, false, 1, 0)).toBe("Working on 1"); // an export: about no track
  });
});

describe("AnalysisQueue", () => {
  it("shows the tracks analysing, counts down, hides when empty and opens the tracks that aren't ready", async () => {
    render(<AnalysisQueue />);
    expect(screen.queryByText(/Analysing/)).toBeNull();
    act(() => useJobs.setState({ running: { a: job("a", "beats", "h1") }, queued: { b: "key", c: "beats" }, queuedHash: { b: "h1", c: "h2" } }));
    expect(screen.getByText("Analysing 2 tracks")).toBeTruthy();
    act(() => useJobs.setState({ running: { s: job("s", "stems", "h1", 0.3) }, queued: {}, queuedHash: {} }));
    expect(screen.getByText("Stems · 1 track")).toBeTruthy();
    expect(screen.getByText("30%")).toBeTruthy();
    await userEvent.setup().click(screen.getByText("Stems · 1 track"));
    expect(window.location.hash).toBe(NOT_READY_HREF);
    act(() => useJobs.setState({ running: {} }));
    expect(screen.queryByText(/Stems/)).toBeNull();
  });
});
