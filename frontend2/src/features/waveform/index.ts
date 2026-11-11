// Waveform feature (owner: FRONTEND-SET): WebGL2 waveforms (RGB, 3-band, stacked; Canvas 2D fallback) for full tracks and A→B mix views.
export { TrackWaveform, type TrackWaveformProps } from "./TrackWaveform";
export { StemViewTabs, WaveFollowTabs, WaveModeTabs, WaveSettingsPanel } from "./WaveSettings";
export { playerStemClock, StemLegend, type StemClock, StemWaveform, type StemWaveformProps } from "./StemWaveform";
export { drawStems } from "./stemDraw";
export { decodeStemWave, laneValue, planStemGains, STEMS, type StemName, type StemView, type StemWave } from "./stems";
export { WaveOverview, type WaveOverviewProps } from "./WaveOverview";
export { MixWaveform, type MixClock, type MixWaveformProps } from "./MixWaveform";
export { mixGeometry, type MixGeometry } from "./mix";
export { decodeWaveBinary, prepareWave, type WaveData } from "./prepare";
export { loadWave, prepareWaveAsync, useStemWave, useTrackWave, useWave } from "./waveData";
export { cueColor, edgeColor, edgeWords, SECTION_LABEL, sectionColor, slotLetter, styleColor, styleOf, wavePalette } from "./palette";
export { barAt, barTime, beatAt, beatTime, phraseLevel, phraseStarts } from "./grid";
export { barBeat, drawWave, drawWave2d, fmtPrecise, mergeCues, setupCanvas, type WaveOptions, type WaveRegion } from "./draw";
export { beatPhase, fmtBarsBeats, type BeatPhase } from "./phase";
export { type WaveFollow, WAVE_FOLLOWS, type WaveMode, WAVE_MODES, useWaveMode, useWaveSettings, waveSettings } from "./settings";
export { waveRendererInfo, waveRendererName, webgl2Available } from "./gl/renderer";
export { WavePlaceholder, type WavePlaceholderProps, type WavePlaceholderState } from "./WavePlaceholder";
