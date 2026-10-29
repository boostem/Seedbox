// Waveform feature (owner: FRONTEND-SET): canvas 3-band waveforms for full tracks and A→B mix views.
export { TrackWaveform, type TrackWaveformProps } from "./TrackWaveform";
export { MixWaveform, type MixClock, type MixWaveformProps } from "./MixWaveform";
export { mixGeometry, type MixGeometry } from "./mix";
export { decodeWaveBinary, prepareWave, type WaveData } from "./prepare";
export { loadWave, prepareWaveAsync, useWave } from "./waveData";
export { cueColor, edgeColor, edgeWords, SECTION_LABEL, sectionColor, wavePalette } from "./palette";
export { barBeat, fmtPrecise, mergeCues } from "./draw";
