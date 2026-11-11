import { describe, expect, it } from "vitest";
import { LANE_DEFAULTS, LANES, type Deck, type Keyframe, type Lanes, type MixPlan } from "@/api/mixplan.generated";
import type { SessionTrack, SetSession } from "@/api/types";
import { beatAtFile, deckTime } from "@/audio/engine/position";
import fixture from "./setchain.fixture.json";
import type { WaveData } from "@/features/waveform/prepare";
import type { StemWave } from "@/features/waveform/stems";
import { bandLevels, blendSpan, deckAnalysis, deckRate, deckRegions, deckRole, extendPlan, fmtCountdown, HANDOFF_BEATS, heldTime, planChain, playableRun, readStrip, stemLevels, timeLeft, upNext } from "./session";

const kf = (beat: number, value: number, curve: Keyframe["curve"] = "linear"): Keyframe => ({ beat, value, curve });

function deck(track_id: string, startBeat: number, startS: number, from: number, to: number, over: Partial<Deck> = {}, bpm = 120): Deck {
  const lanes = Object.fromEntries(LANES.map((n) => [n, [kf(0, LANE_DEFAULTS[n], "hold")]])) as unknown as Lanes;
  return {
    track_id,
    grid: [{ start_beat: startBeat, start_s: startS, bpm }],
    key_shift: 0,
    play_from_beat: from,
    play_to_beat: to,
    lanes,
    stem_lanes: null,
    sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "pre_eq" },
    loops: [],
    fx: [],
    pinned: [],
    ...over,
  };
}

/** A into B over master beats [100, 132), with some room around them. */
function plan(a = "x", b = "y", over: Partial<MixPlan> = {}): MixPlan {
  return {
    version: 3,
    technique: "bass swap",
    bars: 8,
    key_lock: false,
    window: { start_beat: 84, end_beat: 148 },
    tempo: [kf(0, 120, "hold")],
    a: deck(a, 0, 0, 84, 132),
    b: deck(b, 100, 8, 100, 148),
    markers: [],
    notes: [],
    edited: false,
    ...over,
  };
}

const track = (hash: string, duration = 300): SessionTrack => ({ hash, title: hash.toUpperCase(), artist: null, duration, grid: null, sections: [], key: { key: null, camelot: null }, integrated_lufs: null });

describe("extendPlan", () => {
  it("opens the window to where A comes in and B ends, and leaves the rest of the plan", () => {
    const p = plan();
    const e = extendPlan(p, 0, 300);
    expect(e.window.start_beat).toBe(0);
    expect(e.a.play_from_beat).toBe(0);
    expect(deckTime(e.b, e.window.end_beat)).toBeCloseTo(300);
    expect(e.b.play_to_beat).toBe(e.window.end_beat);
    expect(e.a.play_to_beat).toBe(132);
    expect(e.b.play_from_beat).toBe(100);
    expect(p.window.start_beat).toBe(84);
  });

  it("never shrinks a window", () => {
    const e = extendPlan(plan(), 500, 1);
    expect(e.window).toEqual({ start_beat: 84, end_beat: 148 });
  });

  it("reaches a start before the file's first beat of the grid", () => {
    const e = extendPlan(plan("x", "y", { a: deck("x", 16, 8, 84, 132) }), 0, null);
    expect(e.window.start_beat).toBe(0);
  });
});

describe("the chain of a set", () => {
  const tracks = [track("x"), track("y"), track("z")];

  it("picks each track up before the point the plan before hands it over", () => {
    const next = plan("y", "z", { a: deck("y", 0, 0, 200, 260), b: deck("z", 220, 0, 220, 280), window: { start_beat: 200, end_beat: 280 } });
    const [first, second] = planChain({ tracks, plans: [plan(), next] });
    const handed = deckTime(first!.b, first!.window.end_beat - HANDOFF_BEATS);
    expect(beatAtFile(second!.a, handed)).toBeGreaterThan(second!.window.start_beat);
    expect(second!.a.play_from_beat).toBe(second!.window.start_beat);
    expect(deckTime(second!.b, second!.window.end_beat)).toBeCloseTo(300);
    expect(first!.window.end_beat).toBe(148); // not the last: it ends where the plan says
  });

  it("stops at the first pair without a plan, and starts at the first with one", () => {
    expect(playableRun({ plans: [null, plan(), null, plan()] })).toEqual({ first: 1, last: 1 });
    expect(playableRun({ plans: [null] })).toBeNull();
    const four = [...tracks, track("w")];
    const chain = planChain({ tracks: four, plans: [plan(), null, plan()] });
    expect(chain[1]).toBeNull();
    expect(chain[2]).toBeNull();
    expect(deckTime(chain[0]!.b, chain[0]!.window.end_beat)).toBeCloseTo(300); // the track plays out
  });
});

