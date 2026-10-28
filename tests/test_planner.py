"""transitions.suggest / _plan and mixplan.generate / validate on synthetic analyses with known structure."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from musicreco import mixplan, settings, structure, transitions

from .fixtures import (
    BUILD_END, CLASH_KEY, CLUB_PAIRS, COLD_END, DROP_HEAVY, DROP_HEAVY_B, FAST, HALF_TIME, HOUSE, HOUSE_B, INSERT, LONG_OUTRO,
    MUSIC_OUTRO, OUTRO_8, OUTRO_24, PAIRS, PICKUP, SHORT_INTRO, SHORT_INTRO_DROP, SHORT_OUTRO, STANDARD, TECHNO,
    TECHNO_B, VOCAL_INTRO, VOCAL_OUTRO,
    Spec, analysis, track,
)  # fmt: skip

PHRASE = transitions.PHRASE

# (A, B) -> (transitions' technique, overlap bars, mixplan's technique, plan bars)
EXPECTED = {
    "long outro -> standard": ("long blend", 16, "long blend", 16),
    "standard -> long outro": ("long blend", 16, "long blend", 16),
    "short outro -> standard": ("phrase blend", 16, "long blend", 16),
    "cold end -> standard": ("echo out", 16, "loop roll", 16),  # A rolls its last 4 bars into B's phrase
    "vocal outro -> standard": ("phrase blend", 16, "filter sweep", 16),
    "vocal outro -> vocal intro": ("bass swap", 16, "long blend", 16),
    "drop heavy -> drop heavy": ("drop swap", 0, "drop swap", 4),
    "standard -> pickup": ("long blend", 16, "long blend", 16),
    "standard -> fast": ("long blend", 16, "echo out", 4),
    "standard -> half time": ("long blend", 16, "long blend", 16),
    "standard -> clash key": ("long blend", 16, "long blend", 16),
    "short outro -> clash key": ("phrase blend", 16, "filter sweep", 16),
    "insert -> standard": ("long blend", 16, "long blend", 16),
    "outro 24 -> standard": ("long blend", 16, "long blend", 16),
    "standard -> short intro drop": ("long blend", 16, "slam", 4),  # B loops its 8-bar intro
    "build end -> short intro drop": ("phrase blend", 16, "slam", 4),  # B loops its 8-bar intro
    "build end -> fast": ("phrase blend", 16, "slam", 4),
    "build end -> standard": ("phrase blend", 16, "long blend", 16),
    "house -> house": ("long blend", 16, "long blend", 16),
    "techno -> techno": ("long blend", 32, "long blend", 32),
    "music outro -> house": ("phrase blend", 16, "long blend", 16),
    "short edges -> short edges": ("long blend", 16, "long blend", 16),  # A loops its outro, B its intro
    "outro 8 -> standard": ("long blend", 16, "long blend", 16),  # A loops its 8-bar outro
    "music outro -> short intro": ("phrase blend", 16, "long blend", 16),  # B loops its 8-bar intro
}
IDS = [name for name, _, _ in PAIRS]


def _factor(a: Spec, b: Spec) -> float:
    return mixplan.make_mix(track(a), analysis(a), track(b), analysis(b), None).factor


def _grids(a: Spec, b: Spec) -> tuple[transitions.Grid, transitions.Grid]:
    return transitions.make_grid(analysis(a)), transitions.make_grid(analysis(b), _factor(a, b))


def _bar(g: transitions.Grid, t: float) -> float:
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
    length, xa, xb = tr["overlap_bars"], tr["a_loop_bars"], tr["b_loop_bars"]
    assert length in (0, 8, 16, 32)
    assert a0 + length - xa <= ga.end, "the overlap runs past A's music without a loop"
    assert xa % 4 == 0 and xb % PHRASE == 0 and xa + xb <= length
    if tr["swap_bar"] is not None:  # on a phrase start in both tracks (the master grid inside a loop)
        k = tr["swap_bar"] - 1
        assert 0 < k < length
        assert a0 + k in ga.phrases if k < length - xa else k % PHRASE == 0
        assert b0 + k - xb in gb.phrases if k >= xb else k % PHRASE == 0


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
    assert blend is not None and len(blend.clash) > transitions._max_clash(blend.length)
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


# --- phrase model --------------------------------------------------------------------------------------------------


def test_phrase_grid_levels_count_from_each_section_start() -> None:
    # sections at 0 and 40: 32/8/16/8 from bar 0, the count restarts at 40 (a 32-bar phrase opens with the section)
    assert structure.phrase_grid([0, 40], 0, 72) == [
        (0, 32), (8, 8), (16, 16), (24, 8), (32, 32), (40, 32), (48, 8), (56, 16), (64, 8), (72, 32)
    ]  # fmt: skip


def test_phrase_grid_energy_change_restarts_the_count() -> None:
    energy = [0.5] * 24 + [0.9] * 24  # the energy steps up at bar 24 without a section boundary
    assert dict(structure.phrase_grid([], 0, 48, energy))[24] == 32
    assert dict(structure.phrase_grid([], 0, 48, [0.5] * 48))[24] == 8


def test_phrase_grid_reanchors_after_a_4_bar_insert() -> None:
    pts = [p for p, _ in structure.phrase_grid([16, 56, 60, 68], 0, 92)]
    assert pts == [0, 8, 16, 24, 32, 40, 48, 56, 60, 68, 76, 84, 92]
    assert all(p % 8 == 4 for p in pts if p > 56), "the old grid (64, 72, ...) is gone after the insert"
    # a grid point under 8 bars before the next anchor opens no phrase; an off-grid section start is noise
    assert 52 not in [p for p, _ in structure.phrase_grid([16, 60], 0, 92)]
    assert [p for p, _ in structure.phrase_grid([16, 43], 0, 48)] == [0, 8, 16, 24, 32, 40, 48]


def test_grid_reanchors_after_a_4_bar_insert() -> None:
    g = transitions.make_grid(analysis(INSERT))
    after = [p for p in g.phrases if p > INSERT.insert]
    assert after and all(p % PHRASE == 4 for p in after), g.phrases
    assert g.level(g.outro[0]) == 32 and g.level(g.outro[0] + 8) == 8
    p = transitions._plan(g, transitions.make_grid(analysis(STANDARD)), key_clash=False)
    assert p.a0 == g.outro[0] == 116, "the blend starts on the outro's re-anchored phrase, not the old grid's 112"


def test_blend_starts_on_a_16_bar_phrase_of_a() -> None:
    ga, gb = _grids(OUTRO_24, STANDARD)
    assert ga.outro == (104, 128) and ga.level(104) == 32 and ga.level(112) == 8
    assert transitions._plan(ga, gb, key_clash=False).a0 == 104


def test_blend_start_without_the_16_bar_preference_lands_mid_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transitions, "_P_A_HALF_PHRASE", 0.0)
    ga, gb = _grids(OUTRO_24, STANDARD)
    assert transitions._plan(ga, gb, key_clash=False).a0 == 112  # the second 8 bars of A's 16-bar phrase at 104


def test_blend_cues_b_on_its_first_downbeat_or_a_16_bar_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    for a, b in ((VOCAL_OUTRO, VOCAL_INTRO), (LONG_OUTRO, STANDARD), (STANDARD, PICKUP)):
        ga, gb = _grids(a, b)
        b0 = transitions._plan(ga, gb, key_clash=False).b0
        assert b0 in (0, transitions.first_cue(gb)) or gb.level(b0) >= 16
    # cueing B 8 bars into one of its 16-bar phrases costs extra; its first downbeat or a 16-bar phrase doesn't
    ga, gb = _grids(LONG_OUTRO, LONG_OUTRO)
    assert (gb.level(0), gb.level(8), gb.level(16)) == (32, 8, 16)
    fits = {b0: transitions._window(ga, gb, 112, b0, 8, False).fit for b0 in (0, 8, 16)}
    pen = transitions._P_B_HALF_PHRASE
    monkeypatch.setattr(transitions, "_P_B_HALF_PHRASE", 0.0)
    for b0, cost in ((0, 0.0), (8, pen), (16, 0.0)):
        assert transitions._window(ga, gb, 112, b0, 8, False).fit - fits[b0] == pytest.approx(cost)


@pytest.mark.parametrize(("length", "swap"), [(8, None), (16, 8), (32, 16)])
def test_swap_lands_on_a_phrase_inside_the_overlap(length: int, swap: int | None) -> None:
    g = transitions.make_grid(analysis(Spec("bassy", intro_bars=0, outro_bars=0)))
    plan = transitions.Plan("bass swap", 32, 32, length, 90.0, [])
    assert transitions.swap_offset(g, g, plan) == swap  # 8 bars: no phrase inside, swap on the 1 as A leaves


def test_swap_prefers_where_b_main_section_arrives() -> None:
    ga, gb = _grids(STANDARD, LONG_OUTRO)  # B's 32-bar intro
    assert gb.intro == (0, 32)
    # cued 8 bars in, B's main section arrives 24 bars into a 32-bar overlap, on a phrase of both: swap there
    assert transitions.swap_offset(ga, gb, transitions.Plan("long blend", 96, 8, 32, 90.0, [])) == 24
    # cued at 0 it arrives as A leaves: the 16-bar phrase point in the middle (A bar 112 opens A's outro)
    assert transitions.swap_offset(ga, gb, transitions.Plan("long blend", 96, 0, 32, 90.0, [])) == 16


def test_swap_follows_a_after_its_4_bar_insert() -> None:
    ga, gb = _grids(INSERT, STANDARD)
    # A's phrases sit on bars = 4 (mod 8) after the insert; B's on 0 (mod 8): a 16-bar overlap from A 112 / B 0 has
    # no bar where both start a phrase inside it, so the lows change hands at its end instead of mid-phrase in A
    assert transitions.swap_offset(ga, gb, transitions.Plan("bass swap", 112, 0, 16, 90.0, [])) is None
    assert transitions.swap_offset(ga, gb, transitions.Plan("bass swap", 116, 0, 16, 90.0, [])) == 8


def test_analysis_reports_phrase_levels() -> None:
    from .fixtures import click_track

    an = structure._analyze_samples(click_track(STANDARD, structure.SR, 1000.0)[0], structure.SR, STANDARD.bpm)
    assert an["version"] == structure.ANALYSIS_VERSION
    starts = [s["start_bar"] for s in an["sections"]]
    energy = an["energy_per_bar"]
    assert [(p["bar"], p["level"]) for p in an["phrases"]] == structure.phrase_grid(
        starts, an["phrase_offset"], an["bar_count"], energy
    )
    assert an["phrases"][0] == {"bar": an["phrase_offset"], "level": 32}


def _vocal_outro_features(n: int = 96, tail: int = 16, sung_tail: bool = True) -> dict[str, np.ndarray]:
    """Per-bar features of a track at full groove whose last `tail` bars lose their leads and bass (a DJ outro)."""
    thin = np.arange(n) >= n - tail
    f = {k: np.zeros(n) for k in ("e_db", "low_db", "mid_db", "high_db", "harm_db", "bass_db")}
    f["kick"] = np.ones(n)
    f["harm_db"][thin] = f["high_db"][thin] = -12.0
    f["bass_db"][thin] = -16.0
    f["vocal"] = np.where(thin & sung_tail, 0.8, 0.05)
    return f


@pytest.mark.parametrize("sung", [False, True])
def test_vocal_outro_is_still_an_outro(sung: bool) -> None:
    f = _vocal_outro_features(sung_tail=sung)
    sections = structure._refine_edges([("body", 0, 96)], f, 0)
    assert sections[-1] == ("outro", 80, 96), "vocals over a stripped-down tail don't stop it being the outro"


def test_vocal_head_is_not_an_intro() -> None:
    f = {k: v[::-1].copy() for k, v in _vocal_outro_features().items()}  # the same, mirrored: a sung, thin head
    assert structure._refine_edges([("body", 0, 96)], f, 0)[0][0] != "intro"
    f["vocal"][:] = 0.05
    assert structure._refine_edges([("body", 0, 96)], f, 0)[0] == ("intro", 0, 16)


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
    b_loops = [lp for lp in plan["b"]["loops"] if lp["start_beat"] == 0.0]  # B looping its intro from its cue
    xb = round(b_loops[0]["end_beat"] / 4) if b_loops else 0
    a0, b0 = _on_bar(_bar(ga, plan["a"]["beat0_s"])), _on_bar(_bar(gb, plan["b"]["beat0_s"])) + xb
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
            assert k == int(k) and 0 < k <= plan["bars"]
            b_k = b0 + int(k) - xb if k >= xb else None  # B's bar there; inside its loop the master grid's
            assert k == plan["bars"] or (a0 + int(k) in ga.phrases and (b_k in gb.phrases if b_k is not None
                                                                        else k % PHRASE == 0))
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


def test_generate_vocal_clash_ducks_b_while_a_leads() -> None:
    sung = Spec("sung_to_the_end", outro_drums=False, vocals=[(64, 128)])  # no clash-free 16-bar window
    plan = _generate(sung, VOCAL_INTRO, technique="phrase blend", bars=16)
    assert plan["bars"] == 16, "the clash is ducked, not cut short"
    assert any("B's mids are ducked" in n for n in plan["notes"])
    # B is sung through bar 8 of the blend, before the swap on bar 9: B's mids are down from its first beat to the
    # swap and back up 2 bars later, A's are never ducked
    mids = mixplan.lane_value(plan["b"]["lanes"]["eq_mid"], np.arange(0.0, 33.0))
    assert np.all(mids <= mixplan._VOCAL_CUT + 1e-9)
    assert mixplan.lane_value(plan["b"]["lanes"]["eq_mid"], 40.0) == pytest.approx(0.0)
    assert min(k["value"] for k in plan["a"]["lanes"]["eq_mid"]) > mixplan._VOCAL_CUT


def test_vocal_clash_after_the_swap_ducks_a_to_the_end() -> None:
    A, B = mixplan._lanes(), mixplan._lanes()
    mixplan._blend_lanes("bass swap", 16, 8, A, B, [])
    on_b, on_a = mixplan._duck_mids(A, B, [6, 7, 8, 9, 10], 8, 16)
    assert (on_b, on_a) == ([6, 7], [8, 9, 10])
    a_mid = [{"beat": b, "value": v, "curve": c} for b, v, c in A["eq_mid"]]
    b_mid = [{"beat": b, "value": v, "curve": c} for b, v, c in B["eq_mid"]]
    assert np.all(mixplan.lane_value(a_mid, np.arange(32.0, 65.0)) <= mixplan._VOCAL_CUT + 1e-9)
    assert mixplan.lane_value(a_mid, 20.0) == pytest.approx(0.0)  # A untouched until 2 bars before the swap
    assert np.all(mixplan.lane_value(b_mid, np.arange(24.0, 33.0)) <= mixplan._VOCAL_CUT + 1e-9)
    assert mixplan.lane_value(b_mid, 40.0) == pytest.approx(0.0)


def test_generate_drop_swap_cuts_on_both_drops() -> None:
    plan = _generate(DROP_HEAVY, DROP_HEAVY_B)
    ga, gb = _grids(DROP_HEAVY, DROP_HEAVY_B)
    assert _on_bar(_bar(ga, plan["a"]["beat0_s"])) == 136
    assert _on_bar(_bar(gb, plan["b"]["beat0_s"])) == 24
    assert [m["label"] for m in plan["markers"]] == ["cut"]


def _cues(plan: dict, a: Spec, b: Spec) -> tuple[int, int]:
    ga, gb = _grids(a, b)
    return _on_bar(_bar(ga, plan["a"]["beat0_s"])), _on_bar(_bar(gb, plan["b"]["beat0_s"]))


def test_slam_lines_a_build_end_up_with_b_drop() -> None:
    plan = _generate(BUILD_END, SHORT_INTRO_DROP)
    # A's build ends where its last drop would hit (bar 112); on that 1 (master beat 0) B's drop hits (bar 16)
    assert _cues(plan, BUILD_END, SHORT_INTRO_DROP) == (112, 16)
    assert any(m["label"] == "cut" and m["beat"] == 0 for m in plan["markers"])
    assert mixplan.lane_value(plan["a"]["lanes"]["volume"], [-0.01, 0.0]).tolist() == [1.0, 0.0]
    assert "the end of A's build" in plan["notes"][0]


def test_slam_without_a_build_cuts_at_a_outro_into_b_drop() -> None:
    plan = _generate(STANDARD, SHORT_INTRO_DROP)
    assert _cues(plan, STANDARD, SHORT_INTRO_DROP) == (STANDARD.bars - STANDARD.outro_bars, 16)


def test_slam_throws_a_and_rolls_its_last_two_bars() -> None:
    plan = _generate(BUILD_END, SHORT_INTRO_DROP)
    a = plan["a"]
    assert mixplan.lane_value(a["lanes"]["echo_send"], 0.0) > 0.5
    assert mixplan.lane_value(a["lanes"]["reverb_send"], 0.0) > 0
    assert [(lp["start_beat"], lp["length_beats"]) for lp in a["loops"]] == [(-8, 4), (-4, 2), (-2, 1), (-1, 0.5)]
    assert all(lp["source_beat"] == -8 for lp in a["loops"])
    assert mixplan.lane_value(a["lanes"]["eq_low"], -2.0) == mixplan._KILL  # A's lows out on the roll's last bar
    assert [m["label"] for m in plan["markers"]] == ["loop roll", "B pre-roll", "echo throw", "cut"]


def test_roll_halves_down_to_half_a_beat() -> None:
    steps = [(lp["start_beat"], lp["end_beat"], lp["length_beats"]) for lp in mixplan._roll(64.0, 48.0, 4)]
    assert steps == [(48, 56, 8), (56, 60, 4), (60, 62, 2), (62, 63, 1), (63, 66, 0.5)]
    assert [lp["length_beats"] for lp in mixplan._roll(0.0, -8.0)] == [4, 2, 1, 0.5]


def test_cold_end_rolls_its_last_bars_into_b_phrase() -> None:
    """A cold end (full power to its last bar) over B's 16-bar intro: instead of an echo out, A rolls its last 4
    bars into the 1 of B's phrase under a high-pass and an echo build, its lows out for the second half."""
    plan = _generate(COLD_END, STANDARD)
    assert (plan["technique"], plan["bars"]) == ("loop roll", 16)
    a = plan["a"]
    assert [(lp["start_beat"], lp["length_beats"]) for lp in a["loops"]] == [(48, 8), (56, 4), (60, 2), (62, 1),
                                                                            (63, 0.5)]
    assert all(lp["source_beat"] == 48 for lp in a["loops"])
    low = mixplan.lane_value(a["lanes"]["eq_low"], [47.0, 55.0, 57.0])
    assert low.tolist() == [0.0, 0.0, mixplan._KILL]
    filt = mixplan.lane_value(a["lanes"]["filter"], [48.0, 56.0, 64.0])
    assert filt[0] == 0.0 < filt[1] < filt[2] == pytest.approx(0.6)
    echo = mixplan.lane_value(a["lanes"]["echo_send"], [55.0, 60.0, 64.0])
    assert echo[0] == 0.0 < echo[1] < echo[2]
    assert mixplan.lane_value(plan["b"]["lanes"]["eq_low"], [63.9, 64.0]).tolist() == [mixplan._KILL, 0.0]
    assert {"beat": 48.0, "label": "loop roll"} in plan["markers"]
    assert "rolls 2 bars → 1 bar → 2 beats → 1 → ½" in plan["notes"][0]


