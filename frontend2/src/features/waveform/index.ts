// Waveform feature (owner: FRONTEND-SET): WebGL2 waveforms (RGB, 3-band, stacked; Canvas 2D fallback) for full tracks and A→B mix views.
export { TrackWaveform, type TrackWaveformProps } from "./TrackWaveform";
export { WaveModeTabs, WaveSettingsPanel } from "./WaveSettings";
export { WaveOverview, type WaveOverviewProps } from "./WaveOverview";
export { MixWaveform, type MixClock, type MixWaveformProps } from "./MixWaveform";
export { mixGeometry, type MixGeometry } from "./mix";
export { decodeWaveBinary, prepareWave, type WaveData } from "./prepare";
export { loadWave, prepareWaveAsync, useWave } from "./waveData";
export { cueColor, edgeColor, edgeWords, SECTION_LABEL, sectionColor, wavePalette } from "./palette";
export { barBeat, drawWave, drawWave2d, fmtPrecise, mergeCues, type WaveOptions } from "./draw";
export { type WaveMode, WAVE_MODES, useWaveMode, useWaveSettings, waveSettings } from "./settings";
export { waveRendererInfo, waveRendererName, webgl2Available } from "./gl/renderer";