describe("the transition", () => {
  it("is where both decks are heard", () => {
    expect(blendSpan(plan())).toEqual({ start: 100, end: 132 });
  });

  it("is the lead-in of a cut", () => {
    const p = plan("x", "y", { bars: 2, a: deck("x", 0, 0, 84, 120), b: deck("y", 120, 0, 120, 148) });
    expect(blendSpan(p)).toEqual({ start: 112, end: 120 });
  });

  it("falls on each deck's own time", () => {
    const [a] = deckRegions(plan(), "a");
    const [b] = deckRegions(plan(), "b");
    expect([a.start_s, a.end_s]).toEqual([50, 66]);
    expect([b.start_s, b.end_s]).toEqual([8, 24]);
    expect(a.label).toBe("Bass swap");
    expect(a.technique).toBe("bass swap"); // the overlap takes its style's colour
  });
});

describe("a deck at a beat", () => {
  const p = plan();

  it("waits at its cue, then runs, then stays at its last sample", () => {
    expect(heldTime(p.b, 0)).toBe(8);
    expect(heldTime(p.b, 110)).toBe(13);
    expect(heldTime(p.a, 500)).toBe(66);
    expect([deckRole(p.b, 0), deckRole(p.b, 110), deckRole(p.b, 148)]).toEqual(["cued", "playing", "gone"]);
  });

  it("moves at the master's tempo against its own", () => {
    const off = plan("x", "y", { tempo: [kf(0, 126, "hold")] });
    expect(deckRate(off, off.a, 90)).toBeCloseTo(1.05);
    expect(deckRate(off, off.b, 90)).toBe(0);
    expect(timeLeft(off, off.a, 300, 0)).toBeCloseTo(((300 - 42) * 120) / 126, 5); // 84 beats in at its cue
  });

  it("reads the plan's lanes, and a silent fader off the deck's span", () => {
    const d = deck("x", 0, 0, 0, 64, { lanes: { ...plan().a.lanes, eq_low: [kf(0, 0), kf(8, -30)], filter: [kf(0, 0), kf(8, 1)], volume: [kf(0, 0.5, "hold")] } });
    const s = readStrip(d, 4);
    expect(s.eqLow).toBeCloseTo(-15);
    expect(s.filter).toBeCloseTo(0.5);
    expect(s.volume).toBe(0.5);
    expect(readStrip(d, 64).volume).toBe(0);
  });
});

describe("meters", () => {
  const row = (v: number) => new Float32Array(100).fill(v);
  const stems: StemWave = { pps: 10, n: 100, duration: 10, drums: row(1), bass: row(0.5), vocals: row(0.25), other: row(0.8) };
  const wave: WaveData = { pps: 10, n: 100, duration: 10, low: row(1), mid: row(0.5), high: row(0.25) };
  const strip = { volume: 0.5, eqLow: -20, eqMid: 0, eqHigh: 6, filter: 0 };

  it("a stem shows its level by its plan gain and the fader", () => {
    const d = deck("x", 0, 0, 0, 64, { stem_lanes: { gain_drums: [kf(0, 1, "hold")], gain_bass: [kf(0, 0, "hold")], gain_vocals: [kf(0, 0.5, "hold")], gain_other: [kf(0, 1, "hold")] } });
    const out = stemLevels(stems, d, 5, 10, strip, new Float32Array(4));
    expect(Array.from(out)).toEqual([0.5, 0, 0.0625, expect.closeTo(0.4)]);
  });

  it("without stems, the three bands go through the plan's EQ", () => {
    const out = bandLevels(wave, 5, strip, new Float32Array(4));
    expect(out[0]).toBeCloseTo(0.05);
    expect(out[1]).toBeCloseTo(0.25);
    expect(out[2]).toBeCloseTo(0.5 * 0.25 * 10 ** (6 / 20));
  });

  it("is silent past the end of the file", () => {
    expect(bandLevels(wave, 50, strip, new Float32Array(4))[0]).toBe(0);
  });
});