def test_short_blends_end_in_a_two_bar_roll() -> None:
    plan = _generate(LONG_OUTRO, STANDARD, mix_length="short")
    assert (plan["technique"], plan["bars"]) == ("loop roll", 8)
    assert [lp["length_beats"] for lp in plan["a"]["loops"]] == [4, 2, 1, 0.5]
    assert plan["a"]["loops"][0]["start_beat"] == 24.0


def test_roll_constraints_only_where_it_overlaps_b() -> None:
    """The roll needs unsung A bars to loop and B's downbeat to land on; key and B's vocals only count over the
    roll's own bars (B's sung intro start doesn't stop it, a clash key under A's music does)."""
    sung_start = Spec("sung_start", vocals=[(0, 4)])
    assert _generate(LONG_OUTRO, sung_start, mix_length="short")["technique"] == "loop roll"
    sung_end = Spec("sung_end", vocals=[(4, 8)])
    assert _generate(LONG_OUTRO, sung_end, mix_length="short")["technique"] != "loop roll"
    assert _generate(LONG_OUTRO, CLASH_KEY, mix_length="short")["technique"] == "loop roll"  # A rolls drums only
    music = Spec("music_long_outro", outro_bars=32, intro_bars=32, outro_drums=False)
    assert _generate(music, STANDARD, mix_length="short")["technique"] == "loop roll"
    assert _generate(music, CLASH_KEY, mix_length="short")["technique"] != "loop roll"
    sung_outro = Spec("sung_outro", outro_bars=32, intro_bars=32, vocals=[(116, 120)])  # the bars A would roll
    assert _generate(sung_outro, STANDARD, mix_length="short")["technique"] != "loop roll"
    m = mixplan.make_mix(track(COLD_END), analysis(COLD_END), track(STANDARD), analysis(STANDARD), None)
    assert mixplan._roll_ok(m, mixplan._Place(112, 0, 16, None))
    assert not mixplan._roll_ok(m, mixplan._Place(112, 4, 16, None))  # B's bar 20 isn't a downbeat of a phrase


