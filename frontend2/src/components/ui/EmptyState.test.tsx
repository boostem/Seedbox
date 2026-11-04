// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Inbox } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Button } from "./Button";
import { EmptyState } from "./EmptyState";

afterEach(cleanup);

describe("EmptyState", () => {
  it("renders title, description and a working action", async () => {
    const user = userEvent.setup();
    const onScan = vi.fn();
    render(<EmptyState icon={Inbox} title="No tracks" description="Scan a folder to start." action={<Button onClick={onScan}>Scan</Button>} />);
    expect(screen.getByText("No tracks")).toBeTruthy();
    expect(screen.getByText("Scan a folder to start.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Scan" }));
    expect(onScan).toHaveBeenCalledTimes(1);
  });

  it("draws the waveform illustration, not a box of prose", () => {
    const { container } = render(<EmptyState title="No tracks" />);
    expect(container.querySelectorAll("svg rect").length).toBeGreaterThan(50);
    expect(container.querySelector("[role=alert]")).toBeNull();
  });

  it("announces an error", () => {
    render(<EmptyState tone="danger" title="Couldn't load" description="Try again." />);
    expect(screen.getByRole("alert").textContent).toContain("Couldn't load");
  });

  it("renders just a title", () => {
    const { container } = render(<EmptyState title="Nothing selected" compact />);
    expect(container.textContent).toBe("Nothing selected");
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.querySelector("svg")).toBeNull();
  });
});