describe("up next", () => {
  const tracks = [track("x"), track("y"), track("z")];
  const next = plan("y", "z", { technique: "drop swap", a: deck("y", 0, 0, 0, 500), b: deck("z", 400, 0, 400, 560), window: { start_beat: 0, end_beat: 560 } });

  it("counts down to the blend, then through it", () => {
    const chain = [plan(), next];
    expect(upNext(tracks, chain, 0, 92)).toMatchObject({ kind: "coming", beats: 8, track: { hash: "y" } });
    expect(upNext(tracks, chain, 0, 120)).toMatchObject({ kind: "blending", beats: 12, track: { hash: "y" } });
  });

  it("after a blend, counts to the next plan on its own timeline", () => {
    const n = upNext(tracks, [plan(), next], 0, 140);
    // at beat 140 this plan has y at 28 s, which is the next plan's beat 56, and its blend starts on beat 400
    expect(n).toMatchObject({ kind: "coming", track: { hash: "z" }, beats: 344 });
  });

  it("says when the set stops, and when this is the last track", () => {
    expect(upNext(tracks, [plan(), null], 0, 140)).toMatchObject({ kind: "stops", track: { hash: "z" } });
    expect(upNext(tracks.slice(0, 2), [plan()], 0, 140)).toEqual({ kind: "last" });
  });

  it("writes bars.beats, up to a whole beat", () => {
    expect(fmtCountdown(30)).toBe("7.2");
    expect(fmtCountdown(29.2)).toBe("7.2");
    expect(fmtCountdown(0)).toBe("0.0");
  });
});

describe("the analysis a deck draws", () => {
  it("uses the track's own grid and sections", () => {
    const an = deckAnalysis({ ...track("x", 120), grid: { bpm: 125, first_downbeat_s: 0.5, beats_per_bar: 4 }, sections: [{ kind: "drop", start_s: 8.18, end_s: 23.54 }, { kind: "main", start_s: 23.54, end_s: 100 }] }, plan().a);
    expect(an.bar_s).toBeCloseTo(1.92);
    expect(an.first_downbeat_s).toBe(0.5);
    expect(an.bar_count).toBe(62);
    expect(an.sections.map((s) => [s.kind, s.start_bar])).toEqual([["drop", 4], ["body", 12]]);
  });

  it("falls back to the plan's grid before the track is analysed", () => {
    const an = deckAnalysis(track("x"), deck("x", 3, 0.75, 0, 64));
    expect(an.bpm).toBe(120);
    expect(an.first_downbeat_s).toBeCloseTo(((0.75 - 1.5) % 2 + 2) % 2);
    expect(an.grid).toEqual(deck("x", 3, 0.75, 0, 64).grid); // its bar lines follow the plan's grid
  });

  it("draws the track's piecewise grid and numbers its phrases on it", () => {
    const segments = [{ start_beat: 0, start_s: 0.5, bpm: 120 }, { start_beat: 32, start_s: 16.5, bpm: 60 }];
    const an = deckAnalysis({ ...track("x", 120), grid: { bpm: 120, first_downbeat_s: 0.5, beats_per_bar: 4, segments }, phrases: [{ start_s: 0.5, level: 32 }, { start_s: 16.5, level: 8 }, { start_s: 48.5, level: 16 }] }, plan().a);
    expect(an.grid).toEqual(segments);
    // bar 8 is at the tempo change; the 60 BPM bars after it are 4 s long
    expect(an.phrases).toEqual([{ bar: 0, level: 32 }, { bar: 8, level: 8 }, { bar: 16, level: 16 }]);
  });
});

describe("the set-wide tempo line", () => {
  it("is the one the export plays (tests/test_seedbox2_setexport.py holds the other side)", () => {
    const { plans, duration, tempo, windows } = fixture as unknown as {
      plans: MixPlan[];
      duration: number;
      tempo: MixPlan["tempo"][];
      windows: MixPlan["window"][];
    };
    const tracks = [...plans.map(() => ({ duration: 0 })), { duration }] as SetSession["tracks"];
    const chain = planChain({ plans, tracks });
    chain.forEach((plan, i) => {
      expect(plan!.window.start_beat).toBeCloseTo(windows[i].start_beat, 6);
      expect(plan!.window.end_beat).toBeCloseTo(windows[i].end_beat, 6);
      expect(plan!.tempo.length).toBe(tempo[i].length);
      plan!.tempo.forEach((k, j) => {
        expect(k.beat).toBeCloseTo(tempo[i][j].beat, 6);
        expect(k.value).toBeCloseTo(tempo[i][j].value, 6);
        expect(k.curve).toBe(tempo[i][j].curve);
      });
    });
  });
});
