// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ProgressBar } from "./ProgressBar";

afterEach(cleanup);

describe("ProgressBar", () => {
  it("exposes its value as a 0-100 percentage", () => {
    render(<ProgressBar value={0.426} label="Download" />);
    const bar = screen.getByRole("progressbar", { name: "Download" });
    expect(bar.getAttribute("aria-valuemin")).toBe("0");
    expect(bar.getAttribute("aria-valuemax")).toBe("100");
    expect(bar.getAttribute("aria-valuenow")).toBe("43");
  });

  it("clamps out-of-range values", () => {
    render(
      <>
        <ProgressBar value={1.7} label="over" />
        <ProgressBar value={-0.2} label="under" />
      </>,
    );
    expect(screen.getByRole("progressbar", { name: "over" }).getAttribute("aria-valuenow")).toBe("100");
    expect(screen.getByRole("progressbar", { name: "under" }).getAttribute("aria-valuenow")).toBe("0");
  });

  it("has no aria-valuenow when indeterminate", () => {
    render(<ProgressBar value={null} label="Scanning" />);
    expect(screen.getByRole("progressbar", { name: "Scanning" }).hasAttribute("aria-valuenow")).toBe(false);
  });
});
