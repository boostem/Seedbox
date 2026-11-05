import { describe, expect, it } from "vitest";
import { LANE_DEFAULTS, LANES } from "@/api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan } from "@/api/mixplan.generated";
import { laneValue } from "@/features/waveform/stems";
import { applyStyle, fileEndBeat, LENGTHS, planRibbon, settingsOf, STYLES } from "./styles";

const kf = (beat: number, value: number): Keyframe => ({ beat, value, curve: "linear" });

function deck(id: string): Deck {
  const lanes = Object.fromEntries(LANES.map((n) => [n, [kf(0, LANE_DEFAULTS[n])]])) as unknown as Lanes;
  return { track_id: id, grid: [{ start_beat: 0, start_s: 60, bpm: 120 }], key_shift: 0, play_from_beat: -32, play_to_beat: 64, lanes, stem_lanes: null, sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "pre_eq" }, loops: [{ start_beat: 0, end_beat: 4, source_beat: 0, length_beats: 1 }], fx: [], pinned: ["volume"] };
}

const plan = (): MixPlan => ({ version: 3, technique: "long blend", bars: 8, key_lock: true, window: { start_beat: -32, end_beat: 64 }, tempo: [kf(0, 120)], a: deck("a"), b: deck("b"), markers: [], notes: [], edited: false });

const context = (stems = false) => ({ stems: { a: stems, b: stems }, base: plan() });

/** What `validate` in seedbox2/mix/plan.py asks of what the sliders write. */
function sound(p: MixPlan): void {
  for (const d of [p.a, p.b]) {
    expect(d.play_from_beat).toBeLessThan(d.play_to_beat);
    const lanes = [...Object.values(d.lanes), ...Object.values(d.stem_lanes ?? {})] as Keyframe[][];
    for (const lane of lanes) {
      expect(lane.length).toBeGreaterThan(0);
      lane.forEach((k, i) => {
        expect(Number.isFinite(k.value)).toBe(true);
        if (i) expect(k.beat).toBeGreaterThanOrEqual(lane[i - 1].beat);
      });
    }
  }
  expect(p.bars).toBeGreaterThanOrEqual(1);
}

