import { describe, expect, it } from "vitest";
import { LANE_DEFAULTS, LANES } from "@/api/mixplan.generated";
import type { Deck, Keyframe, Lanes, MixPlan } from "@/api/mixplan.generated";
import type { MixCheck } from "@/api/types";
import type { WaveData } from "@/features/waveform/prepare";
import type { StemWave } from "@/features/waveform/stems";
import { type CheckInput, fixCheck, keySteps, overlapOf, planChecks } from "./checks";
import { applyStyle } from "./styles";

const kf = (beat: number, value: number, curve: Keyframe["curve"] = "linear"): Keyframe => ({ beat, value, curve });
const PPS = 50;
const SECONDS = 400;

function deck(id: string): Deck {
  const lanes = Object.fromEntries(LANES.map((n) => [n, [kf(-64, LANE_DEFAULTS[n])]])) as unknown as Lanes;
  return { track_id: id, grid: [{ start_beat: -64, start_s: 100, bpm: 120 }], key_shift: 0, play_from_beat: -64, play_to_beat: 128, lanes, stem_lanes: null, sends: { echo_beats: 0.75, echo_tap: "pre_eq", reverb_size: 0.75, reverb_tap: "pre_eq" }, loops: [], fx: [], pinned: [] };
}

/** Both decks full up from beat 0 to beat 64, then B alone. */
const blend = (over: Partial<MixPlan> = {}): MixPlan => {
  const a = deck("a");
  const b = deck("b");
  a.lanes.volume = [kf(0, 1), kf(64, 0)];
  b.lanes.volume = [kf(0, 0), kf(0.5, 1)];
  return { version: 3, technique: "t", bars: 16, key_lock: true, window: { start_beat: -64, end_beat: 128 }, tempo: [kf(-64, 120)], a, b, markers: [], notes: [], edited: false, ...over };
};

const plane = (v = 1) => new Float32Array(PPS * SECONDS).fill(v);
const wave = (): WaveData => ({ pps: PPS, n: PPS * SECONDS, duration: SECONDS, low: plane(), mid: plane(), high: plane() });
const stems = (vocals = 1): StemWave => ({ pps: PPS, n: PPS * SECONDS, duration: SECONDS, drums: plane(), bass: plane(), vocals: plane(vocals), other: plane() });

const input = (plan: MixPlan, over: Partial<CheckInput> = {}): CheckInput => ({ plan, waves: { a: wave(), b: wave() }, stems: { a: null, b: null }, keys: { a: "8A", b: "8A" }, ...over });
const check = (i: CheckInput, id: MixCheck["id"]) => planChecks(i).find((c) => c.id === id)!;

describe("the checks", () => {
  it("lists the four the editor shows", () => {
    expect(planChecks(input(blend())).map((c) => c.id)).toEqual(["bass", "vocals", "key", "loudness"]);
  });

  it("finds where the decks overlap, and that a cut has none", () => {
    expect(overlapOf(blend())).toEqual([0.25, 62.75]);
    const p = applyStyle(blend(), { style: "quick_cut", bars: 1, smooth: 0.5, fx: 0 }, { stems: { a: false, b: false }, base: blend() });
    expect(overlapOf(p)).toBeNull();
    for (const c of planChecks(input(p))) expect(c.status).toBe("pass");
  });

  it("says nothing it can't know while the waveforms aren't there", () => {
    const i = input(blend(), { waves: { a: null, b: null } });
    expect(check(i, "bass").status).toBe("na");
    expect(check(i, "loudness").status).toBe("na");
  });

  it("needs both tracks' stems to speak about vocals, and doesn't count a stem that isn't there", () => {
    expect(check(input(blend()), "vocals").status).toBe("na");
    const heard = input(blend(), { stems: { a: stems(), b: stems() } });
    expect(check(heard, "vocals").status).toBe("fail");
    const leak = input(blend(), { stems: { a: stems(0.02), b: stems() } });
    expect(check(leak, "vocals").status).toBe("pass");
  });

  it("grades the time both basses play with mixmetrics' limits", () => {
    const fail = check(input(blend()), "bass");
    expect(fail.status).toBe("fail");
    expect(fail.value).toBe("28.8 s");

    const plan = blend();
    plan.a.lanes.eq_low = [kf(8, 0), kf(8, -30)];
    plan.b.lanes.eq_low = [kf(7, -30), kf(7, 0)]; // a beat of both: half a second, the edge of a pass
    expect(check(input(plan), "bass").value).toBe("0.5 s");
    expect(check(input(plan), "bass").status).toBe("pass");
    plan.b.lanes.eq_low = [kf(6, -30), kf(6, 0)];
    expect(check(input(plan), "bass").status).toBe("warn");
  });

  it("hears a stem lane at zero as silence with stems", () => {
    const plan = blend();
    plan.a.stem_lanes = { gain_drums: [kf(0, 1)], gain_bass: [kf(0, 0)], gain_vocals: [kf(0, 0)], gain_other: [kf(0, 1)] };
    const i = input(plan, { stems: { a: stems(), b: stems() } });
    expect(check(i, "bass").value).toBe("0.0 s");
    expect(check(i, "vocals").value).toBe("0.0 s");
  });

  it("grades the stretch against the deck's own tempo, and the keys on the wheel", () => {
    const plan = blend({ tempo: [kf(-64, 126)] }); // A and B at 120: 5%
    const c = check(input(plan), "key");
    expect(c.status).toBe("warn");
    expect(c.value).toContain("stretch 5.0%");
    expect(check(input(blend({ tempo: [kf(-64, 132)] })), "key").status).toBe("fail");
    expect(check(input(blend()), "key").status).toBe("pass");
    expect(check(input(blend(), { keys: { a: "8A", b: "11A" } }), "key").status).toBe("fail");
    const unknown = check(input(blend(), { keys: { a: null, b: "11A" } }), "key");
    expect(unknown.value).toBe("stretch 0.0%");
    expect(unknown.detail).toContain("Keys unknown");
  });

  it("measures the wheel with the relative at no distance and a shift on the way", () => {
    expect(keySteps("8A", "8A")).toBe(0);
    expect(keySteps("8A", "8B")).toBe(0);
    expect(keySteps("8A", "9A")).toBe(1);
    expect(keySteps("8A", "9B")).toBe(2);
    expect(keySteps("1A", "12A")).toBe(1);
    expect(keySteps("8A", "1A", 1)).toBe(0); // up a semitone is 7 round the wheel
    expect(keySteps(null, "1A")).toBeNull();
  });

  it("sees the bump of two full decks and the dip of a hole between them", () => {
    const bump = check(input(blend()), "loudness");
    expect(bump.status).toBe("warn");
    expect(bump.value).toMatch(/\+2\.7 LU$/);

    const hole = blend();
    hole.a.lanes.volume = [kf(0, 1), kf(16, 0.2), kf(32, 0)];
    hole.b.lanes.volume = [kf(0, 0.02), kf(16, 0.2), kf(32, 1)];
    expect(check(input(hole), "loudness").status).toBe("fail");
    expect(check(input(hole), "loudness").value.startsWith("-")).toBe(true);
  });
});