def test_drop_swap_rolls_an_unsung_build_into_the_cut() -> None:
    unsung = Spec("drop_heavy_unsung", bars=160, outro_bars=8, outro_drums=False, drops=[40, 96, 136],
                  vocals=[(16, 32)])
    plan = _generate(unsung, DROP_HEAVY_B, technique="drop swap")
    a = plan["a"]
    assert [(lp["start_beat"], lp["length_beats"]) for lp in a["loops"]] == [(-8, 4), (-4, 2), (-2, 1), (-1, 0.5)]
    assert mixplan.lane_value(a["lanes"]["eq_low"], -2.0) == mixplan._KILL
    assert {"beat": -8.0, "label": "loop roll"} in plan["markers"]
    assert "rolling its last 2 bars" in plan["notes"][0]
    sung = _generate(DROP_HEAVY, DROP_HEAVY_B, technique="drop swap")
    assert sung["a"]["loops"] == [] and "loop roll" not in [m["label"] for m in sung["markers"]]


def test_slam_does_not_roll_a_sung_build() -> None:
    sung = Spec("sung_build", outro_bars=8, outro_drums=False, drops=[112], vocals=[(100, 112)])
    plan = _generate(sung, SHORT_INTRO_DROP)
    assert plan["technique"] == "slam" and plan["a"]["loops"] == []
    assert "loop roll" not in [m["label"] for m in plan["markers"]]