describe("the Simple view's styles", () => {
  it("writes a sound plan for every style, length and extreme, with stems and without", () => {
    for (const s of STYLES) {
      for (const bars of LENGTHS) {
        for (const [smooth, fx] of [[0, 0], [1, 1], [0, 1], [1, 0]]) {
          for (const stems of [false, true]) sound(applyStyle(plan(), { style: s.id, bars, smooth, fx }, context(stems)));
        }
      }
    }
  });

  it("marks the plan edited and starts from a clean deck", () => {
    const p = applyStyle(plan(), { style: "bass_swap", bars: 8, smooth: 0.5, fx: 0.5 }, context());
    expect(p.edited).toBe(true);
    expect(p.technique).toBe("bass swap");
    expect(p.a.loops).toEqual([]);
    expect(p.a.pinned).toEqual([]);
  });

  it("takes A out and brings B in over the length asked for", () => {
    const p = applyStyle(plan(), { style: "long_blend", bars: 16, smooth: 1, fx: 0 }, context());
    expect(laneValue(p.a.lanes.volume, 0)).toBe(1);
    expect(laneValue(p.a.lanes.volume, 64)).toBe(0);
    expect(laneValue(p.b.lanes.volume, 0)).toBe(0);
    expect(laneValue(p.b.lanes.volume, 64)).toBe(1);
  });

  it("hands the bass over on the stem lanes with stems, on the lows without", () => {
    const withStems = applyStyle(plan(), { style: "bass_swap", bars: 8, smooth: 0, fx: 0 }, context(true));
    expect(laneValue(withStems.a.stem_lanes!.gain_bass, 15)).toBe(1);
    expect(laneValue(withStems.a.stem_lanes!.gain_bass, 17)).toBe(0);
    expect(laneValue(withStems.b.stem_lanes!.gain_bass, 15)).toBe(0);
    expect(laneValue(withStems.b.stem_lanes!.gain_bass, 17)).toBe(1);
    expect(withStems.a.lanes.eq_low).toHaveLength(1);

    const without = applyStyle(plan(), { style: "bass_swap", bars: 8, smooth: 0, fx: 0 }, context(false));
    expect(without.a.stem_lanes).toBeNull();
    expect(laneValue(without.a.lanes.eq_low, 17)).toBe(-30);
    expect(laneValue(without.b.lanes.eq_low, 15)).toBe(-30);
  });

  it("never has the two vocals up together with stems", () => {
    const p = applyStyle(plan(), { style: "long_blend", bars: 16, smooth: 1, fx: 0 }, context(true));
    for (let beat = -8; beat < 72; beat += 0.25) {
      expect(Math.min(laneValue(p.a.stem_lanes!.gain_vocals, beat), laneValue(p.b.stem_lanes!.gain_vocals, beat))).toBe(0);
    }
  });

  it("smooth widens the swap; FX 0 leaves no effects and more FX sends more", () => {
    const swapBeats = (smooth: number) => {
      const lane = applyStyle(plan(), { style: "bass_swap", bars: 8, smooth, fx: 0 }, context()).a.lanes.eq_low;
      return lane[lane.length - 1].beat - lane[0].beat;
    };
    expect(swapBeats(0)).toBe(0);
    expect(swapBeats(1)).toBeGreaterThan(swapBeats(0.5));

    const send = (fx: number) => Math.max(...applyStyle(plan(), { style: "echo_out", bars: 4, smooth: 0.5, fx }, context()).a.lanes.echo_send.map((k) => k.value));
    expect(send(0)).toBe(0);
    expect(send(1)).toBeGreaterThan(send(0.5));
    const flat = applyStyle(plan(), { style: "filter_sweep", bars: 8, smooth: 0.5, fx: 0 }, context());
    expect(flat.a.lanes.filter).toHaveLength(1);
  });

  it("cuts at beat 0, with the length as the lead-in", () => {
    const p = applyStyle(plan(), { style: "quick_cut", bars: 2, smooth: 0.5, fx: 1 }, context());
    expect(laneValue(p.a.lanes.volume, -0.01)).toBe(1);
    expect(laneValue(p.a.lanes.volume, 0)).toBe(0);
    expect(laneValue(p.b.lanes.volume, -0.01)).toBe(0);
    expect(laneValue(p.b.lanes.volume, 0)).toBe(1);
    expect(p.b.play_from_beat).toBe(0);
  });

  it("starts a blend earlier when A's file would run out, on a bar", () => {
    const base = plan();
    expect(fileEndBeat(base.a, 60 + 30)).toBe(60);
    const p = applyStyle(base, { style: "long_blend", bars: 32, smooth: 0.5, fx: 0 }, { ...context(), aDuration: 90 });
    expect(laneValue(p.a.lanes.volume, 60)).toBe(0);
    expect(p.b.play_from_beat).toBe(60 - 128);
    expect(p.window.start_beat).toBeLessThan(-68);
  });

  it("keeps the timeline at least the size it came as, so a slider doesn't rescale it", () => {
    const p = applyStyle(plan(), { style: "bass_swap", bars: 1, smooth: 0.5, fx: 0 }, context());
    expect(p.window).toEqual({ start_beat: -32, end_beat: 64 });
  });

  it("a plan with no sends yet starts at the middle of the FX slider, so a style's effects are there when you pick it", () => {
    expect(settingsOf(plan()).fx).toBe(0.5);
  });

  it("remembers what made a plan, and reads the rest from the plan", () => {
    const settings = { style: "echo_out" as const, bars: 4, smooth: 0.2, fx: 0.7 };
    expect(settingsOf(applyStyle(plan(), settings, context()))).toEqual(settings);
    const other = settingsOf({ ...plan(), technique: "something new", bars: 12 });
    expect(other.style).toBe("long_blend");
    expect(other.bars).toBe(12);
  });
});

describe("the ribbon", () => {
  it("tells the plan as pieces that cover the whole timeline", () => {
    const p = applyStyle(plan(), { style: "bass_swap", bars: 8, smooth: 0.5, fx: 0 }, context());
    const pieces = planRibbon(p);
    expect(pieces[0].from).toBe(p.window.start_beat);
    expect(pieces[pieces.length - 1].to).toBe(p.window.end_beat);
    pieces.forEach((piece, i) => {
      if (i) expect(piece.from).toBe(pieces[i - 1].to);
    });
    const kinds = pieces.map((x) => x.kind);
    expect(kinds[0]).toBe("a");
    expect(kinds).toContain("swap");
    expect(kinds[kinds.length - 1]).toBe("b");
  });

  it("doesn't call a fade a bass swap", () => {
    const fades = plan();
    fades.a.lanes.volume = [kf(0, 1), kf(32, 0)];
    fades.b.lanes.volume = [kf(0, 0), kf(16, 1)];
    expect(planRibbon(fades).map((x) => x.kind)).toEqual(["a", "both", "b"]);
  });

  it("marks the effects, and shows a cut as no overlap", () => {
    const echo = planRibbon(applyStyle(plan(), { style: "filter_sweep", bars: 8, smooth: 1, fx: 1 }, context())).map((x) => x.kind);
    expect(echo).toContain("fx");
    const cut = planRibbon(applyStyle(plan(), { style: "quick_cut", bars: 1, smooth: 0.5, fx: 0 }, context())).map((x) => x.kind);
    expect(cut).toEqual(["a", "b"]);
  });
});
