import { describe, expect, it } from "vitest";
import { createViewCtl } from "./hooks";

describe("view controller", () => {
  it("clamps zoom and scroll to the range and notifies only on change", () => {
    const ctl = createViewCtl(0, 100, 4);
    let calls = 0;
    const un = ctl.subscribe(() => calls++);
    expect(ctl.get()).toEqual({ start: 0, span: 100 });
    ctl.zoom(1000, 50);
    expect(ctl.get().span).toBe(4);
    expect(ctl.get().start).toBeCloseTo(49.95); // the span is clamped after anchoring, as before
    ctl.scroll(1000);
    expect(ctl.get()).toEqual({ start: 96, span: 4 });
    const before = calls;
    ctl.scroll(10); // already at the end
    expect(calls).toBe(before);
    ctl.reveal(10);
    expect(ctl.get().start).toBeCloseTo(10 - 0.6);
    ctl.fit();
    expect(ctl.get()).toEqual({ start: 0, span: 100 });
    un();
    ctl.zoom(2);
    expect(calls).toBe(before + 2);
  });

  it("starts from the initial view", () => {
    expect(createViewCtl(-10, 50, 2, { start: 5, span: 10 }).get()).toEqual({ start: 5, span: 10 });
  });
});
