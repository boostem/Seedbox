import { describe, expect, it } from "vitest";
import { useWaveSettings, WAVE_MODES, waveSettings } from "./settings";

describe("waveform settings", () => {
  it("offers RGB, 3-band and stacked, 3-band first", () => {
    expect(WAVE_MODES.map((m) => m.id)).toEqual(["bands", "rgb", "stacked"]);
    expect(useWaveSettings.getState().mode).toBe("bands");
  });

  it("switches the mode and the renderer", () => {
    waveSettings.setMode("rgb");
    expect(useWaveSettings.getState().mode).toBe("rgb");
    waveSettings.setGpu(false);
    expect(useWaveSettings.getState().gpu).toBe(false);
    waveSettings.setMode("bands");
    waveSettings.setGpu(true);
  });
});
