/** Two synthetic tracks in a bass-swap blend, for screenshots and the benchmark: no server, no audio. It rests mid-blend, and
 * `play` runs a clock over the blend again and again. */
import { LANE_DEFAULTS, LANES, type Deck, type Keyframe, type Lanes, type MixPlan } from "@/api/mixplan.generated";
import type { SessionTrack, SetSession } from "@/api/types";
import type { NowPlayingDriver } from "@/features/nowplaying/driver";
import { planChain } from "@/features/nowplaying/session";
import { demoAnalysis } from "./analysis";
import { demoStems } from "./stems";

const BPM = 128;
const BLEND_BAR = 48;
const BLEND_BARS = 16;
const FROM = BLEND_BAR * 4;
const TO = FROM + BLEND_BARS * 4;
const REST = FROM + 32 + 2;
const LOOP_FROM = FROM - 32;

const kf = (beat: number, value: number, curve: Keyframe["curve"] = "linear"): Keyframe => ({ beat, value, curve });
const ramp = (a: number, b: number, from = FROM, to = TO): Keyframe[] => [kf(from, a), kf(to, b)];

function deck(track_id: string, startBeat: number, lanes: Partial<Lanes>, stemLanes: Deck["stem_lanes"], playFrom: number, playTo: number): Deck {
  const rest = Object.fromEntries(LANES.map((n) => [n, [kf(0, LANE_DEFAULTS[n], "hold")]])) as unknown as Lanes;
  return {
    track_id,
    grid: [{ start_beat: startBeat, start_s: 0, bpm: BPM }],
    key_shift: 0,
    play_from_beat: playFrom,
    play_to_beat: playTo,
    lanes: { ...rest, ...lanes },
    stem_lanes: stemLanes,
    sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "pre_eq" },
    loops: [],
    fx: [],
    pinned: [],
  };
}

const HASHES = ["demo-a", "demo-b", "demo-c"];

function track(i: number, title: string, artist: string, camelot: string, key: string): SessionTrack {
  const duration = demoAnalysis().duration_s;
  return { hash: HASHES[i], title, artist, duration, grid: null, sections: [], key: { camelot, key }, integrated_lufs: null };
}

export function demoNowPlaying(): NowPlayingDriver & { setRunning: (on: boolean) => void } {
  const hold = (v: number) => [kf(0, v, "hold")];
  const plan: MixPlan = {
    version: 3,
    technique: "bass swap",
    bars: BLEND_BARS,
    key_lock: false,
    window: { start_beat: 0, end_beat: TO + 32 },
    tempo: [kf(0, BPM, "hold")],
    a: deck(
      HASHES[0],
      0,
      { eq_low: ramp(0, -30, REST - 4, REST + 4), filter: ramp(0, 0.45, REST, TO), volume: ramp(1, 0.2) },
      { gain_drums: ramp(1, 0.5), gain_bass: ramp(1, 0, REST - 4, REST + 4), gain_vocals: ramp(1, 0, FROM, FROM + 16), gain_other: hold(1) },
      0,
      TO,
    ),
    b: deck(
      HASHES[1],
      FROM,
      { eq_high: ramp(-12, 0), volume: ramp(0.3, 1) },
      { gain_drums: ramp(0.4, 1), gain_bass: ramp(0, 1, REST - 4, REST + 4), gain_vocals: hold(0), gain_other: ramp(0.3, 1) },
      FROM,
      TO + 32,
    ),
    markers: [],
    notes: [],
    edited: false,
  };
  const session: SetSession = {
    id: 0,
    name: "Demo set",
    tracks: [track(0, "Night Shift", "Harbour Lights", "8A", "A minor"), track(1, "Paper Tigers", "Low Season", "9A", "E minor"), track(2, "Open Water", "Harbour Lights", "9B", "G major")],
    plans: [plan, null],
  };
  const chain = planChain(session);
  chain[0] = plan; // the demo's window is its own
  const analysis = demoAnalysis();
  const assets = {
    [HASHES[0]]: { analysis: { ...analysis, track_id: HASHES[0] }, stems: demoStems(3) },
    [HASHES[1]]: { analysis: { ...analysis, track_id: HASHES[1] }, stems: demoStems(8) },
  };
  const subs = new Set<() => void>();
  let running = false;
  let started = 0;
  const beat = () => (running ? LOOP_FROM + ((((performance.now() - started) / 1000) * BPM) / 60) % (TO + 16 - LOOP_FROM) : REST);
  const setRunning = (on: boolean) => {
    running = on;
    started = performance.now();
    for (const s of subs) s();
  };
  return {
    session,
    chain,
    status: () => (running ? "playing" : "paused"),
    error: () => null,
    index: () => 0,
    plan: () => plan,
    beat,
    play: () => setRunning(true),
    pause: () => setRunning(false),
    seek: () => {},
    subscribe: (l) => {
      subs.add(l);
      return () => subs.delete(l);
    },
    assets: (hash) => assets[hash] ?? null,
    close: () => setRunning(false),
    setRunning,
  };
}
