// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { demoNowPlaying } from "@/lib/demo/nowPlaying";
import { NowPlaying } from "./NowPlaying";

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the decks", () => {
  it("puts the outgoing track on A and the incoming one on B, with their keys and what they're doing", () => {
    render(<NowPlaying driver={demoNowPlaying()} />);
    const a = within(screen.getByRole("region", { name: "Deck A" }));
    const b = within(screen.getByRole("region", { name: "Deck B" }));
    expect(a.getByText("Night Shift")).toBeTruthy();
    expect(a.getByText("8A")).toBeTruthy();
    expect(a.getByText("128.0")).toBeTruthy();
    expect(a.getByText("Playing")).toBeTruthy();
    expect(b.getByText("Paper Tigers")).toBeTruthy();
    expect(b.getByText("9A")).toBeTruthy();
  });

  it("says what's mixing in, how, and for how long", () => {
    render(<NowPlaying driver={demoNowPlaying()} />);
    const strip = within(screen.getByRole("region", { name: "Up next" }));
    expect(strip.getByText("Mixing in")).toBeTruthy();
    expect(strip.getByText("Paper Tigers · Low Season")).toBeTruthy();
    expect(strip.getByText("Bass swap, 16 bars")).toBeTruthy();
    expect(strip.getByText("7.2")).toBeTruthy();
    expect(strip.getByText("bars left")).toBeTruthy();
  });

  it("plays and pauses from the button and the space bar", async () => {
    const user = userEvent.setup();
    const driver = demoNowPlaying();
    render(<NowPlaying driver={driver} />);
    await user.click(screen.getByRole("button", { name: "Play" }));
    expect(driver.status()).toBe("playing");
    await user.keyboard(" ");
    expect(driver.status()).toBe("paused");
    driver.close();
  });

  it("zooms both decks together", async () => {
    const user = userEvent.setup();
    render(<NowPlaying driver={demoNowPlaying()} />);
    expect(screen.getByText("16 bars")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Zoom in (=)" }));
    expect(screen.getByText("8 bars")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Zoom out (−)" }));
    await user.click(screen.getByRole("button", { name: "Zoom out (−)" }));
    expect(screen.getByText("32 bars")).toBeTruthy();
  });
});