def test_slam_pre_rolls_b_last_build_bar_under_a() -> None:
    plan = _generate(BUILD_END, SHORT_INTRO_DROP)
    b = plan["b"]
    assert b["play_from_beat"] == -4.0
    vol = mixplan.lane_value(b["lanes"]["volume"], [-4.0, -2.0, 0.0])
    assert vol[0] == 0.0 and 0 < vol[1] < 1 and vol[2] == 1.0
    assert mixplan.lane_value(b["lanes"]["eq_low"], -1.0) == mixplan._KILL  # no bass under A
    assert mixplan.lane_value(b["lanes"]["eq_low"], 0.0) == 0.0 and mixplan.lane_value(b["lanes"]["filter"], 0.0) == 0


def test_slam_over_a_big_tempo_gap_jumps_on_the_cut_without_pre_roll() -> None:
    plan = _generate(BUILD_END, FAST)
    assert plan["technique"] == "slam"
    assert _cues(plan, BUILD_END, FAST)[0] == 112
    tempo = mixplan.lane_value(plan["tempo"], [-0.01, 0.0])
    assert tempo.tolist() == pytest.approx([BUILD_END.bpm, FAST.bpm])
    assert plan["b"]["play_from_beat"] == 0.0  # B would be stretched to A's tempo for its pre-roll bar
    assert "B pre-roll" not in [m["label"] for m in plan["markers"]]


