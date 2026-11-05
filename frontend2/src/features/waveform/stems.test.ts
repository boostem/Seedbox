import { describe, expect, it } from "vitest";
import { stemAtlas } from "./gl/mips";
import { decodeStemWave, laneValue, planStemGains, stemBrightness, stemShape, stemStack } from "./stems";

function file(rows: number[][], { magic = "SBSW", version = 1 } = {}): ArrayBuffer {
  const n = rows[0].length;
  const buf = new ArrayBuffer(20 + rows.length * n);
  const v = new DataView(buf);
  for (let i = 0; i < 4; i++) v.setUint8(i, magic.charCodeAt(i));
  v.setUint16(4, version, true);
  v.setUint16(6, 20, true);
  v.setUint16(8, 150, true);
  v.setUint8(10, rows.length);
  v.setUint8(11, 48);
  v.setUint32(12, n, true);
  v.setFloat32(16, n / 150, true);
  rows.forEach((r, k) => new Uint8Array(buf, 20 + k * n, n).set(r));
  return buf;
}

describe("the stem file", () => {
  it("reads stems in the file's order, on one shared ceiling", () => {
    // file order: drums, bass, other, vocals. 255 is full scale, each 85 down is 16 dB, 0 is silence
    const w = decodeStemWave(file([[0, 0], [255, 255], [170, 170], [85, 85]]));
    expect([w.n, w.pps]).toEqual([2, 150]);
    expect(w.duration).toBeCloseTo(2 / 150);
    expect(w.drums[0]).toBe(0);
    expect(w.bass[0]).toBeCloseTo(1);
    expect(w.other[0]).toBeCloseTo(10 ** (-16 / 20), 3);
    expect(w.vocals[0]).toBeCloseTo(10 ** (-32 / 20), 3);
  });

  it("refuses what isn't one", () => {
    expect(() => decodeStemWave(new ArrayBuffer(4))).toThrow("too short");
    expect(() => decodeStemWave(file([[1], [1], [1], [1]], { magic: "SBWF" }))).toThrow("not a stem");
    expect(() => decodeStemWave(file([[1], [1], [1], [1]], { version: 2 }))).toThrow("version 2");
    expect(() => decodeStemWave(file([[1], [1], [1]]))).toThrow("corrupt");
  });
});

describe("heights and brightness", () => {
  it("splits the stems' energy sum by amplitude, and stays inside the band", () => {
    const [d, b, v, o] = stemStack(0.6, 0.3, 0, 0);
    expect(d + b + v + o).toBeCloseTo(Math.hypot(0.6, 0.3));
    expect(d / b).toBeCloseTo(2);
    expect(v).toBe(0);
    const full = stemStack(1, 1, 1, 1);
    expect(full.reduce((a, x) => a + x)).toBeCloseTo(1);
    expect(stemStack(0, 0, 0, 0)).toEqual([0, 0, 0, 0]);
  });

  it("keeps a muted stem as a dark outline and a silent point at nothing", () => {
    expect(stemBrightness(1)).toBe(1);
    expect(stemBrightness(0)).toBeCloseTo(0.14);
    expect(stemBrightness(2)).toBe(1);
    expect(stemShape(0)).toBe(0);
  });
});

describe("the plan's lanes", () => {
  // the values seedbox2/mix/plan.py lane_value gives for the same lane
  const lane = [
    { beat: 0, value: 0, curve: "linear" as const },
    { beat: 4, value: 1, curve: "ease" as const },
    { beat: 8, value: 0, curve: "hold" as const },
    { beat: 12, value: 1, curve: "linear" as const },
    { beat: 12, value: 0.5, curve: "linear" as const },
  ];

  it("reads like plan.py's lane_value", () => {
    expect(laneValue(lane, -1)).toBe(0);
    expect(laneValue(lane, 2)).toBeCloseTo(0.5);
    expect(laneValue(lane, 5)).toBeCloseTo(1 - 0.15625);
    expect(laneValue(lane, 10)).toBe(0);
    expect(laneValue(lane, 12)).toBe(0.5);
    expect(laneValue(lane, 99)).toBe(0.5);
  });

  it("gives each stem its gain, and 1 to a deck playing the full mix", () => {
    const k = (value: number) => [{ beat: 0, value, curve: "linear" as const }];
    const stem_lanes = { gain_drums: k(0.1), gain_bass: k(0.2), gain_vocals: k(0.3), gain_other: k(0.4) };
    expect(Array.from(planStemGains({ stem_lanes }, 3), (x) => +x.toFixed(2))).toEqual([0.1, 0.2, 0.3, 0.4]);
    expect(Array.from(planStemGains({ stem_lanes: null }, 3))).toEqual([1, 1, 1, 1]);
  });
});

describe("the stem atlas", () => {
  it("keeps four stems in four channels, with peaks per channel going up the levels", () => {
    const n = 512;
    const zero = () => new Float32Array(n);
    const stems = { n, drums: zero(), bass: zero(), vocals: zero(), other: zero() };
    stems.drums[0] = 1;
    stems.other[1] = 0.5;
    const a = stemAtlas(stems, 256);
    expect(a.width).toBe(256);
    expect(a.lengths[0]).toBe(512);
    expect(Array.from(a.data.slice(0, 8))).toEqual([255, 0, 0, 0, 0, 0, 0, 128]);
    const level1 = a.rows[1] * a.width * 4;
    expect(Array.from(a.data.slice(level1, level1 + 4))).toEqual([255, 0, 0, 128]); // not the loudest in alpha
  });
});
