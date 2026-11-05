/**
 * How waveforms look, chosen in Settings and kept in localStorage: the mode, and whether to use the GPU (WebGL2) or
 * the Canvas 2D painter. Every waveform view selects `mode` and redraws when it changes.
 */
import { create } from "zustand";
import type { StemView } from "./stems";

export type WaveMode = "rgb" | "bands" | "stacked";

export const WAVE_MODES: { id: WaveMode; label: string; title: string }[] = [
  { id: "bands", label: "3-band", title: "Blue lows, amber mids and white highs, layered (Rekordbox 3-band)" },
  { id: "rgb", label: "RGB", title: "One waveform, each column coloured by its low/mid/high balance (Rekordbox RGB)" },
  { id: "stacked", label: "Stacked", title: "Each band in its own row: highs on top, lows at the bottom" },
];

export const STEM_VIEWS: { id: StemView; label: string; title: string }[] = [
  { id: "stacked", label: "Stacked", title: "One waveform split into four coloured layers" },
  { id: "lanes", label: "Lanes", title: "Each stem in its own thin lane" },
];

const MODE_KEY = "seedbox2.waveMode";
const STEM_VIEW_KEY = "seedbox2.stemView";
const GPU_KEY = "seedbox2.waveGpu";

function stored<T extends string>(key: string, ok: readonly T[], fallback: T): T {
  try {
    const v = localStorage.getItem(key) as T | null;
    return v && ok.includes(v) ? v : fallback;
  } catch {
    return fallback;
  }
}

function store(key: string, v: string): void {
  try {
    localStorage.setItem(key, v);
  } catch {
    // private mode / no storage: the choice lasts this session
  }
}

interface WaveSettings {
  mode: WaveMode;
  /** how stem waveforms draw: one stacked colour view or four lanes */
  stemView: StemView;
  /** false: always the Canvas 2D painter (for comparing, or a GPU driver that draws it wrong) */
  gpu: boolean;
}

export const useWaveSettings = create<WaveSettings>()(() => ({
  mode: stored(MODE_KEY, ["rgb", "bands", "stacked"], "bands"),
  stemView: stored(STEM_VIEW_KEY, ["stacked", "lanes"], "stacked"),
  gpu: stored(GPU_KEY, ["on", "off"], "on") === "on",
}));

export const waveSettings = {
  setMode(mode: WaveMode) {
    store(MODE_KEY, mode);
    useWaveSettings.setState({ mode });
  },
  setStemView(stemView: StemView) {
    store(STEM_VIEW_KEY, stemView);
    useWaveSettings.setState({ stemView });
  },
  setGpu(gpu: boolean) {
    store(GPU_KEY, gpu ? "on" : "off");
    useWaveSettings.setState({ gpu });
  },
};

/** The waveform mode, for components that draw waveforms (re-renders only when it changes). */
export const useWaveMode = () => useWaveSettings((s) => s.mode);
/** A key that changes whenever the waveform's look does: put it in the deps of whatever repaints. */
export const useWaveLook = () => useWaveSettings((s) => `${s.mode}:${s.gpu}`);
