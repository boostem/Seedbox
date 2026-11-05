import { describe, expect, it, vi } from "vitest";
import type { MixPlan } from "@/api/mixplan.generated";
import { PlanSession } from "./session";

const plan = (technique: string) => ({ technique }) as MixPlan;

describe("A/B on a running engine", () => {
  it("plays the plan as it came until there's an edit, and can't switch to an edit that isn't there", () => {
    const schedule = vi.fn();
    const auto = plan("auto");
    const s = new PlanSession({ schedule }, auto);
    s.setSide("edit");
    expect(s.side).toBe("auto");
    expect(s.plan).toBe(auto);
    expect(schedule).not.toHaveBeenCalled();
  });

  it("hears an edit at once, and each side is one schedule with no restart", () => {
    const schedule = vi.fn();
    const auto = plan("auto");
    const edit = plan("edit");
    const s = new PlanSession({ schedule }, auto);
    s.edit(edit);
    expect(schedule).toHaveBeenLastCalledWith(edit);
    s.setSide("auto");
    expect(schedule).toHaveBeenLastCalledWith(auto);
    s.setSide("auto");
    expect(schedule).toHaveBeenCalledTimes(2);
    s.setSide("edit");
    expect(schedule).toHaveBeenLastCalledWith(edit);
  });

  it("an edit made on A goes to B, built on what you were hearing", () => {
    const schedule = vi.fn();
    const s = new PlanSession({ schedule }, plan("auto"));
    s.edit(plan("first"));
    s.setSide("auto");
    const second = plan("second");
    s.edit(second);
    expect(s.side).toBe("edit");
    expect(s.plan).toBe(second);
  });

  it("tells listeners about coarse changes, not about every move of a drag", () => {
    const s = new PlanSession({ schedule: vi.fn() }, plan("auto"));
    const heard = vi.fn();
    s.subscribe(heard);
    s.edit(plan("1"));
    s.edit(plan("2"));
    s.edit(plan("3"));
    expect(heard).toHaveBeenCalledTimes(1);
  });

  it("discarding the edit goes back to the plan as it came", () => {
    const schedule = vi.fn();
    const auto = plan("auto");
    const s = new PlanSession({ schedule }, auto);
    s.edit(plan("edit"));
    s.reset();
    expect(s.side).toBe("auto");
    expect(s.edited).toBeNull();
    expect(schedule).toHaveBeenLastCalledWith(auto);
  });
});
