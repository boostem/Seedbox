"""Seedbox 2's mix plan (#51): the schema and its validation, deck time on piecewise grids, stem lanes falling back to
EQ, the TypeScript types, and every Seedbox 1 snapshot pair converted to it.

The summaries of those conversions are compared with tests/snapshots/plan_v3_summaries.txt: regenerate it with
`UPDATE_SNAPSHOTS=1 python -m pytest tests/test_seedbox2_mix_plan.py` and commit it with the change."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import numpy as np
import pytest

from musicreco import mixplan as sb1
from seedbox2.analysis.grid import Grid, Segment
from seedbox2.mix import convert, typescript
from seedbox2.mix import plan as v3

from .fixtures import PAIRS, analysis, track

SNAPSHOT = Path(__file__).parent / "snapshots" / "plan_v3_summaries.txt"
CURVE = {"linear": "l", "hold": "h", "ease": "e"}


def _kf(value: float, beat: float = 0.0, curve: str = "hold") -> dict:
    return {"beat": beat, "value": value, "curve": curve}


def _deck(track_id: str = "a", **over: object) -> dict:
    lanes = {name: [_kf(v3.LANE_DEFAULTS[name])] for name in v3.LANES}
    return {"track_id": track_id, "grid": [{"start_beat": 0.0, "start_s": 10.0, "bpm": 120.0}],
            "play_from_beat": -32.0, "play_to_beat": 64.0, "lanes": lanes, **over}  # fmt: skip


def _plan(**over: object) -> dict:
    return {"technique": "long blend", "bars": 16, "window": {"start_beat": -32.0, "end_beat": 64.0},
            "tempo": [_kf(120.0)], "a": _deck("a"), "b": _deck("b"), **over}  # fmt: skip


def _stems(**over: list[dict]) -> dict:
    return {**{name: [_kf(1.0)] for name in v3.STEM_LANES}, **over}


def _s1_pairs() -> list[tuple[str, dict]]:
    return [(name, sb1.generate(track(a), analysis(a), track(b), analysis(b), "folder")) for name, a, b in PAIRS]


# --- the schema ------------------------------------------------------------------------------------------------------


def test_a_minimal_plan_fills_in_its_defaults_and_round_trips_as_json() -> None:
    plan = v3.validate(_plan())
    assert plan.version == v3.PLAN_VERSION
    assert (
        plan.a.key_shift == 0 and plan.a.stem_lanes is None and plan.a.loops == [] and plan.a.sends.echo_beats == 0.75
    )
    assert v3.validate(json.loads(plan.model_dump_json())) == plan


def test_keyframes_loops_effects_and_markers_come_out_sorted() -> None:
    lanes = {**_deck()["lanes"], "volume": [_kf(0.0, 8.0), _kf(1.0, 0.0), _kf(0.5, 8.0)]}
    loops = [{"start_beat": b, "end_beat": b + 4.0, "source_beat": 0.0, "length_beats": 4.0} for b in (8.0, 0.0)]
    fx = [{"type": "brake", "start_beat": b, "end_beat": b + 1.0} for b in (8.0, 0.0)]
    plan = v3.validate(_plan(a=_deck(lanes=lanes, loops=loops, fx=fx),
                             markers=[{"beat": 2.0, "label": "y"}, {"beat": 1.0, "label": "x"}]))  # fmt: skip
    assert [k.value for k in plan.a.lanes.volume] == [1.0, 0.0, 0.5]  # the step keeps its order
    assert [lp.start_beat for lp in plan.a.loops] == [0.0, 8.0]
    assert [ev.start_beat for ev in plan.a.fx] == [0.0, 8.0]
    assert [m.label for m in plan.markers] == ["x", "y"]


@pytest.mark.parametrize(("over", "message"), [
    ({"tempo": [_kf(20.0)]}, "tempo"),
    ({"tempo": []}, "at least 1"),
    ({"window": {"start_beat": -64.0, "end_beat": 64.0}}, "window"),
    ({"window": {"start_beat": 0.0, "end_beat": 0.5}}, "window"),
    ({"a": _deck(play_from_beat=64.0)}, "play_from_beat"),
    ({"a": _deck(key_shift=3)}, "key_shift"),
    ({"a": _deck(key_shift=0.5)}, "key_shift"),
    ({"a": _deck(track_id="")}, "track_id"),
    ({"a": _deck(unknown=1)}, "unknown"),
    ({"a": _deck(fx=[{"type": "flange", "start_beat": 0.0, "end_beat": 1.0}])}, "flange"),
    ({"a": _deck(fx=[{"type": "brake", "start_beat": 0.0, "end_beat": 32.0}])}, "at most 16"),
    ({"a": _deck(fx=[{"type": "stutter", "start_beat": 0.0, "end_beat": 1.0, "div": 2.0}])}, "div"),
    ({"bars": "16"}, "bars"),
    ({"version": 2}, "version"),
])  # fmt: skip
def test_what_is_not_a_plan_is_refused(over: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        v3.validate(_plan(**over))


@pytest.mark.parametrize("raw", [None, [], "plan", {}])
def test_garbage_is_refused(raw: object) -> None:
    with pytest.raises(ValueError):
        v3.validate(raw)


def test_a_non_finite_number_is_refused() -> None:
    with pytest.raises(ValueError):
        v3.validate(_plan(tempo=[_kf(float("nan"))]))


@pytest.mark.parametrize("lane", v3.LANES)
def test_every_lane_is_held_to_its_range(lane: str) -> None:
    lo, hi = v3.LANE_RANGES[lane]
    for value in (lo, hi):
        v3.validate(_plan(a=_deck(lanes={**_deck()["lanes"], lane: [_kf(value)]})))
    for value in (lo - 0.01, hi + 0.01):
        with pytest.raises(ValueError, match=lane):
            v3.validate(_plan(a=_deck(lanes={**_deck()["lanes"], lane: [_kf(value)]})))


@pytest.mark.parametrize("lane", v3.STEM_LANES)
def test_every_stem_lane_is_held_to_its_range(lane: str) -> None:
    v3.validate(_plan(a=_deck(stem_lanes=_stems(**{lane: [_kf(0.0)]}))))  # 0 is silence, and allowed
    with pytest.raises(ValueError, match=lane):
        v3.validate(_plan(a=_deck(stem_lanes=_stems(**{lane: [_kf(1.5)]}))))


def test_a_stem_lane_set_has_all_four_stems() -> None:
    stems = _stems()
    del stems["gain_other"]
    with pytest.raises(ValueError, match="gain_other"):
        v3.validate(_plan(a=_deck(stem_lanes=stems)))


def test_loops_are_on_whole_beats() -> None:
    good = {"start_beat": 0.0, "end_beat": 8.0, "source_beat": -4.0, "length_beats": 4.0}
    v3.validate(_plan(a=_deck(loops=[good])))
    v3.validate(_plan(a=_deck(loops=[{**good, "length_beats": 0.5}])))  # a roll, started on a beat
    for bad in ({"start_beat": 0.5}, {"end_beat": 7.5}, {"source_beat": 0.25}, {"length_beats": 1.5},
                {"length_beats": 0.3}, {"length_beats": 0.0}, {"end_beat": 0.0}):  # fmt: skip
        with pytest.raises(ValueError):
            v3.validate(_plan(a=_deck(loops=[{**good, **bad}])))


def test_loops_and_effects_of_a_deck_cannot_overlap() -> None:
    lp = {"start_beat": 0.0, "end_beat": 8.0, "source_beat": 0.0, "length_beats": 4.0}
    with pytest.raises(ValueError, match="overlap"):
        v3.validate(_plan(a=_deck(loops=[lp, {**lp, "start_beat": 4.0, "end_beat": 12.0}])))
    ev = {"type": "brake", "start_beat": 0.0, "end_beat": 4.0}
    with pytest.raises(ValueError, match="overlap"):
        v3.validate(_plan(a=_deck(fx=[ev, {**ev, "start_beat": 2.0, "end_beat": 6.0}])))


# --- deck time on a piecewise grid -----------------------------------------------------------------------------------

STEP_GRID = [
    {"start_beat": 0.0, "start_s": 10.0, "bpm": 120.0},  # 0.5 s a beat
    {"start_beat": 8.0, "start_s": 14.0, "bpm": 60.0},  # 1 s a beat
    {"start_beat": 12.0, "start_s": 18.0, "bpm": 240.0},  # 0.25 s a beat
]


def test_deck_time_reads_each_beat_from_its_own_segment() -> None:
    deck = v3.validate(_plan(a=_deck(grid=STEP_GRID))).a
    beats = np.array([-2.0, 0.0, 4.0, 8.0, 10.0, 12.0, 16.0])
    assert v3.deck_time(deck, beats) == pytest.approx([9.0, 10.0, 12.0, 14.0, 16.0, 18.0, 19.0])
    assert v3.deck_time(deck, 10.0) == pytest.approx(16.0)  # a scalar stays a scalar
    assert isinstance(v3.deck_time(deck, 10.0), float)
    assert v3.deck_bpm(deck, beats) == pytest.approx([120.0, 120.0, 120.0, 60.0, 60.0, 240.0, 240.0])


def test_a_loop_reads_its_source_from_the_segment_it_is_in() -> None:
    loop = {"start_beat": 0.0, "end_beat": 4.0, "source_beat": 8.0, "length_beats": 2.0}
    deck = v3.validate(_plan(a=_deck(grid=STEP_GRID, loops=[loop]))).a
    # in the loop: beats 8, 9 of the 60 BPM segment, over and over; out of it: where it would have been (slip)
    assert v3.deck_time(deck, np.array([0.0, 1.0, 2.0, 3.0, 4.0])) == pytest.approx([14.0, 15.0, 14.0, 15.0, 12.0])
    assert v3.deck_bpm(deck, 1.0) == pytest.approx(60.0)


def test_grid_segments_must_meet_and_increase() -> None:
    gap = [STEP_GRID[0], {**STEP_GRID[1], "start_s": 14.5}]
    with pytest.raises(ValueError, match="continuous"):
        v3.validate(_plan(a=_deck(grid=gap)))
    with pytest.raises(ValueError, match="sorted"):
        v3.validate(_plan(a=_deck(grid=[STEP_GRID[1], STEP_GRID[0]])))
    with pytest.raises(ValueError):
        v3.validate(_plan(a=_deck(grid=[])))
    with pytest.raises(ValueError, match="bpm"):
        v3.validate(_plan(a=_deck(grid=[{**STEP_GRID[0], "bpm": 10.0}])))


def test_an_analysed_grid_places_on_the_master_timeline() -> None:
    """Rounded by Grid.to_json, half-time and off zero, it still validates, and the deck reads the analysed time."""
    t1 = 1.2345678 + 64 * 60 / 87.123456
    grid = Grid([Segment(0.0, 1.2345678, 87.123456), Segment(64.0, t1, 90.654321),
                 Segment(128.0, t1 + 64 * 60 / 90.654321, 88.5)])  # fmt: skip
    placed = grid.scaled(2.0, offset=32.0)
    deck = v3.validate(_plan(a=_deck(grid=placed.to_json()))).a
    assert v3.deck_time(deck, 0.0) == pytest.approx(grid.time_at(32.0), abs=1e-3)
    assert v3.deck_time(deck, 100.0) == pytest.approx(grid.time_at(82.0), abs=1e-3)


# --- stem lanes without stems ----------------------------------------------------------------------------------------


def test_a_deck_with_stems_keeps_its_stem_lanes() -> None:
    raw = _plan(a=_deck(stem_lanes=_stems(gain_bass=[_kf(0.0)])))
    plan = v3.validate(raw, has_stems=lambda _track: True)
    assert plan.a.stem_lanes is not None and plan.a.lanes.eq_low[0].value == 0.0 and plan.notes == []


def test_validate_without_has_stems_leaves_the_plan_as_stored() -> None:
    plan = v3.validate(_plan(a=_deck(stem_lanes=_stems(gain_bass=[_kf(0.0)]))))
    assert plan.a.stem_lanes is not None


def test_stem_lanes_fall_back_to_eq_on_the_deck_without_stems_only() -> None:
    bass = [_kf(1.0, 0.0), _kf(0.0, 32.0)]
    raw = _plan(a=_deck("a", stem_lanes=_stems(gain_bass=bass)), b=_deck("b", stem_lanes=_stems(gain_bass=bass)))
    plan = v3.validate(raw, has_stems=lambda track_id: track_id == "b")
    assert plan.a.stem_lanes is None and plan.b.stem_lanes is not None
    assert v3.lane_value(plan.a.lanes.eq_low, np.array([0.0, 31.0, 32.0, 40.0])) == pytest.approx([0, 0, -30, -30])
    assert plan.b.lanes.eq_low == v3.validate(raw).b.lanes.eq_low
    assert plan.a.lanes.eq_mid == v3.validate(raw).a.lanes.eq_mid
    assert any("Deck A has no stems" in n for n in plan.notes)


def test_each_stem_goes_to_its_band_and_other_goes_nowhere() -> None:
    half = [_kf(0.5)]
    raw = _plan(a=_deck(stem_lanes=_stems(gain_bass=half, gain_vocals=[_kf(0.25)], gain_drums=[_kf(0.1)],
                                          gain_other=[_kf(0.0)])))  # fmt: skip
    a = v3.validate(raw, has_stems=lambda _track: False).a
    assert a.lanes.eq_low[0].value == pytest.approx(-6.02, abs=0.01)
    assert a.lanes.eq_mid[0].value == pytest.approx(-12.04, abs=0.01)
    assert a.lanes.eq_high[0].value == pytest.approx(-20.0, abs=0.01)
    assert a.lanes.volume == v3.validate(raw).a.lanes.volume


def test_a_fold_adds_to_the_eq_already_there_within_the_band_range() -> None:
    eq = [_kf(-12.0, 0.0, "linear"), _kf(-12.0, 8.0, "hold"), _kf(0.0, 16.0)]  # 16 ends a hold: a step
    lanes = {**_deck()["lanes"], "eq_low": eq}
    gain = [_kf(1.0, 0.0, "linear"), _kf(0.5, 8.0, "hold"), _kf(0.0, 32.0)]
    a = v3.validate(_plan(a=_deck(lanes=lanes, stem_lanes=_stems(gain_bass=gain))), has_stems=lambda _t: False).a

    def at(beat: float) -> float:
        return float(v3.lane_value(a.lanes.eq_low, beat))

    assert at(0.0) == pytest.approx(-12.0)
    assert at(4.0) == pytest.approx(-12.0 + 20 * np.log10(0.75), abs=0.02)  # the gain's linear ramp, in dB
    assert at(8.0) == pytest.approx(-18.02, abs=0.01)
    assert at(15.9) == pytest.approx(-18.02, abs=0.01)  # steps stay steps ...
    assert at(16.0) == pytest.approx(-6.02, abs=0.01)
    assert at(32.0) == pytest.approx(-30.0)  # ... and the sum stops at the band's kill


def test_stem_lanes_left_up_fall_back_silently() -> None:
    plan = v3.validate(_plan(a=_deck(stem_lanes=_stems())), has_stems=lambda _track: False)
    assert plan.a.stem_lanes is None and plan.notes == []
    assert plan.a.lanes == v3.validate(_plan()).a.lanes


# --- the TypeScript types --------------------------------------------------------------------------------------------


def test_the_typescript_types_are_up_to_date() -> None:
    assert typescript.OUTPUT.read_text("utf-8") == typescript.render(), "rerun: python -m seedbox2.mix.typescript"


def test_the_typescript_types_cover_the_plan() -> None:
    text = typescript.render()
    assert "export interface MixPlan {" in text
    assert "  stem_lanes: StemLanes | null;" in text
    assert "  fx: (StopFx | StutterFx)[];" in text
    assert '  type: "spinback" | "brake";' in text


def test_the_json_schema_describes_the_plan() -> None:
    schema = v3.json_schema()
    assert schema["title"] == "MixPlan"
    assert set(schema["$defs"]["StemLanes"]["properties"]) == set(v3.STEM_LANES)
    assert set(schema["$defs"]["Lanes"]["properties"]) == set(v3.LANES)
    assert {"grid", "key_shift", "stem_lanes"} <= set(schema["$defs"]["Deck"]["properties"])


# --- Seedbox 1 plans -------------------------------------------------------------------------------------------------


def _summary(plan: v3.MixPlan) -> list[str]:
    out = [f"{plan.technique}, {plan.bars} bars, window {plan.window.start_beat:g}..{plan.window.end_beat:g}, "
           f"tempo {_lane(plan.tempo)}"]  # fmt: skip
    for name in ("a", "b"):
        deck: v3.Deck = getattr(plan, name)
        grid = " ".join(f"{s.bpm:g}@{s.start_beat:g}={s.start_s:.3f}s" for s in deck.grid)
        out.append(f"{name.upper()} {deck.track_id}: play {deck.play_from_beat:g}..{deck.play_to_beat:g}, grid {grid}, "
                   f"echo {deck.sends.echo_beats:g}")  # fmt: skip
        out += [f"  {lane}: {_lane(getattr(deck.lanes, lane))}" for lane in v3.LANES
                if len(getattr(deck.lanes, lane)) > 1]  # fmt: skip
        out += [f"  loop {lp.start_beat:g}..{lp.end_beat:g} from {lp.source_beat:g} every {lp.length_beats:g}"
                for lp in deck.loops]  # fmt: skip
        out += [f"  {ev.type} {ev.start_beat:g}..{ev.end_beat:g}" for ev in deck.fx]
    return out


def _lane(keyframes: list[v3.Keyframe]) -> str:
    return " ".join(f"{k.value:g}@{k.beat:g}{CURVE[k.curve]}" for k in keyframes)


def test_every_seedbox1_snapshot_pair_converts_to_the_golden_file() -> None:
    out = []
    for name, s1 in _s1_pairs():
        out += [f"## {name}", *_summary(convert.from_seedbox1(s1)), ""]
    text = "\n".join(out)
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(text, encoding="utf-8")
    assert text == SNAPSHOT.read_text(encoding="utf-8"), "converted plans changed: rerun with UPDATE_SNAPSHOTS=1"


def test_the_golden_file_has_every_pair_of_the_seedbox1_snapshot() -> None:
    names = [ln[3:] for ln in SNAPSHOT.read_text("utf-8").splitlines() if ln.startswith("## ")]
    s1 = (SNAPSHOT.parent / "plan_summaries.txt").read_text("utf-8")
    assert names == [ln[3:] for ln in s1.splitlines() if ln.startswith("## ")]
    assert len(names) == len(PAIRS)


@pytest.mark.parametrize(("name", "s1"), _s1_pairs(), ids=[p[0] for p in PAIRS])
def test_a_converted_plan_plays_like_seedbox1(name: str, s1: dict) -> None:
    plan = convert.from_seedbox1(s1)
    assert v3.validate(json.loads(plan.model_dump_json())) == plan, name
    beats = np.arange(s1["window"]["start_beat"] - 4.0, s1["window"]["end_beat"] + 4.0, 1 / 16)
    assert v3.lane_value(plan.tempo, beats) == pytest.approx(sb1.lane_value(s1["tempo"], beats))
    for deck, old in ((plan.a, s1["a"]), (plan.b, s1["b"])):
        assert deck.track_id == old["track_id"]
        assert v3.deck_time(deck, beats) == pytest.approx(sb1.deck_time(old, beats))
        assert v3.deck_bpm(deck, beats) == pytest.approx(sb1.deck_bpm(old, beats))
        for lane in sb1.LANES:
            ours, theirs = v3.lane_value(getattr(deck.lanes, lane), beats), sb1.lane_value(old["lanes"][lane], beats)
            assert ours == pytest.approx(theirs), lane


def test_every_technique_seedbox1_can_plan_converts_without_moving_a_loop() -> None:
    """Loop rolls have the ½-beat loops that made `length_beats` allow divisions. No generated plan is off the beat."""
    converted, looped = 0, 0
    for technique in sb1.TECHNIQUES:
        for _name, a, b in PAIRS:
            try:
                s1 = sb1.generate(track(a), analysis(a), track(b), analysis(b), "f", technique=technique)
            except ValueError:  # this pair can't do this technique
                continue
            plan = convert.from_seedbox1(s1)
            assert plan.technique == s1["technique"]
            assert not [n for n in plan.notes if "Off-beat loops" in n], (technique, _name)
            converted += 1
            looped += any(lp.length_beats < 1.0 for lp in plan.a.loops)
    assert converted > 100 and looped > 0


def _edited() -> dict:
    _name, a, b = PAIRS[0]
    s1 = copy.deepcopy(sb1.generate(track(a), analysis(a), track(b), analysis(b), "folder"))
    s1["edited"] = True
    s1["a"]["lanes"]["volume"] = [_kf(0.25, -8.0, "ease"), _kf(0.75, 2.5, "linear")]
    s1["b"]["lanes"]["filter"] = [_kf(-0.5, 1.0, "linear")]
    s1["markers"] = [{"beat": 3.0, "label": "my drop"}]
    s1["notes"] = ["fixed by hand"]
    s1["a"]["echo_beats"] = 1.5
    s1["b"]["reverb_tap"] = "post_fader"
    s1["a"]["fx"] = [{"type": "stutter", "start_beat": 40.0, "end_beat": 42.0, "div": 0.5}]
    return s1


def test_a_user_edited_plan_survives() -> None:
    plan = convert.from_seedbox1(_edited())
    assert plan.edited
    assert [(k.beat, k.value, k.curve) for k in plan.a.lanes.volume] == [(-8.0, 0.25, "ease"), (2.5, 0.75, "linear")]
    assert plan.b.lanes.filter[0].value == -0.5
    assert [m.label for m in plan.markers] == ["my drop"]
    assert plan.notes == ["fixed by hand"]
    assert plan.a.sends.echo_beats == 1.5 and plan.b.sends.reverb_tap == "post_fader"
    assert plan.a.fx[0].type == "stutter" and plan.a.fx[0].div == 0.5


def test_an_off_beat_loop_moves_to_the_beat_and_the_notes_say_so() -> None:
    s1 = _edited()
    s1["a"]["loops"] = [
        {"start_beat": 0.25, "end_beat": 8.25, "source_beat": -3.75, "length_beats": 3.0},
        {"start_beat": 16.0, "end_beat": 20.0, "source_beat": 0.0, "length_beats": 0.3},
        {"start_beat": 24.1, "end_beat": 24.2, "source_beat": 0.0, "length_beats": 1.0},  # nothing left of it
    ]
    plan = convert.from_seedbox1(s1)
    assert [(lp.start_beat, lp.end_beat, lp.source_beat, lp.length_beats) for lp in plan.a.loops] == [
        (0.0, 8.0, -4.0, 3.0), (16.0, 20.0, 0.0, 0.25)]  # fmt: skip
    assert plan.notes[-1].endswith("Seedbox 1: 3.")


def test_a_seedbox1_piecewise_grid_carries_over() -> None:
    s1 = _edited()
    s1["a"]["grid"] = [dict(g) for g in STEP_GRID]
    plan = convert.from_seedbox1(s1)
    beats = np.arange(-4.0, 20.0, 0.25)
    assert v3.deck_time(plan.a, beats) == pytest.approx(sb1.deck_time(sb1.validate(s1)["a"], beats))
    assert len(plan.a.grid) == 3


def test_a_constant_seedbox1_deck_gets_one_segment() -> None:
    plan = convert.from_seedbox1(_edited())
    s1 = _edited()["b"]
    assert [(s.start_beat, s.start_s, s.bpm) for s in plan.b.grid] == [(0.0, s1["beat0_s"], s1["native_bpm"])]


def test_the_converter_can_key_the_decks_by_library_hash() -> None:
    plan = convert.from_seedbox1(_edited(), from_track="hash-a", to_track="hash-b")
    assert (plan.a.track_id, plan.b.track_id) == ("hash-a", "hash-b")


@pytest.mark.parametrize("raw", [None, [], "plan", {}, {"from_id": "a"}])
def test_the_converter_refuses_what_seedbox1_refuses(raw: object) -> None:
    with pytest.raises(ValueError):
        convert.from_seedbox1(raw)


def test_load_reads_either_version() -> None:
    from_s1 = convert.load(_edited())
    assert from_s1.version == v3.PLAN_VERSION and from_s1.edited
    assert convert.load(json.loads(from_s1.model_dump_json())) == from_s1


def test_a_plan_from_before_versions_loads_too() -> None:
    s1 = _edited()
    del s1["version"]
    assert convert.load(s1).edited
