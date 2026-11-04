// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Star } from "lucide-react";
import { afterEach, describe, expect, it } from "vitest";
import { MOVE_HELP } from "@/lib/moves";
import { Badge, KeyBadge, MoveChip, ScoreChip, StatusDot } from "./Badge";

afterEach(cleanup);

describe("Badge", () => {
  it("renders its children and an icon, passing through attributes", () => {
    const { container } = render(<Badge icon={Star} title="new tracks" data-testid="b">NEW</Badge>);
    const badge = screen.getByTestId("b");
    expect(badge.textContent).toBe("NEW");
    expect(badge.getAttribute("title")).toBe("new tracks");
    expect(container.querySelector("svg")).not.toBeNull();
  });
});

describe("KeyBadge", () => {
  it("normalises the Camelot code", () => {
    render(<KeyBadge camelot=" 8a " />);
    expect(screen.getByText("8A")).toBeTruthy();
  });

  it("shows ? for a missing key", () => {
    render(<KeyBadge camelot={null} />);
    expect(screen.getByText("?")).toBeTruthy();
  });

  it("shows the key name as a tooltip on hover", async () => {
    const user = userEvent.setup();
    render(<KeyBadge camelot="8A" keyName="A minor" />);
    expect(screen.queryByRole("tooltip")).toBeNull();
    await user.hover(screen.getByText("8A"));
    expect((await screen.findByRole("tooltip")).textContent).toBe("A minor · 8A");
  });
});

describe("ScoreChip", () => {
  it("shows the rounded score", () => {
    render(<ScoreChip value={87.6} move="+1" />);
    expect(screen.getByText("88")).toBeTruthy();
  });

  it("renders a failed score too", () => {
    render(<ScoreChip value={12.2} passed={false} />);
    expect(screen.getByText("12")).toBeTruthy();
  });
});

describe("MoveChip", () => {
  it("shows the move label and its help on hover", async () => {
    const user = userEvent.setup();
    render(<MoveChip move="relative" />);
    await user.hover(screen.getByText("Relative"));
    expect((await screen.findByRole("tooltip")).textContent).toBe(MOVE_HELP.relative);
  });

  it("has no tooltip with showHelp={false}", async () => {
    const user = userEvent.setup();
    render(<MoveChip move="-1" showHelp={false} />);
    await user.hover(screen.getByText("−1"));
    await new Promise((r) => setTimeout(r, 400));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});

describe("StatusDot", () => {
  it("renders its label", () => {
    render(<StatusDot tone="ok" pulse label="Connected" />);
    expect(screen.getByText("Connected")).toBeTruthy();
  });
});