def test_slam_needs_a_short_intro_and_a_hitting_drop() -> None:
    long_intro = Spec("long_intro_drop", intro_bars=16, drops=[32])
    assert _generate(STANDARD, long_intro)["technique"] != "slam"
    flat = Spec("flat_drop", intro_bars=8, drops=[16])
    an = analysis(flat)
    an["energy_per_bar"][12:16] = [0.95] * 4  # no build before it: the drop doesn't hit
    plan = mixplan.generate(track(STANDARD), analysis(STANDARD), track(flat), an, "folder")
    assert plan["technique"] != "slam"


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
    assert plan["technique"] == "bass swap" and plan["bars"] == 16
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


# --- blend length ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "a", "b"), CLUB_PAIRS, ids=[p[0] for p in CLUB_PAIRS])
def test_club_pairs_blend_16_bars_or_more(name: str, a: Spec, b: Spec) -> None:
    tr = transitions.suggest(track(a), analysis(a), track(b), analysis(b))
    plan = _generate(a, b)
    assert tr["overlap_bars"] >= 16 and plan["bars"] >= 16
    assert plan["technique"] in ("long blend", "bass swap", "phrase blend")


@pytest.mark.parametrize(
    ("a", "b", "bars"),
    [
        (TECHNO, TECHNO_B, {"short": 8, "normal": 32, "long": 32}),
        (HOUSE, HOUSE_B, {"short": 8, "normal": 16, "long": 16}),
        (STANDARD, LONG_OUTRO, {"short": 8, "normal": 16, "long": 32}),  # B's 32-bar drum intro
    ],
    ids=["techno", "house", "into a 32-bar intro"],
)
def test_mix_length_preference(a: Spec, b: Spec, bars: dict[str, int]) -> None:
    for pref, n in bars.items():
        tr = transitions.suggest(track(a), analysis(a), track(b), analysis(b), mix_length=pref)
        assert tr["overlap_bars"] == n, pref
        assert _generate(a, b, mix_length=pref)["bars"] == n, pref


