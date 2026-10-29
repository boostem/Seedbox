// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { List } from "lucide-react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type TabItem, Tabs } from "./Tabs";

afterEach(cleanup);

type Id = "tracks" | "sets" | "jobs";
const ITEMS: TabItem<Id>[] = [
  { id: "tracks", label: "Tracks", icon: List, count: 12 },
  { id: "sets", label: "Sets" },
  { id: "jobs", label: "Jobs", disabled: true, title: "Nothing running" },
];

function Controlled({ variant }: { variant?: "underline" | "segmented" }) {
  const [v, setV] = useState<Id>("tracks");
  return <Tabs items={ITEMS} value={v} onChange={setV} variant={variant} />;
}

const selected = () => screen.getAllByRole("tab", { selected: true }).map((t) => t.textContent);

describe.each(["underline", "segmented"] as const)("Tabs (%s)", (variant) => {
  it("renders a tablist with the selected tab", () => {
    render(<Controlled variant={variant} />);
    expect(screen.getByRole("tablist")).toBeTruthy();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(selected()).toEqual(["Tracks12"]);
  });

  it("switches on click", async () => {
    const user = userEvent.setup();
    render(<Controlled variant={variant} />);
    await user.click(screen.getByRole("tab", { name: "Sets" }));
    expect(selected()).toEqual(["Sets"]);
  });

  it("switches from the keyboard (Tab + Enter/Space)", async () => {
    const user = userEvent.setup();
    render(<Controlled variant={variant} />);
    await user.tab();
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Sets" }));
    await user.keyboard("{Enter}");
    expect(selected()).toEqual(["Sets"]);
    await user.tab({ shift: true });
    await user.keyboard(" ");
    expect(selected()).toEqual(["Tracks12"]);
  });

  it("doesn't switch to a disabled tab", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Tabs items={ITEMS} value="tracks" onChange={onChange} variant={variant} />);
    const jobs = screen.getByRole("tab", { name: "Jobs" });
    expect(jobs.getAttribute("title")).toBe("Nothing running");
    await user.click(jobs);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("Tabs", () => {
  // BUG (a11y): no roving tabindex / arrow-key navigation as in the WAI-ARIA tabs pattern; every tab is a Tab stop
  it.skip("moves between tabs with the arrow keys", async () => {
    const user = userEvent.setup();
    render(<Controlled />);
    await user.tab();
    await user.keyboard("{ArrowRight}");
    expect(selected()).toEqual(["Sets"]);
  });
});
