import { describe, expect, it } from "vitest";
import type { LibraryRow } from "@/api/types";
import { activeFilters, cleanFilter, EMPTY_FILTER, filterRows, isEmptyFilter, sortRows } from "./filter";

let n = 0;
export function row(p: Partial<LibraryRow> & { state?: LibraryRow["status"]["state"] } = {}): LibraryRow {
  const { state = "done", ...rest } = p;
  n += 1;
  return {
    hash: `h${n}`,
    duration: 200,
    title: `Track ${n}`,
    artist: "Artist",
    album: "",
    source: "local",
    added_at: "2026-11-01T00:00:00Z",
    bpm: null,
    bpm_from: null,
    key: null,
    key_from: null,
    key_hint: null,
    key_hint_from: null,
    energy: null,
    energy_from: null,
    genre: "",
    text: "",
    artwork: false,
    format: "mp3",
    status: { state, ready: state === "done", missing: [], waveform: state === "done", stems: false, stems_progress: null, analyses: [], jobs: {} },
    ...rest,
  };
}

describe("filterRows", () => {
  const house = row({ title: "Sunrise", artist: "Beyoncé", bpm: 124, key: "8A", energy: 0.6, genre: "Deep House", text: "Deep House · warm and dubby" });
  const dnb = row({ title: "Rollers", bpm: 87, key: "9A", energy: 0.9, genre: "Drum & Bass", state: "queued" });
  const techno = row({ title: "Warehouse", bpm: 132, key: "3B", energy: 0.8, genre: "Techno" });
  const unknown = row({ title: "Voice memo", state: "new" });
  const all = [house, dnb, techno, unknown];

  it("keeps everything with no filter", () => {
    expect(filterRows(all, EMPTY_FILTER)).toEqual(all);
    expect(isEmptyFilter(EMPTY_FILTER)).toBe(true);
  });

  it("finds the vibe in any text, without case or accents, every word", () => {
    expect(filterRows(all, { ...EMPTY_FILTER, text: "beyonce" })).toEqual([house]);
    expect(filterRows(all, { ...EMPTY_FILTER, text: "warm DUBBY" })).toEqual([house]);
    expect(filterRows(all, { ...EMPTY_FILTER, text: "warm techno" })).toEqual([]);
    expect(filterRows(all, { ...EMPTY_FILTER, text: "8a" })).toEqual([house]);
  });

  it("filters a BPM range, with half and double tempo on request", () => {
    expect(filterRows(all, { ...EMPTY_FILTER, bpmMin: 120, bpmMax: 130 })).toEqual([house]);
    expect(filterRows(all, { ...EMPTY_FILTER, bpmMin: 170, bpmMax: 176 })).toEqual([]);
    expect(filterRows(all, { ...EMPTY_FILTER, bpmMin: 170, bpmMax: 176, halfDouble: true })).toEqual([dnb]);
    expect(filterRows(all, { ...EMPTY_FILTER, bpmMin: 130 })).toEqual([techno]);
  });

  it("keeps keys that mix with the chosen one", () => {
    expect(filterRows(all, { ...EMPTY_FILTER, keyCompat: "8A" })).toEqual([house, dnb]); // same, +1
    expect(filterRows(all, { ...EMPTY_FILTER, keyCompat: "8B" })).toEqual([house]); // relative
    expect(filterRows(all, { ...EMPTY_FILTER, keyCompat: "2B" })).toEqual([techno]);
  });

  it("filters energy on a 1-10 scale and the tracks that aren't ready", () => {
    expect(filterRows(all, { ...EMPTY_FILTER, energyMin: 8 })).toEqual([dnb, techno]);
    expect(filterRows(all, { ...EMPTY_FILTER, energyMin: 5, energyMax: 8 })).toEqual([house, techno]);
    expect(filterRows(all, { ...EMPTY_FILTER, unanalysed: true })).toEqual([dnb, unknown]);
    // ready but re-analysing (a newer analyser version) is still ready; failed is not
    const busy = row({ state: "analysing", status: { ...house.status, state: "analysing", ready: true } });
    const failed = row({ state: "failed" });
    expect(filterRows([busy, failed], { ...EMPTY_FILTER, unanalysed: true })).toEqual([failed]);
  });

  it("narrows to a crate's tracks", () => {
    expect(filterRows(all, EMPTY_FILTER, new Set([techno.hash, house.hash]))).toEqual([house, techno]);
  });

  it("names the filters in use and reads stored ones back safely", () => {
    const f = { ...EMPTY_FILTER, text: "dub", bpmMin: 120, bpmMax: 124, keyCompat: "8A", energyMin: 6, unanalysed: true };
    expect(activeFilters(f)).toEqual(["“dub”", "120-124 BPM", "fits 8A", "energy 6-10", "not ready"]);
    expect(cleanFilter(JSON.parse(JSON.stringify(f)) as Record<string, unknown>)).toEqual(f);
    expect(cleanFilter({ bpmMin: "fast", keyCompat: "13Z", extra: 1 })).toEqual(EMPTY_FILTER);
    expect(cleanFilter(null)).toEqual(EMPTY_FILTER);
  });
});

describe("sortRows", () => {
  const a = row({ title: "B", artist: "Zed", bpm: 128, key: "12A", energy: 0.3 });
  const b = row({ title: "A", artist: "Abe", bpm: null, key: "1B", energy: 0.9 });
  const c = row({ title: "C", artist: "Mia", bpm: 90, key: "1A", energy: null, state: "analysing" });

  it("sorts numbers and keys around the wheel, unknowns last either way", () => {
    expect(sortRows([a, b, c], { key: "bpm", dir: "asc" }).map((r) => r.title)).toEqual(["C", "B", "A"]);
    expect(sortRows([a, b, c], { key: "bpm", dir: "desc" }).map((r) => r.title)).toEqual(["B", "C", "A"]);
    expect(sortRows([a, b, c], { key: "key", dir: "asc" }).map((r) => r.key)).toEqual(["1A", "1B", "12A"]);
    expect(sortRows([a, b, c], { key: "energy", dir: "desc" }).map((r) => r.title)).toEqual(["A", "B", "C"]);
  });

  it("sorts text and the analysis status", () => {
    expect(sortRows([a, b, c], { key: "artist", dir: "asc" }).map((r) => r.artist)).toEqual(["Abe", "Mia", "Zed"]);
    expect(sortRows([a, b, c], { key: "status", dir: "asc" })[0]).toBe(c);
  });
});
