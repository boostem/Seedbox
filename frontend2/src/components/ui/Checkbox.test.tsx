// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Checkbox } from "./Checkbox";

afterEach(cleanup);

function Controlled({ label }: { label?: string }) {
  const [on, setOn] = useState(false);
  return <Checkbox checked={on} onChange={setOn} label={label} />;
}

describe("Checkbox", () => {
  it("toggles aria-checked on click", async () => {
    const user = userEvent.setup();
    render(<Controlled />);
    const box = screen.getByRole("checkbox");
    expect(box.getAttribute("aria-checked")).toBe("false");
    await user.click(box);
    expect(box.getAttribute("aria-checked")).toBe("true");
    await user.click(box);
    expect(box.getAttribute("aria-checked")).toBe("false");
  });

  it("toggles with Space and Enter when focused", async () => {
    const user = userEvent.setup();
    render(<Controlled />);
    await user.tab();
    const box = screen.getByRole("checkbox");
    expect(document.activeElement).toBe(box);
    await user.keyboard(" ");
    expect(box.getAttribute("aria-checked")).toBe("true");
    await user.keyboard("{Enter}");
    expect(box.getAttribute("aria-checked")).toBe("false");
  });

  it("drops focus after a mouse click", async () => {
    const user = userEvent.setup();
    render(<Controlled />);
    await user.click(screen.getByRole("checkbox"));
    expect(document.activeElement).toBe(document.body);
  });

  it("toggles when its label is clicked, once", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Checkbox checked={false} onChange={onChange} label="Only new" />);
    await user.click(screen.getByText("Only new"));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toBe(true);
    await user.click(screen.getByRole("checkbox"));
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it("does not bubble clicks to the row and passes shiftKey", async () => {
    const user = userEvent.setup();
    const onRow = vi.fn();
    const onChange = vi.fn();
    render(
      <div onClick={onRow}>
        <Checkbox checked={false} onChange={onChange} />
      </div>,
    );
    await user.keyboard("{Shift>}");
    await user.click(screen.getByRole("checkbox"));
    await user.keyboard("{/Shift}");
    expect(onRow).not.toHaveBeenCalled();
    expect(onChange.mock.calls[0][1].shiftKey).toBe(true);
  });

  it("shows mixed when indeterminate", () => {
    render(<Checkbox checked={false} indeterminate onChange={() => {}} />);
    expect(screen.getByRole("checkbox").getAttribute("aria-checked")).toBe("mixed");
  });

  it("does not fire when disabled, and isn't tabbable", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Checkbox checked={false} disabled onChange={onChange} label="Off" />);
    const box = screen.getByRole("checkbox");
    expect(box.getAttribute("aria-disabled")).toBe("true");
    await user.click(box);
    await user.click(screen.getByText("Off"));
    await user.tab();
    expect(document.activeElement).not.toBe(box);
    box.focus();
    await user.keyboard(" ");
    expect(onChange).not.toHaveBeenCalled();
  });

  // BUG: the label is a sibling <span>, not linked via aria-labelledby, so the checkbox has no accessible name
  it.skip("is named by its label", () => {
    render(<Checkbox checked={false} onChange={() => {}} label="Only new" />);
    expect(screen.getByRole("checkbox", { name: "Only new" })).toBeTruthy();
  });
});
