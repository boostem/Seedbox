import { describe, expect, it } from "vitest";
import { demoAnalysis } from "@/lib/demo/analysis";
import { decodeWaveBinary, encodeWaveBinary, prepareBands, prepareWave, WAVE_HEADER } from "./prepare";
import { prepareWaveAsync } from "./waveData";

describe("waveform prep", () => {
  it("normalises each band to its 99th percentile", () => {
    const w = prepareBands({ points_per_second: 10, low: [0, 0.3, 0.6, 0.6], mid: [1, 1, 1, 1], high: [0.1, 0.2, 0.2, 0.2] }, 0)!;
    expect(w.n).toBe(4);
    expect(w.duration).toBeCloseTo(0.4);
    expect([...w.low]).toEqual([0, 0.5, 1, 1]);
    expect([...w.mid]).toEqual([1, 1, 1, 1]);
  });

  it("gives the same data off the main thread (inline where there are no workers)", async () => {
    const an = demoAnalysis();
    const data = await prepareWaveAsync(an);
    expect(data!.n).toBe(prepareWave(an)!.n);
    expect([...data!.low.slice(0, 50)]).toEqual([...prepareWave(an)!.low.slice(0, 50)]);
    expect(await prepareWaveAsync(an)).toBe(data); // cached per waveform
  });
});

describe("binary waveform file", () => {
  it("round-trips through the log-scaled bytes within a decibel", () => {
    const low = [0, 0.001, 0.01, 0.1, 0.5, 1];
    const buf = encodeWaveBinary(150, [low, low.map((x) => x / 2), low.map((x) => x / 4)]);
    expect(buf.byteLength).toBe(WAVE_HEADER + 3 * low.length);
    const w = decodeWaveBinary(buf);
    expect(w.pps).toBe(150);
    expect(w.n).toBe(6);
    expect(w.duration).toBeCloseTo(6 / 150);
    expect(w.low[0]).toBe(0);
    // normalised to the band's ceiling (its loudest point here), so compare ratios in dB
    for (let i = 2; i < low.length; i++) expect(20 * Math.log10(w.low[i] / w.low[5])).toBeCloseTo(20 * Math.log10(low[i]), 0);
  });

  it("is a 6-minute track at 150 points/s in well under 200 KB", () => {
    const n = 360 * 150;
    const band = Array.from({ length: n }, (_, i) => Math.abs(Math.sin(i / 7)));
    expect(encodeWaveBinary(150, [band, band, band]).byteLength).toBeLessThan(200_000);
  });

  it("rejects other files", () => {
    expect(() => decodeWaveBinary(new ArrayBuffer(4))).toThrow(/too short/);
    expect(() => decodeWaveBinary(new TextEncoder().encode("RIFF0000WAVEfmt 0000").buffer)).toThrow(/not a waveform/);
    const buf = encodeWaveBinary(150, [[1], [1], [1]]);
    new DataView(buf).setUint8(4, 9);
    expect(() => decodeWaveBinary(buf)).toThrow(/version 9/);
    expect(() => decodeWaveBinary(encodeWaveBinary(150, [[1, 1], [1, 1], [1, 1]]).slice(0, WAVE_HEADER + 3))).toThrow(/corrupt/);
  });
});
