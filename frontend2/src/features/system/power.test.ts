import { describe, expect, it } from "vitest";
import type { PowerStatus } from "@/api/types";
import { powerEffects, powerLabel, powerSource, powerWhy } from "./power";

const NORMAL: PowerStatus = {
  mode: "auto",
  low_power: false,
  reason: null,
  on_battery: false,
  battery_percent: 100,
  thermal: "nominal",
  limits: { stems_paused: false, analysis_paused: false, light_workers: 6, light_workers_full: 6, fps: null },
};
const BATTERY: PowerStatus = { ...NORMAL, low_power: true, reason: "battery", on_battery: true, battery_percent: 64, limits: { ...NORMAL.limits, stems_paused: true, light_workers: 3, fps: 30 } };
const CRITICAL: PowerStatus = { ...NORMAL, low_power: true, reason: "thermal", thermal: "critical", limits: { ...NORMAL.limits, stems_paused: true, analysis_paused: true, light_workers: 1, fps: 30 } };

describe("what low-power mode says", () => {
  it("says on battery, what is paused and until when", () => {
    expect(powerLabel(BATTERY)).toBe("On battery: stems paused");
    expect(powerWhy(BATTERY)).toBe("On battery, 64% left");
    const effects = powerEffects(BATTERY, 3, 0);
    expect(effects[0]).toContain("paused until you plug in (3 tracks waiting)");
    expect(effects).toContain("Analysis runs on 3 of 6 workers.");
    expect(effects).toContain("Waveforms draw at 30 fps.");
  });

  it("says when it's the heat, and that analysis is paused too", () => {
    expect(powerLabel(CRITICAL)).toBe("Running hot: analysis paused");
    expect(powerWhy(CRITICAL)).toBe("This computer is very hot");
    const effects = powerEffects(CRITICAL, 1, 12);
    expect(effects[0]).toContain("until it cools down (1 track waiting)");
    expect(effects[1]).toContain("(12 jobs waiting)");
  });

  it("says when it was switched on by hand", () => {
    const manual: PowerStatus = { ...BATTERY, reason: "manual", on_battery: false };
    expect(powerLabel(manual)).toBe("Low power: stems paused");
    expect(powerWhy(manual)).toBe("You turned it on");
    expect(powerEffects(manual, 0, 0)[0]).toContain("until you turn low-power mode off.");
  });

  it("says how the machine is doing", () => {
    expect(powerSource(NORMAL)).toBe("Plugged in, temperature normal");
    expect(powerSource({ ...BATTERY, thermal: "unknown" })).toBe("On battery, 64% left");
    expect(powerSource({ ...NORMAL, on_battery: null, battery_percent: null, thermal: "serious" })).toBe("No battery, running hot");
  });
});
