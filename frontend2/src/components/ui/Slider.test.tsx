// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Slider } from "./Slider";

afterEach(cleanup);

describe("Slider", () => {
  it("is a named range with min/max/step and the fill percentage", () => {
    render(<Slider value={25} min={0} max={50} step={5} onChange={() => {}} label="Tolerance" />);
    const s = screen.getByRole("slider", { name: "Tolerance" }) as HTMLInputElement;
    expect([s.min, s.max, s.step, s.value]).toEqual(["0", "50", "5", "25"]);
    expect(s.style.getPropertyValue("--sb-pct")).toBe("50%");
  });

  it("fires onChange with a number", () => {
    const onChange = vi.fn();
    render(<Slider value={0.5} onChange={onChange} label="Volume" />);
    fireEvent.change(screen.getByRole("slider", { name: "Volume" }), { target: { value: "0.8" } });
    expect(onChange).toHaveBeenCalledWith(0.8);
  });

  it("commits on key release and pointer up", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    function Vol() {
      const [v, setV] = useState(5);
      return <Slider value={v} min={0} max={10} step={1} onChange={setV} onCommit={onCommit} label="Volume" />;
    }
    render(<Vol />);
    const s = screen.getByRole("slider", { name: "Volume" });
    s.focus();
    await user.keyboard("{ArrowRight}");
    expect(onCommit).toHaveBeenLastCalledWith(Number((s as HTMLInputElement).value));
    fireEvent.pointerUp(s);
    expect(onCommit).toHaveBeenCalledTimes(2);
  });

  it("is disabled", () => {
    render(<Slider value={0} onChange={() => {}} disabled label="Off" />);
    expect((screen.getByRole("slider", { name: "Off" }) as HTMLInputElement).disabled).toBe(true);
  });
});
