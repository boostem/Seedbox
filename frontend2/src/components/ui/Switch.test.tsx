// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Switch } from "./Switch";

afterEach(cleanup);

function Controlled({ label }: { label?: string }) {
  const [on, setOn] = useState(false);
  return <Switch checked={on} onChange={setOn} label={label} />;
}

describe("Switch", () => {
  it("toggles aria-checked on click", async () => {
    const user = userEvent.setup();
    render(<Controlled label="Auto-tag" />);
    const sw = screen.getByRole("switch", { name: "Auto-tag" });
    expect(sw.getAttribute("aria-checked")).toBe("false");
    await user.click(sw);
    expect(sw.getAttribute("aria-checked")).toBe("true");
    await user.click(sw);
    expect(sw.getAttribute("aria-checked")).toBe("false");
  });

  it("toggles when its label is clicked", async () => {
    const user = userEvent.setup();
    render(<Controlled label="Auto-tag" />);
    await user.click(screen.getByText("Auto-tag"));
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
  });

  it("toggles from the keyboard", async () => {
    const user = userEvent.setup();
    render(<Controlled label="Auto-tag" />);
    await user.tab();
    await user.keyboard(" ");
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("false");
  });

  it("does not fire when disabled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Switch checked onChange={onChange} disabled label="Locked" />);
    await user.click(screen.getByRole("switch", { name: "Locked" }));
    await user.click(screen.getByText("Locked"));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("uses a given id", () => {
    render(<Switch id="sw" checked={false} onChange={() => {}} label="X" />);
    expect(screen.getByRole("switch", { name: "X" }).id).toBe("sw");
  });
});
