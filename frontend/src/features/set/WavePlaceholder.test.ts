import { describe, expect, it } from "vitest";
import type { AnalysisView } from "./useAnalysis";
import { mixPlaceholderView } from "./WavePlaceholder";

const ready = { status: "ready", analysis: {}, stale: false } as unknown as AnalysisView;
const running: AnalysisView = { status: "pending", state: "running" };
const failed: AnalysisView = { status: "error", message: "boom" };

describe("mixPlaceholderView", () => {
  it("shows the analysis still missing for a ready transition", () => {
    expect(mixPlaceholderView(true, ready, running)).toBe(running);
    expect(mixPlaceholderView(true, failed, ready)).toBe(failed);
  });
  it("prefers a failure, then a running analysis, else queued", () => {
    expect(mixPlaceholderView(false, running, failed)).toEqual({ status: "error", message: "No mix points: an analysis failed for one of these tracks." });
    expect(mixPlaceholderView(false, { status: "loading" }, running)).toBe(running);
    expect(mixPlaceholderView(false, { status: "loading" }, { status: "loading" })).toEqual({ status: "pending", state: "queued" });
  });
});
