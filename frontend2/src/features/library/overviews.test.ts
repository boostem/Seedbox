import { describe, expect, it, vi } from "vitest";
import { overviewWave, OverviewStore, parseOverviews } from "./overviews";

function response(rows: (Uint8Array | null)[], columns = 4, bands = 3): ArrayBuffer {
  const size = 12 + rows.reduce((n, r) => n + 1 + (r ? r.length : 0), 0);
  const buf = new ArrayBuffer(size);
  const dv = new DataView(buf);
  [..."SBOV"].forEach((ch, i) => dv.setUint8(i, ch.charCodeAt(0)));
  dv.setUint16(4, columns, true);
  dv.setUint8(6, bands);
  dv.setUint32(8, rows.length, true);
  let at = 12;
  const bytes = new Uint8Array(buf);
  for (const r of rows) {
    bytes[at++] = r ? 1 : 0;
    if (r) {
      bytes.set(r, at);
      at += r.length;
    }
  }
  return buf;
}

describe("parseOverviews", () => {
  it("reads present and absent tracks in order", () => {
    const peaks = Uint8Array.from({ length: 12 }, (_, i) => i * 20);
    const got = parseOverviews(response([null, peaks]));
    expect(got[0]).toBeNull();
    expect(got[1]?.columns).toBe(4);
    expect(Array.from(got[1]!.peaks)).toEqual(Array.from(peaks));
  });

  it("rejects other data and cut-short responses", () => {
    expect(() => parseOverviews(new ArrayBuffer(4))).toThrow();
    expect(() => parseOverviews(response([new Uint8Array(12)]).slice(0, 20))).toThrow();
  });
});

describe("overviewWave", () => {
  it("gives the renderer linear, normalised bands over the overview's columns", () => {
    const peaks = new Uint8Array(12);
    peaks.set([0, 128, 255, 255], 0); // low
    peaks.set([255, 255, 255, 255], 4); // mid
    const w = overviewWave({ columns: 4, bands: 3, peaks })!;
    expect([w.n, w.pps, w.duration]).toEqual([4, 4, 1]);
    expect(w.low[0]).toBe(0);
    expect(w.low[1]).toBeGreaterThan(0.05); // half the 48 dB scale is -24 dB
    expect(w.low[1]).toBeLessThan(0.08);
    expect(w.low[3]).toBe(1);
    expect(w.mid[0]).toBe(1);
    expect(w.high[0]).toBe(0);
  });

  it("has nothing for an overview without three bands", () => {
    expect(overviewWave({ columns: 2, bands: 1, peaks: new Uint8Array(2) })).toBeNull();
  });
});

describe("OverviewStore", () => {
  it("batches what the rows ask for into one request and notifies", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn((hashes: string[]) => Promise.resolve(response(hashes.map((h) => (h === "a" ? new Uint8Array(12) : null)))));
    const store = new OverviewStore(fetcher);
    const heard = vi.fn();
    store.subscribe(heard);
    store.want("a");
    store.want("b");
    store.want("a");
    await vi.runAllTimersAsync();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith(["a", "b"]);
    expect(store.get("a")?.columns).toBe(4);
    expect(store.get("b")).toBeNull();
    expect(heard).toHaveBeenCalled();
    store.want("b"); // known to have none: not asked again…
    await vi.runAllTimersAsync();
    expect(fetcher).toHaveBeenCalledTimes(1);
    store.invalidate("b"); // …until its waveform is done
    expect(store.get("b")).toBeUndefined();
    store.want("b");
    await vi.runAllTimersAsync();
    expect(fetcher).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
});
