/**
 * Stem waveforms: the four peaks of a track's stems (#78), and how they turn into heights and brightness. Pure
 * functions, so the worker can decode and tests can run them. `stemDraw.ts` paints, `gl/shaders.ts` has the same sums
 * for the GPU.
 */
import type { Deck, Keyframe, StemLanes } from "@/api/mixplan.generated";
import { STEM_CURVE } from "./gl/shaders";
import { ceiling } from "./prepare";

export const STEMS = ["drums", "bass", "vocals", "other"] as const;
export type StemName = (typeof STEMS)[number];
export type StemView = "stacked" | "lanes";

export interface StemWave {
  pps: number; // points per second
  n: number;
  duration: number;
  drums: Float32Array;
  bass: Float32Array;
  vocals: Float32Array;
  other: Float32Array;
}

// ------------------------------------------------------------------------------------------------ the file

const MAGIC = "SBSW";
const FILE_ORDER: StemName[] = ["drums", "bass", "other", "vocals"]; // seedbox2/analysis/waveform.py STEMS

/** The stem detail file (`GET /api/tracks/:hash/waveform/stems`, format in seedbox2/analysis/waveform.py). All stems
 * share one ceiling: a quiet stem stays quiet next to the others, which is what the layers and lanes are there to show. */
export function decodeStemWave(buf: ArrayBuffer): StemWave {
  if (buf.byteLength < 20) throw new Error("stem waveform file too short");
  const v = new DataView(buf);
  const magic = String.fromCharCode(v.getUint8(0), v.getUint8(1), v.getUint8(2), v.getUint8(3));
  if (magic !== MAGIC) throw new Error("not a stem waveform file");
  const version = v.getUint16(4, true);
  if (version !== 1) throw new Error(`unsupported stem waveform file version ${version}`);
  const header = v.getUint16(6, true);
  const pps = v.getUint16(8, true);
  const stems = v.getUint8(10);
  const floorDb = v.getUint8(11);
  const n = v.getUint32(12, true);
  const duration = v.getFloat32(16, true);
  if (stems !== FILE_ORDER.length || !(pps > 0) || buf.byteLength < header + stems * n) throw new Error("corrupt stem waveform file");
  const fromByte = new Float32Array(256).map((_, b) => (b === 0 ? 0 : 10 ** (((b / 255 - 1) * floorDb) / 20)));
  const planes = FILE_ORDER.map((_, k) => {
    const bytes = new Uint8Array(buf, header + k * n, n);
    const amp = new Float32Array(n);
    for (let i = 0; i < n; i++) amp[i] = fromByte[bytes[i]];
    return amp;
  });
  const top = Math.max(...planes.map(ceiling));
  for (const amp of planes) {
    for (let i = 0; i < n; i++) amp[i] = Math.min(1, amp[i] / top);
  }
  const [drums, bass, other, vocals] = planes;
  return { pps, n, duration: duration || n / pps, drums, bass, vocals, other };
}

export type StemFetch = { state: "ready"; wave: StemWave } | { state: "analysing" } | { state: "none" };

/** Fetches and decodes a track's stem waveform. `analysing`: the waveform job is writing it; `none`: no stems yet. */
export async function fetchStemWave(url: string): Promise<StemFetch> {
  const r = await fetch(url);
  if (r.status === 202) return { state: "analysing" };
  if (r.status === 404) return { state: "none" };
  if (!r.ok) throw new Error(`stem waveform: HTTP ${r.status}`);
  return { state: "ready", wave: decodeStemWave(await r.arrayBuffer()) };
}

// ------------------------------------------------------------------------------------------------ heights

/** A stem's peak (0-1) as a height (0-1). */
export const stemShape = (v: number): number => (v > 0 ? v ** STEM_CURVE : 0);

/**
 * One column of the stacked view, from the shaped peaks of drums, bass, vocals and other: the height of each layer
 * (same order), as a share of the half height. The envelope is the stems' energy sum, as the whole waveform would be,
 * and each stem gets the share of it its amplitude has.
 */
export function stemStack(drums: number, bass: number, vocals: number, other: number): [number, number, number, number] {
  const total = drums + bass + vocals + other;
  if (!(total > 0)) return [0, 0, 0, 0];
  const k = Math.min(1, Math.hypot(drums, bass, vocals, other)) / total;
  return [drums * k, bass * k, vocals * k, other * k];
}

/** Brightness of a stem at a linear gain: a muted stem stays as a dark outline of what isn't playing. */
export const stemBrightness = (gain: number): number => 0.14 + 0.86 * Math.min(1, Math.max(0, gain));

// ------------------------------------------------------------------------------------------------ the plan

/** A lane's value at a master beat: `lane_value` in seedbox2/mix/plan.py. */
export function laneValue(lane: Keyframe[], beat: number): number {
  if (beat < lane[0].beat) return lane[0].value;
  let i = 0;
  while (i + 1 < lane.length && lane[i + 1].beat <= beat) i++; // the later one of a step wins from that beat on
  const a = lane[i];
  const b = lane[Math.min(i + 1, lane.length - 1)];
  const span = b.beat - a.beat;
  if (!(span > 0) || a.curve === "hold") return a.value;
  const t = Math.min(1, (beat - a.beat) / span);
  return a.value + (b.value - a.value) * (a.curve === "ease" ? t * t * (3 - 2 * t) : t);
}

const LANE_OF: Record<StemName, keyof StemLanes> = { drums: "gain_drums", bass: "gain_bass", vocals: "gain_vocals", other: "gain_other" };

/** The linear gains (drums, bass, vocals, other) of a plan's deck at a master beat, written to `out` when given, so a
 * per-frame caller allocates nothing. A deck without stem lanes plays the full mix: all 1. */
export function planStemGains(deck: Pick<Deck, "stem_lanes">, beat: number, out: number[] | Float32Array = new Float32Array(4)): number[] | Float32Array {
  STEMS.forEach((name, i) => {
    out[i] = deck.stem_lanes ? laneValue(deck.stem_lanes[LANE_OF[name]], beat) : 1;
  });
  return out;
}
