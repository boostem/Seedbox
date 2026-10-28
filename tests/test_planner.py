"""transitions.suggest / _plan and mixplan.generate / validate on synthetic analyses with known structure."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from musicreco import mixplan, transitions

from .fixtures import (
    COLD_END, DROP_HEAVY, DROP_HEAVY_B, FAST, HALF_TIME, LONG_OUTRO, PAIRS, PICKUP, SHORT_OUTRO, STANDARD,
    VOCAL_INTRO, VOCAL_OUTRO, Spec, analysis, track,
)  # fmt: skip

PHRASE = transitions.PHRASE

# (A, B) -> (transitions' technique, overlap bars, mixplan's technique, plan bars)
EXPECTED = {
    "long outro -> standard": ("long blend", 16, "long blend", 16),
    "standard -> long outro": ("long blend", 16, "long blend", 16),
    "short outro -> standard": ("phrase blend", 16, "long blend", 16),
    "cold end -> standard": ("echo out", 16, "echo out", 16),
    "vocal outro -> standard": ("phrase blend", 16, "filter sweep", 16),
    "vocal outro -> vocal intro": ("bass swap", 8, "bass swap", 8),
    "drop heavy -> drop heavy": ("drop swap", 0, "drop swap", 4),
    "standard -> pickup": ("long blend", 16, "long blend", 16),
    "standard -> fast": ("long blend", 16, "echo out", 4),
    "standard -> half time": ("long blend", 16, "long blend", 16),
    "standard -> clash key": ("long blend", 16, "long blend", 16),
    "short outro -> clash key": ("phrase blend", 16, "filter sweep", 16),
}
IDS = [name for name, _, _ in PAIRS]


def _factor(a: Spec, b: Spec) -> float:
    return mixplan._mix(track(a), analysis(a), track(b), analysis(b), None).factor


def _grids(a: Spec, b: Spec) -> tuple[transitions._Grid, transitions._Grid]:
    return transitions._grid(analysis(a)), transitions._grid(analysis(b), _factor(a, b))


def _bar(g: transitions._Grid, t: float) -> float:
    return (t - g.fd) / g.bar_s


def _on_bar(x: float) -> int:
    assert x == pytest.approx(round(x), abs=1e-3), f"{x} is not on a bar line"
    return round(x)


def test_every_pair_has_an_expectation() -> None:
    assert sorted(EXPECTED) == sorted(IDS)


# --- transitions ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "a", "b"), PAIRS, ids=IDS)
def test_suggest_technique_and_overlap(name: str, a: Spec, b: Spec) -> None:
    tr = transitions.suggest(track(a), analysis(a), track(b), analysis(b))
    tech, overlap, _, _ = EXPECTED[name]
    assert tr["ready"] is True
    assert (tr["technique"], tr["overlap_bars"]) == (tech, overlap)


@pytest.mark.parametrize(("name", "a", "b"), PAIRS, ids=IDS)
def test_suggest_points_are_on_phrases(name: str, a: Spec, b: Spec) -> None:
    tr = transitions.suggest(track(a), analysis(a), track(b), analysis(b))
    ga, gb = _grids(a, b)
    a0, b0 = _on_bar(_bar(ga, tr["out_start_s"])), _on_bar(_bar(gb, tr["in_start_s"]))
    if tr["technique"] == "drop swap":
        assert a0 in ga.drops and b0 in gb.drops
    else:
        assert a0 in ga.phrases, f"A comes out at bar {a0}, not a phrase start {ga.phrases}"
        assert b0 in gb.phrases, f"B is cued at bar {b0}, not a phrase start {gb.phrases}"
    length = tr["overlap_bars"]
    assert length in (0, 8, 16, 32)
    assert a0 + length <= ga.end, "the overlap runs past A's music"
    if tr["swap_bar"] is not None:
        k = tr["swap_bar"] - 1
        assert 0 < k < length and k % (PHRASE // 2) == 0
        assert k % PHRASE == 0 or k == length // 2


def test_suggest_without_analysis_is_not_ready() -> None:
    tr = transitions.suggest(track(STANDARD), analysis(STANDARD), track(LONG_OUTRO), None)
    assert tr["ready"] is False and tr["technique"] is None
    assert any("B is analysed" in n for n in tr["notes"])


def test_plan_long_blend_needs_drums_on_both_sides() -> None:
    ga, gb = _grids(LONG_OUTRO, STANDARD)
    p = transitions._plan(ga, gb, key_clash=False)
    assert p.technique == "long blend"
    assert p.a0 >= ga.outro[0], "the blend starts inside A's drums-only outro"
    assert p.b0 + p.length <= gb.intro[1], "and stays inside B's drums-only intro"


def test_plan_cold_end_echoes_out() -> None:
    ga, gb = _grids(COLD_END, STANDARD)
    assert transitions._abrupt(ga)
    p = transitions._plan(ga, gb, key_clash=False)
    assert p.technique == "echo out"
    assert p.a0 + p.length == ga.end


def test_plan_vocal_clash_drops_the_blend_for_a_drop_swap() -> None:
    ga, gb = _grids(DROP_HEAVY, DROP_HEAVY_B)
    blend = transitions._best_blend(ga, gb, key_clash=False)
    assert blend is not None and len(blend.clash) > transitions._MAX_CLASH_BARS
    p = transitions._plan(ga, gb, key_clash=False)
    assert (p.technique, p.a0, p.b0, p.length) == ("drop swap", 136, 24, 0)


def test_plan_pickup_cues_b_on_its_phrase_offset() -> None:
    ga, gb = _grids(STANDARD, PICKUP)
    assert gb.off == 4 and gb.phrases[0] == 4
    assert transitions._plan(ga, gb, key_clash=False).b0 % PHRASE == 4


def test_half_time_b_is_put_on_a_double_grid() -> None:
    _, gb = _grids(STANDARD, HALF_TIME)
    assert _factor(STANDARD, HALF_TIME) == 2.0
    assert gb.n == 2 * HALF_TIME.bars


# --- mixplan -------------------------------------------------------------------------------------------------------


def _generate(a: Spec, b: Spec, **kw: object) -> dict:
    return mixplan.generate(track(a), analysis(a), track(b), analysis(b), "folder", **kw)


@pytest.mark.parametrize(("name", "a", "b"), PAIRS, ids=IDS)
def test_generate_technique_and_bars(name: str, a: Spec, b: Spec) -> None:
    plan = _generate(a, b)
    _, _, tech, bars = EXPECTED[name]
    assert (plan["technique"], plan["bars"]) == (tech, bars)


@pytest.mark.parametrize(("name", "a", "b"), PAIRS, ids=IDS)
def test_generate_cues_and_swap_on_phrases(name: str, a: Spec, b: Spec) -> None:
    plan = _generate(a, b)
    ga, gb = _grids(a, b)
    a0, b0 = _on_bar(_bar(ga, plan["a"]["beat0_s"])), _on_bar(_bar(gb, plan["b"]["beat0_s"]))
    if plan["technique"] in ("drop swap", "quick cut", "slam", "echo out") and not any(
        m["label"] == "B in" for m in plan["markers"]
    ):
        assert a0 in ga.phrases or a0 in ga.drops or a0 == ga.end
        assert b0 in gb.phrases or b0 in gb.drops
    else:
        assert a0 in ga.phrases and b0 in gb.phrases
    for m in plan["markers"]:
        if m["label"] == "bass swap":
            k = m["beat"] / 4
            assert k == int(k) and int(k) % (PHRASE // 2) == 0 and 0 < k <= plan["bars"]
    # the decks' beat grids: master beat 0 is a downbeat of both
    assert plan["a"]["native_bpm"] == pytest.approx(a.bpm)
    assert plan["b"]["native_bpm"] == pytest.approx(b.bpm * _factor(a, b))


@pytest.mark.parametrize(("name", "a", "b"), PAIRS, ids=IDS)
def test_generate_is_a_valid_plan(name: str, a: Spec, b: Spec) -> None:
    plan = _generate(a, b)
    assert mixplan.validate(plan) == plan
    w = plan["window"]
    assert w["start_beat"] < 0 < w["end_beat"]
    assert mixplan.master_seconds(plan["tempo"], w["end_beat"], w["start_beat"]) <= mixplan._MAX_PREVIEW_S + 1e-6
    for deck in (plan["a"], plan["b"]):
        assert set(deck["lanes"]) == set(mixplan.LANES)
        for lane, kfs in deck["lanes"].items():
            lo, hi = mixplan.LANE_RANGES[lane]
            assert all(lo <= k["value"] <= hi for k in kfs)
            assert [k["beat"] for k in kfs] == sorted(k["beat"] for k in kfs)


def test_generate_big_tempo_gap_cuts_and_jumps_tempo() -> None:
    plan = _generate(STANDARD, FAST)
    assert plan["technique"] == "echo out"
    tempos = [k["value"] for k in plan["tempo"]]
    assert tempos[0] == pytest.approx(STANDARD.bpm) and tempos[-1] == pytest.approx(FAST.bpm)
    assert any(m["label"] == "echo throw" for m in plan["markers"])


def test_generate_vocal_clash_ducks_a_mids() -> None:
    sung = Spec("sung_to_the_end", outro_drums=False, vocals=[(64, 128)])  # no clash-free 16-bar window
    plan = _generate(sung, VOCAL_INTRO, technique="phrase blend", bars=16)
    assert any("A's mids are ducked" in n for n in plan["notes"])
    # B is sung through bar 8 of the blend: A's mids are down by bar 3 (a 2-bar ease) and stay down
    mids = mixplan.lane_value(plan["a"]["lanes"]["eq_mid"], np.arange(8.0, 32.0))
    assert np.all(mids <= mixplan._VOCAL_CUT + 1e-9)


def test_generate_drop_swap_cuts_on_both_drops() -> None:
    plan = _generate(DROP_HEAVY, DROP_HEAVY_B)
    ga, gb = _grids(DROP_HEAVY, DROP_HEAVY_B)
    assert _on_bar(_bar(ga, plan["a"]["beat0_s"])) == 136
    assert _on_bar(_bar(gb, plan["b"]["beat0_s"])) == 24
    assert [m["label"] for m in plan["markers"]] == ["cut"]


@pytest.mark.parametrize("technique", mixplan.TECHNIQUES)
@pytest.mark.parametrize("bars", [None, *mixplan.BARS])
def test_generate_every_technique_and_length(technique: str, bars: int | None) -> None:
    plan = _generate(SHORT_OUTRO, STANDARD, technique=technique, bars=bars)
    assert plan["technique"] == technique
    if bars is not None:
        assert plan["bars"] == bars
    assert mixplan.validate(plan) == plan


def test_generate_without_analyses_is_a_rough_plan() -> None:
    plan = mixplan.generate(track(STANDARD), None, track(LONG_OUTRO), None, "folder")
    assert plan["technique"] == "bass swap" and plan["bars"] == 8
    assert any("Rough plan" in n for n in plan["notes"])


def test_generate_rejects_unknown_technique_and_bars() -> None:
    with pytest.raises(ValueError):
        _generate(STANDARD, STANDARD, technique="scratch")
    with pytest.raises(ValueError):
        _generate(STANDARD, STANDARD, bars=12)


# --- validate ------------------------------------------------------------------------------------------------------


def test_validate_normalises() -> None:
    plan = copy.deepcopy(_generate(STANDARD, LONG_OUTRO))
    plan["bars"] = 15
    plan["a"]["lanes"]["eq_low"] = [{"beat": 8, "value": -99}, {"beat": 0, "value": 12, "curve": "bogus"}]
    del plan["b"]["lanes"]["filter"]
    v = mixplan.validate(plan)
    assert v["bars"] == 16
    assert [(k["beat"], k["value"]) for k in v["a"]["lanes"]["eq_low"]] == [(0.0, 6.0), (8.0, -30.0)]
    assert v["b"]["lanes"]["filter"] == [{"beat": 0.0, "value": 0.0, "curve": "hold"}]


@pytest.mark.parametrize(
    "breakage",
    [
        lambda p: p.pop("from_id"),
        lambda p: p.update(technique="scratch"),
        lambda p: p.update(bars="many"),
        lambda p: p.pop("a"),
        lambda p: p.update(window={"start_beat": 10_000, "end_beat": 10_000.5}),
    ],
    ids=["no from_id", "unknown technique", "bars not a number", "no deck a", "empty window"],
)
def test_validate_rejects(breakage) -> None:  # noqa: ANN001
    plan = copy.deepcopy(_generate(STANDARD, LONG_OUTRO))
    breakage(plan)
    with pytest.raises(ValueError):
        mixplan.validate(plan)