describe("the fixes", () => {
  it("bass: one hand-off on a bar, and the check passes", () => {
    const i = input(blend());
    const fixed = fixCheck("bass", i)!;
    expect(check({ ...i, plan: fixed }, "bass").status).toBe("pass");
    const at = fixed.b.lanes.eq_low[0].beat;
    expect(at % 4).toBe(0);
    expect(fixed.a.lanes.eq_low[fixed.a.lanes.eq_low.length - 1]).toEqual(kf(at, -30));
    expect(fixed.edited).toBe(true);
    expect(fixed.a.pinned).toContain("eq_low");
  });

  it("bass with stems: on the stem lanes, whether or not the deck had any", () => {
    const i = input(blend(), { stems: { a: stems(), b: stems() } });
    const fixed = fixCheck("bass", i)!;
    expect(check({ ...i, plan: fixed }, "bass").status).toBe("pass");
    expect(fixed.a.stem_lanes!.gain_bass[fixed.a.stem_lanes!.gain_bass.length - 1].value).toBe(0);
    expect(fixed.a.lanes.eq_low).toEqual(i.plan.a.lanes.eq_low);
  });

  it("vocals: on the stem lanes; without stems there's nothing it can do", () => {
    const i = input(blend(), { stems: { a: stems(), b: stems() } });
    const fixed = fixCheck("vocals", i)!;
    expect(check({ ...i, plan: fixed }, "vocals").status).toBe("pass");
    expect(fixCheck("vocals", input(blend()))).toBeNull();
  });

  it("key: shifts B by the fewest semitones that bring the keys together", () => {
    const i = input(blend(), { keys: { a: "8A", b: "1A" } });
    expect(check(i, "key").status).toBe("fail");
    const fixed = fixCheck("key", i)!;
    expect(fixed.b.key_shift).toBe(1);
    expect(check({ ...i, plan: fixed }, "key").status).toBe("pass");
  });

  it("key: an unworkable key is left, and the same plan comes back when nothing can improve", () => {
    const fine = input(blend());
    expect(fixCheck("key", fine)).toBe(fine.plan);
  });

  it("tempo: meets in the middle, so neither deck takes all of the stretch", () => {
    const plan = blend({ tempo: [kf(-64, 120)] });
    plan.b.grid = [{ start_beat: -64, start_s: 100, bpm: 132 }];
    const i = input(plan);
    expect(check(i, "key").value).toContain("stretch 9.1%");
    const fixed = fixCheck("key", i)!;
    expect(fixed.tempo[0].value).toBeCloseTo(Math.sqrt(120 * 132));
    expect(check({ ...i, plan: fixed }, "key").value).toContain("stretch 4.9%");
    expect(fixed.key_lock).toBe(true);
  });

  it("loudness: rides out the bump and the dip, with the kills left alone", () => {
    const bumpy = input(blend());
    const down = fixCheck("loudness", bumpy)!;
    expect(check({ ...bumpy, plan: down }, "loudness").status).toBe("pass");

    const hole = blend();
    hole.a.lanes.volume = [kf(0, 1), kf(16, 0.4), kf(32, 0)];
    hole.b.lanes.volume = [kf(0, 0.02), kf(16, 0.4), kf(32, 1)];
    hole.a.lanes.eq_low = [kf(-64, -30)];
    const i = input(hole);
    const before = check(i, "loudness").value;
    const up = fixCheck("loudness", i)!;
    const after = check({ ...i, plan: up }, "loudness").value;
    expect(Number(after.split(" ")[0])).toBeGreaterThan(Number(before.split(" ")[0]));
    expect(up.a.lanes.eq_low).toEqual(hole.a.lanes.eq_low);
    for (const lane of [up.a.lanes.volume, up.b.lanes.volume, up.a.lanes.eq_mid, up.b.lanes.eq_high]) {
      lane.forEach((k, n) => {
        if (n) expect(k.beat).toBeGreaterThanOrEqual(lane[n - 1].beat);
      });
    }
    for (const k of up.a.lanes.eq_mid) expect(k.value).toBeLessThanOrEqual(6);
  });

  it("does nothing for a check it has no answer to", () => {
    expect(fixCheck("beat", input(blend()))).toBeNull();
    expect(fixCheck("bass", input(blend(), { waves: { a: null, b: null } }))).toBeNull();
  });
});
