// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Download, Grid } from "lucide-react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { comboLabel } from "@/lib/hotkeys";
import { Button, IconButton } from "./Button";

afterEach(cleanup);

describe("Button", () => {
  it("is a type=button with its text as name and fires onClick", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Button onClick={onClick} icon={Download}>Download</Button>);
    const btn = screen.getByRole("button", { name: "Download" });
    expect(btn.getAttribute("type")).toBe("button");
    await user.click(btn);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("fires from the keyboard", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Go" }));
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    expect(onClick).toHaveBeenCalledTimes(2);
  });

  it("does not fire when disabled or loading", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <>
        <Button onClick={onClick} disabled>Off</Button>
        <Button onClick={onClick} loading>Busy</Button>
      </>,
    );
    const off = screen.getByRole("button", { name: "Off" });
    const busy = screen.getByRole("button", { name: "Busy" });
    expect((off as HTMLButtonElement).disabled).toBe(true);
    expect((busy as HTMLButtonElement).disabled).toBe(true);
    await user.click(off);
    await user.click(busy);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("shows its shortcut hint and forwards ref and type", () => {
    const ref = createRef<HTMLButtonElement>();
    render(<Button ref={ref} type="submit" kbd="Mod+D">Save</Button>);
    expect(ref.current?.getAttribute("type")).toBe("submit");
    expect(screen.getByText(comboLabel("Mod+D")).tagName).toBe("KBD");
  });
});

describe("IconButton", () => {
  it("uses its label as accessible name and tooltip", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<IconButton icon={Grid} label="Grid view" onClick={onClick} />);
    const btn = screen.getByRole("button", { name: "Grid view" });
    await user.hover(btn);
    expect((await screen.findByRole("tooltip")).textContent).toBe("Grid view");
    await user.click(btn);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("uses a custom tooltip, or none", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<IconButton icon={Grid} label="Grid" tooltip="Grid view (G)" />);
    await user.hover(screen.getByRole("button", { name: "Grid" }));
    expect((await screen.findByRole("tooltip")).textContent).toBe("Grid view (G)");
    await user.unhover(screen.getByRole("button", { name: "Grid" }));
    expect(screen.queryByRole("tooltip")).toBeNull();
    rerender(<IconButton icon={Grid} label="Grid" tooltip={false} />);
    await user.hover(screen.getByRole("button", { name: "Grid" }));
    await new Promise((r) => setTimeout(r, 400));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("reflects active as aria-pressed", () => {
    const { rerender } = render(<IconButton icon={Grid} label="Grid" active />);
    expect(screen.getByRole("button", { name: "Grid", pressed: true })).toBeTruthy();
    rerender(<IconButton icon={Grid} label="Grid" active={false} />);
    expect(screen.getByRole("button", { name: "Grid", pressed: false })).toBeTruthy();
  });

  it("does not fire when disabled or loading", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <>
        <IconButton icon={Grid} label="A" disabled onClick={onClick} />
        <IconButton icon={Grid} label="B" loading onClick={onClick} />
      </>,
    );
    await user.click(screen.getByRole("button", { name: "A" }));
    await user.click(screen.getByRole("button", { name: "B" }));
    expect(onClick).not.toHaveBeenCalled();
  });
});
