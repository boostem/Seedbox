// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStemWave } from "./waveData";

function stemFile(): ArrayBuffer {
  const buf = new ArrayBuffer(20 + 4);
  const v = new DataView(buf);
  for (let i = 0; i < 4; i++) v.setUint8(i, "SBSW".charCodeAt(i));
  v.setUint16(4, 1, true);
  v.setUint16(6, 20, true);
  v.setUint16(8, 150, true);
  v.setUint8(10, 4);
  v.setUint8(11, 48);
  v.setUint32(12, 1, true);
  new Uint8Array(buf, 20).fill(255);
  return buf;
}

const answers = (...statuses: number[]) => {
  vi.stubGlobal("Worker", undefined); // deno has one: these run without it, as in a browser that has none
  const list = [...statuses];
  const fetchMock = vi.fn(() => {
    const status = list.length > 1 ? list.shift()! : list[0];
    return Promise.resolve({ status, ok: status >= 200 && status < 300, arrayBuffer: () => Promise.resolve(stemFile()) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
};

const flush = () => act(() => vi.advanceTimersByTimeAsync(0));

describe("useStemWave", () => {
  beforeEach(() => void vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("waits for stems that aren't separated yet, and for the job that measures them", async () => {
    const fetchMock = answers(404, 202, 200);
    const { result } = renderHook(() => useStemWave("waiting"));
    expect(result.current.state).toBe("loading");
    await flush();
    expect(result.current.state).toBe("none");
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(result.current.state).toBe("analysing");
    await act(() => vi.advanceTimersByTimeAsync(1500));
    expect(result.current.state).toBe("ready");
    expect(result.current.wave?.n).toBe(1);
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(fetchMock).toHaveBeenCalledTimes(3); // and it stops asking
  });

  it("gives up on a job that never finishes, and tries again on retry", async () => {
    answers(202);
    const { result } = renderHook(() => useStemWave("stuck"));
    await act(() => vi.advanceTimersByTimeAsync(1500 * 41));
    expect(result.current.state).toBe("failed");
    answers(200);
    act(() => result.current.retry());
    await flush();
    expect(result.current.state).toBe("ready");
  });

  it("says so when the server fails", async () => {
    answers(500);
    const { result } = renderHook(() => useStemWave("broken"));
    await flush();
    expect(result.current.state).toBe("failed");
  });

  it("does nothing without a track", () => {
    const fetchMock = answers(200);
    expect(renderHook(() => useStemWave(null)).result.current.state).toBe("idle");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
