// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { comboLabel } from "@/lib/hotkeys";
import { Kbd } from "./Kbd";

afterEach(cleanup);

describe("Kbd", () => {
  it("renders its keys", () => {
    render(<Kbd>K</Kbd>);
    expect(screen.getByText("K").tagName).toBe("KBD");
  });

  it("renders a combo with the platform's modifier labels", () => {
    render(<Kbd combo="Mod+Enter" />);
    expect(screen.getByText(comboLabel("Mod+Enter"))).toBeTruthy();
    expect(comboLabel("Mod+Enter").endsWith("↵")).toBe(true);
  });

  it("prefers combo over children", () => {
    render(<Kbd combo="Shift+a">ignored</Kbd>);
    expect(screen.queryByText("ignored")).toBeNull();
    expect(screen.getByText(comboLabel("Shift+a"))).toBeTruthy();
  });
});
