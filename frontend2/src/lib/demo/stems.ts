/** Synthetic stems and a bass hand-off between two decks, for the design-system page: the stem waveforms and their
 * live gains without a backend or an engine. The clock rests mid-hand-off, so the page shows the dimming standing still. */
import type { Keyframe, StemLanes } from "@/api/mixplan.generated";
import type { StemClock } from "@/features/waveform/StemWaveform";
import { planStemGains, type StemWave } from "@/features/waveform/stems";

const BPM = 128;
const BEAT_S = 60 / BPM;
const BARS = 64;
const PPS = 150;
export const HANDOFF_BAR = 32; // the bass changes hands around this bar
export const DEMO_HANDOFF_START = (HANDOFF_BAR - 8) * 4 * BEAT_S;
export const DEMO_HANDOFF_SPAN = 16 * 4 * BEAT_S;
const PLAY_FROM = (HANDOFF_BAR - 6) * 4 * BEAT_S;
const PLAY_TO = (HANDOFF_BAR + 6) * 4 * BEAT_S;
const REST = (HANDOFF_BAR + 0.5) * 4 * BEAT_S;

export function demoStems(seed: number): StemWave {
  const n = BARS * 4 * BEAT_S * PPS;
  const len = Math.round(n);
  let s = seed >>> 0;
  const r = () => (s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32;
  const out = { drums: new Float32Array(len), bass: new Float32Array(len), vocals: new Float32Array(len), other: new Float32Array(len) };
  for (let i = 0; i < len; i++) {
    const beats = (i / PPS) / BEAT_S;
    const beat = beats % 1;
    const bar = beats / 4;
    const phrase = Math.floor(bar / 8);
    const sung = (phrase + seed) % 2 === 0 && (bar % 2) < 1.5;
    out.drums[i] = Math.exp(-beat * 8) * 0.8 + (beat > 0.5 && beat < 0.58 ? 0.2 : 0) + r() * 0.03;
    out.bass[i] = 0.5 * Math.exp(-((beat + 0.5) % 1) * 4) + r() * 0.02;
    out.vocals[i] = sung ? 0.12 + 0.16 * Math.sin(beats * 1.7) ** 2 + r() * 0.04 : r() * 0.005;
    out.other[i] = 0.07 + 0.09 * Math.sin(beats * 0.4) ** 2 + r() * 0.03;
  }
  return { pps: PPS, n: len, duration: len / PPS, ...out };
}

const key = (bar: number, value: number, curve: Keyframe["curve"] = "linear"): Keyframe => ({ beat: bar * 4, value, curve });
const fade = (from: number, to: number, bars: number): Keyframe[] => [key(HANDOFF_BAR - bars / 2, from), key(HANDOFF_BAR + bars / 2, to)];
const lanes = (bass: Keyframe[], vocals: Keyframe[]): StemLanes => ({ gain_drums: [key(0, 1)], gain_bass: bass, gain_vocals: vocals, gain_other: [key(0, 1)] });

export function demoHandoff() {
  const subs = new Set<() => void>();
  let at = REST;
  let raf = 0;
  const clock: StemClock = {
    get: () => at,
    running: () => raf !== 0,
    subscribe(cb) {
      subs.add(cb);
      return () => subs.delete(cb);
    },
  };
  const decks = [
    { label: "Outgoing deck: its bass and vocals leave", stem_lanes: lanes(fade(1, 0, 2), fade(1, 0, 8)), seed: 3 },
    { label: "Incoming deck: its bass and vocals arrive", stem_lanes: lanes(fade(0, 1, 2), fade(0, 1, 8)), seed: 8 },
  ].map(({ label, stem_lanes, seed }) => {
    const out = new Float32Array(4);
    return { label, stems: demoStems(seed), gainsAt: (t: number) => planStemGains({ stem_lanes }, t / BEAT_S, out) };
  });
  const play = () => {
    cancelAnimationFrame(raf);
    const started = performance.now();
    const step = () => {
      at = PLAY_FROM + (performance.now() - started) / 1000;
      if (at >= PLAY_TO) {
        at = REST;
        raf = 0;
      } else raf = requestAnimationFrame(step);
      for (const cb of subs) cb();
    };
    raf = requestAnimationFrame(step);
    for (const cb of subs) cb();
  };
  return { decks, clock, play };
}