def test_mix_length_sets_the_rough_plan_length() -> None:
    for pref, n in (("short", 8), ("normal", 16), ("long", 16)):  # no analysis: never longer than 16
        plan = mixplan.generate(track(STANDARD), None, track(LONG_OUTRO), None, "folder", mix_length=pref)
        assert plan["bars"] == n, pref


def test_blend_lanes_are_phrased() -> None:
    plan = _generate(TECHNO, TECHNO_B)  # 32 bars, bass swap on bar 17
    a, b = plan["a"]["lanes"], plan["b"]["lanes"]

    def at(lane: list[dict], bar: float) -> float:
        return float(mixplan.lane_value(lane, 4.0 * bar))

    assert at(b["volume"], 4) < 1.0 and at(b["volume"], 8) == 1.0, "B's fader comes up over the first 8 bars"
    assert at(b["eq_high"], 8) == 0.0 and at(b["eq_mid"], 12) == -4.0 and at(b["eq_mid"], 18) == 0.0
    assert at(b["eq_low"], 16) == mixplan._KILL and at(b["eq_low"], 16.5) == 0.0, "the lows swap on bar 17"
    for lane in ("volume", "eq_mid", "eq_high", "filter"):  # the ride: A untouched through bar 24
        assert at(a[lane], 23.9) == mixplan.LANE_DEFAULTS[lane], lane
    assert at(a["eq_mid"], 26) == 0.0 and at(a["eq_high"], 28) == -12.0 and at(a["volume"], 28) == 1.0, \
        "highs out first"
    assert at(a["eq_mid"], 30) == -10.0 and 0.0 < at(a["volume"], 30) < 1.0, "then mids, then the fader"
    assert at(a["volume"], 32) == 0.0


# --- loop extension --------------------------------------------------------------------------------------------------


def _bar_at(g: transitions.Grid, deck: dict, beat: float) -> float:
    """Track bar a plan's deck plays at master beat `beat`, through its loops."""
    return _bar(g, float(mixplan.deck_time(deck, beat)))


def test_short_outro_blends_16_bars_with_an_a_loop() -> None:
    """An 8-bar drum outro over a 16-bar drum intro: B comes in on A's outro and A loops it for the second half,
    instead of an 8-bar blend or one starting 8 bars into A's body."""
    tr = transitions.suggest(track(OUTRO_8), analysis(OUTRO_8), track(STANDARD), analysis(STANDARD))
    ga, _ = _grids(OUTRO_8, STANDARD)
    assert ga.outro == (120, 128)
    assert (tr["technique"], tr["overlap_bars"], tr["a_loop_bars"], tr["b_loop_bars"]) == ("long blend", 16, 8, 0)
    assert _on_bar(_bar(ga, tr["out_start_s"])) == 120
    assert any("loop its bars 121–128" in n for n in tr["notes"])

    plan = _generate(OUTRO_8, STANDARD)
    assert (plan["technique"], plan["bars"]) == ("long blend", 16)
    assert plan["a"]["loops"] == [{"start_beat": 32.0, "end_beat": 64.0, "source_beat": 0.0, "length_beats": 32.0}]
    a = plan["a"]
    for beat in np.arange(0.0, 64.0, 0.5):  # A plays its outro, then its outro again, never past its music
        bar = _bar_at(ga, a, beat)
        assert 120 - 1e-3 <= bar < 128, beat
        assert bar == pytest.approx(120 + (beat / 4) % 8, abs=1e-3)
    assert {"beat": 32.0, "label": "A loop"} in plan["markers"]
    assert not any("A runs out" in n for n in plan["notes"])
    mixplan.validate(plan)


