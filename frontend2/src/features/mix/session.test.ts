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

describe("undo and redo", () => {
  it("takes a gesture back in one step, however many moves it scheduled", () => {
    const schedule = vi.fn();
    const auto = plan("auto");
    const s = new PlanSession({ schedule }, auto);
    s.begin();
    s.edit(plan("1"));
    s.edit(plan("2"));
    s.end();
    expect(s.canUndo).toBe(true);
    s.undo();
    expect(s.plan).toBe(auto);
    expect(s.side).toBe("auto");
    expect(s.canUndo).toBe(false);
    expect(schedule).toHaveBeenLastCalledWith(auto);
  });

  it("redoes what undo took back, until there's a new edit", () => {
    const s = new PlanSession({ schedule: vi.fn() }, plan("auto"));
    const first = plan("first");
    s.edit(first);
    s.undo();
    s.redo();
    expect(s.plan).toBe(first);
    s.undo();
    s.edit(plan("other"));
    expect(s.canRedo).toBe(false);
  });

  it("a gesture that changed nothing leaves no step and tells nobody", () => {
    const s = new PlanSession({ schedule: vi.fn() }, plan("auto"));
    const heard = vi.fn();
    s.subscribe(heard);
    s.begin();
    s.end();
    expect(s.canUndo).toBe(false);
    expect(heard).not.toHaveBeenCalled();
  });

  it("a drag is news only at its end; a slider's steps each are", () => {
    const s = new PlanSession({ schedule: vi.fn() }, plan("auto"));
    const heard = vi.fn();
    s.subscribe(heard);
    s.begin();
    s.edit(plan("1"));
    s.edit(plan("2"));
    expect(heard).not.toHaveBeenCalled();
    s.end();
    expect(heard).toHaveBeenCalledTimes(1);
    s.begin(true);
    s.edit(plan("3"));
    s.edit(plan("4"));
    expect(heard).toHaveBeenCalledTimes(3);
    s.end();
    s.undo();
    expect(s.plan.technique).toBe("2");
  });

  it("reset to auto can be undone", () => {
    const s = new PlanSession({ schedule: vi.fn() }, plan("auto"));
    const edit = plan("edit");
    s.edit(edit);
    s.reset();
    expect(s.edited).toBeNull();
    s.undo();
    expect(s.plan).toBe(edit);
  });

  it("opens on a saved edit, with the plan it started from on the other side", () => {
    const auto = plan("auto");
    const saved = plan("saved");
    const s = new PlanSession({ schedule: vi.fn() }, auto, saved);
    expect(s.plan).toBe(saved);
    s.reset();
    expect(s.plan).toBe(auto);
  });
});
