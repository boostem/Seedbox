import { describe, expect, it, vi } from "vitest";
import { overviewColumns, OverviewStore, parseOverviews } from "./overviews";

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

describe("overviewColumns", () => {
  it("keeps the loudest column under each pixel", () => {
    const peaks = new Uint8Array([0, 255, 51, 102, /* mid */ 0, 0, 0, 0, /* high */ 255, 255, 255, 255]);
    const [low, mid, high] = overviewColumns({ columns: 4, bands: 3, peaks }, 2);
    expect(low[0]).toBe(1);
    expect(low[1]).toBeCloseTo(0.4);
    expect(Array.from(mid)).toEqual([0, 0]);
    expect(Array.from(high)).toEqual([1, 1]);
    expect(Array.from(overviewColumns({ columns: 4, bands: 3, peaks }, 8)[0]).slice(0, 4)).toEqual([0, 0, 1, 1]);
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