def test_short_intro_is_looped_from_b_cue() -> None:
    """An 8-bar drum intro under A's music outro: B loops it for the first 8 bars, releases on the phrase and its
    intro then runs as A leaves, so B's main section never plays over A."""
    tr = transitions.suggest(track(MUSIC_OUTRO), analysis(MUSIC_OUTRO), track(SHORT_INTRO), analysis(SHORT_INTRO))
    assert (tr["overlap_bars"], tr["a_loop_bars"], tr["b_loop_bars"]) == (16, 0, 8)
    assert tr["in_start_s"] == 0.0

    plan = _generate(MUSIC_OUTRO, SHORT_INTRO)
    _, gb = _grids(MUSIC_OUTRO, SHORT_INTRO)
    b = plan["b"]
    assert b["loops"] == [{"start_beat": 0.0, "end_beat": 32.0, "source_beat": 32.0, "length_beats": 32.0}]
    assert b["play_from_beat"] == 0.0
    assert _bar_at(gb, b, 0.0) == pytest.approx(0.0, abs=1e-3), "B starts on its cue"
    assert _bar_at(gb, b, 31.9) == pytest.approx(7.975, abs=1e-3)
    assert _bar_at(gb, b, 32.0) == pytest.approx(0.0, abs=1e-3), "released on the 1, B plays on from its cue"
    assert _bar_at(gb, b, 63.9) < gb.intro[1], "B's main section waits until A is out"
    assert {"beat": 32.0, "label": "B loop out"} in plan["markers"]
    assert "cue: A bar 112 (216.77 s) / B bar 0 (0.00 s)" in mixplan.summary(plan, analysis(MUSIC_OUTRO),
                                                                                analysis(SHORT_INTRO))
    mixplan.validate(plan)


@pytest.mark.parametrize(("name", "a", "b"), PAIRS, ids=IDS)
def test_loops_release_on_a_phrase(name: str, a: Spec, b: Spec) -> None:
    """A blend's loops engage and release on the phrase grid: B's after whole 8-bar phrases from its cue, A's on a
    4-bar line of its music and run until A leaves on the blend's last 1."""
    plan = _generate(a, b)
    if plan["technique"] in ("loop roll", "slam", "drop swap", "quick cut"):
        return
    E = 4.0 * plan["bars"]
    for lp in plan["a"]["loops"]:
        assert lp["start_beat"] % 16 == 0 and lp["end_beat"] == E and lp["length_beats"] in (16.0, 32.0)
        assert (lp["end_beat"] - lp["start_beat"]) % lp["length_beats"] == 0
    for lp in plan["b"]["loops"]:
        assert lp["start_beat"] == 0.0 and lp["end_beat"] % 32 == 0 and lp["end_beat"] <= E / 2
        assert lp["end_beat"] % lp["length_beats"] == 0


def test_no_loop_of_a_sung_outro() -> None:
    sung = Spec("sung_outro_8", outro_bars=8, vocals=[(120, 128)])
    ga, _ = _grids(sung, STANDARD)
    assert transitions.outro_loop(ga, 120, 16) is None
    tr = transitions.suggest(track(sung), analysis(sung), track(STANDARD), analysis(STANDARD))
    assert tr["a_loop_bars"] == 0
    assert _on_bar(_bar(ga, tr["out_start_s"])) + tr["overlap_bars"] <= ga.end


def test_extend_with_loop_picks_the_cleanest_phrase() -> None:
    # a 16-bar drum outro sung over its last 8 bars: A loops the first 8, on its 4-bar grid
    g, _ = _grids(Spec("half_sung", outro_bars=16, vocals=[(120, 128)]), STANDARD)
    lp = transitions._extend_with_loop(g, "outro", 128, 8, 8)
    assert lp == transitions.Loop(src=112, size=8, at=8, bars=8)
    assert transitions._extend_with_loop(g, "outro", 128, 4, 12).size == 4  # 4 bars to make up: a 4-bar loop
    assert transitions._extend_with_loop(g, "outro", 128, 6) is None  # not on the 4-bar grid
    # loops never run more than half the blend
    ga, _ = _grids(OUTRO_8, STANDARD)
    assert transitions.outro_loop(ga, 120, 32) is None and transitions.outro_loop(ga, 120, 16).bars == 8


def test_looped_bars_cost_more_past_one_phrase() -> None:
    """A 16-bar drum outro into a 32-bar drum intro stays a 16-bar blend at the normal length (32 needs A to loop 16
    bars, twice its last phrase); at the long length the 32 runs from A's body, not over a loop."""
    for pref, (bars, xa) in (("normal", (16, 0)), ("long", (32, 0))):
        tr = transitions.suggest(track(STANDARD), analysis(STANDARD), track(LONG_OUTRO), analysis(LONG_OUTRO),
                                 mix_length=pref)
        assert (tr["overlap_bars"], tr["a_loop_bars"]) == (bars, xa), pref


