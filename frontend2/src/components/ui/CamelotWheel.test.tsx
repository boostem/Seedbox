// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CamelotWheel } from "./CamelotWheel";

afterEach(cleanup);

/** The segment <path> for a key, found by its <title>. */
function segment(container: HTMLElement, code: string) {
  const title = [...container.querySelectorAll("path > title")].find((t) => t.textContent?.split(" ")[0] === code);
  return title!.parentElement as unknown as SVGPathElement;
}

describe("CamelotWheel", () => {
  it("is a labelled image with all 24 keys", () => {
    const { container } = render(<CamelotWheel />);
    expect(screen.getByRole("img", { name: "Camelot wheel" })).toBeTruthy();
    expect(container.querySelectorAll("path > title")).toHaveLength(24);
    for (const code of ["1A", "12B", "8A"]) expect(screen.getByText(code, { selector: "text" })).toBeTruthy();
  });

  it("calls onSelect with the clicked key", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const { container } = render(<CamelotWheel onSelect={onSelect} />);
    await user.click(segment(container, "5B"));
    expect(onSelect).toHaveBeenCalledWith("5B");
    await user.click(screen.getByText("11A", { selector: "text" }));
    expect(onSelect).toHaveBeenLastCalledWith("11A");
  });

  it("titles segments with track counts", () => {
    const { container } = render(<CamelotWheel counts={{ "8A": 3, "9A": 1 }} />);
    expect(segment(container, "8A").textContent).toBe("8A · 3 tracks");
    expect(segment(container, "9A").textContent).toBe("9A · 1 track");
    expect(segment(container, "1B").textContent).toBe("1B · 0 tracks");
    expect(screen.getByText("3", { selector: "text" })).toBeTruthy(); // count label for keys with > 1 track
  });

  it("shows the active key in the centre, or a custom centre", () => {
    const { container, rerender } = render(<CamelotWheel active="8a" />);
    expect(container.querySelector("svg > text")?.textContent).toBe("8A");
    rerender(<CamelotWheel active="8A" center="42" />);
    expect(container.querySelector("svg > text")?.textContent).toBe("42");
  });

  it("draws a path through the set's keys, skipping unknown ones", () => {
    const { container } = render(<CamelotWheel path={["8A", "??", "9A", "9B"]} />);
    expect(container.querySelector("polyline")?.getAttribute("points")?.split(" ")).toHaveLength(3);
    expect(container.querySelectorAll("polyline ~ circle")).toHaveLength(3);
  });
});