def test_settings_mix_length() -> None:
    assert settings.Settings().mix_length == "normal"
    assert settings.from_json({"mix_length": "long"}).mix_length == "long"
    assert settings.from_json({"mix_length": "huge"}).mix_length == "normal"
    with pytest.raises(ValueError, match="mix_length"):
        settings.from_json({"mix_length": "huge"}, strict=True)
    assert settings.to_json(settings.from_json({"mix_length": "short"}))["mix_length"] == "short"


def _send_at(plan: dict, lane: str, beats: list[float]) -> list[float]:
    return [round(float(mixplan.lane_value(plan["a"]["lanes"][lane], b)), 3) for b in beats]


def test_throws_keep_feeding_the_sends_past_the_cut() -> None:
    an = analysis(STANDARD)
    short = mixplan.generate(track(STANDARD), an, track(FAST), analysis(FAST), "f")
    assert 0 < short["a"]["play_to_beat"] < 2.0  # A's file ends just after the cut: it plays to its end
    an["duration_s"] += 10.0
    plan = mixplan.generate(track(STANDARD), an, track(FAST), analysis(FAST), "f")
    assert plan["technique"] == "echo out" and plan["a"]["play_to_beat"] == 2.0
    assert mixplan.lane_value(plan["a"]["lanes"]["volume"], 0.0) == 0.0  # A's fader is down on the cut ...
    echo, reverb = _send_at(plan, "echo_send", [-1, 0.5, 2]), _send_at(plan, "reverb_send", [0.5, 2])
    assert echo[0] > 0.3 and echo[1] >= 0.9 and echo[2] == 0.0  # ... but its sends get a beat more of it
    assert reverb[0] >= 0.6 and reverb[1] == 0.0


def test_blend_echo_throw_at_the_end_of_a_vocal_phrase() -> None:
    a = Spec("vocal_ends", vocals=[(64, 120)])  # A's vocals stop 8 bars into the 16-bar blend from bar 112
    plan = mixplan.generate(track(a), analysis(a), track(STANDARD), analysis(STANDARD), "f", "long blend", 16)
    assert {"beat": 31.0, "label": "echo throw"} in plan["markers"]
    assert _send_at(plan, "echo_send", [30.0, 31.5, 33.0]) == [0.0, 0.8, 0.0]
    assert any("echo throw" in n for n in plan["notes"])


def test_mid_throws_on_breakdown_exits_and_spaced() -> None:
    a = Spec("a_drops", vocals=[(96, 104), (106, 109)], drops=[104])
    b = Spec("b_drop", intro_bars=4, drops=[12])
    m = mixplan.make_mix(track(a), analysis(a), track(b), analysis(b), None)
    # A's vocal phrase ends where it drops (overlap bar 4: one throw) and B drops on bar 12 (drops go first); the
    # vocal phrase ending on bar 9 would be a third, over the cap
    assert mixplan._mid_throws(m, mixplan._Place(100, 0, 16, None)) == [16.0, 48.0]
    assert mixplan._mid_throws(m, mixplan._Place(100, 20, 16, None)) == [16.0, 36.0]  # B's drop outside the blend
    assert mixplan._mid_throws(m, mixplan._Place(100, 0, 4, None)) == []  # no room inside a 4-bar blend


def test_reverb_wash_swells_then_cuts() -> None:
    plan = mixplan.generate(track(STANDARD), analysis(STANDARD), track(LONG_OUTRO), analysis(LONG_OUTRO), "f",
                            "reverb wash")
    assert plan["bars"] == 8 and plan["a"]["reverb_size"] > mixplan.REVERB_SIZE
    assert _send_at(plan, "reverb_send", [-32, -16, 0, 2]) == [0.0, 0.45, 0.9, 0.0]
    assert _send_at(plan, "filter", [-32, 0]) == [0.0, 0.55]
    assert mixplan.lane_value(plan["a"]["lanes"]["volume"], 0.0) == 0.0
    short = mixplan.generate(track(STANDARD), analysis(STANDARD), track(LONG_OUTRO), analysis(LONG_OUTRO), "f",
                             "reverb wash", 4)
    assert _send_at(short, "reverb_send", [-17, -16]) == [0.0, 0.0] and _send_at(short, "reverb_send", [-8])[0] > 0


def test_validate_send_taps_and_reverb_size() -> None:
    plan = mixplan.generate(track(STANDARD), analysis(STANDARD), track(LONG_OUTRO), analysis(LONG_OUTRO), "f")
    assert plan["a"]["echo_tap"] == plan["a"]["reverb_tap"] == "pre_eq"
    p = copy.deepcopy(plan)
    p["a"].update(echo_tap="post_fader", reverb_tap="nonsense", reverb_size=3.0)
    v = mixplan.validate(p)
    assert (v["a"]["echo_tap"], v["a"]["reverb_tap"], v["a"]["reverb_size"]) == ("post_fader", "pre_eq", 1.0)
