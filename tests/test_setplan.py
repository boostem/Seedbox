"""Set-level planning (musicreco.setplan): a set varies its transitions instead of repeating each pair's own pick."""

from __future__ import annotations

from musicreco import mixplan, setplan, transitions

from .fixtures import SET_12, set_tracks


def _set(entries=SET_12):
    tracks, analyses = set_tracks(entries)
    trs = transitions.suggest_set(tracks, analyses)
    return tracks, analyses, trs, setplan.plan_set(tracks, analyses, trs)


def test_twelve_track_set_varies_its_transitions():
    tracks, analyses, trs, picks = _set()
    techs = [p.technique for p in picks]
    assert len(picks) == 11
    assert [p.own for p in picks].count("long blend") == 10  # on its own nearly every pair would long-blend
    assert len(set(techs)) >= 3, techs
    for x, y in zip(techs, techs[1:]):
        assert x != y or x in setplan.BLENDS, f"{x} twice in a row: {techs}"
    for x, y in zip(techs, techs[1:]):
        assert not (x in setplan.FAMILY and setplan.FAMILY.get(y) == setplan.FAMILY[x]), techs
    assert techs.count("long blend") > len(techs) / 2  # long blends stay the default


def test_twelve_track_set_is_paced():
    _, _, _, picks = _set()
    techs = [p.technique for p in picks]
    run = 0
    for t in techs:
        run = 0 if t in setplan.BREAKERS else run + 1
        assert run <= setplan._PACE_EVERY + 1, f"{run} transitions without a cut or echo out: {techs}"
    assert techs == [
        "long blend", "long blend", "long blend", "echo out", "long blend", "slam", "long blend", "reverb wash",
        "long blend", "long blend", "long blend",
    ]  # fmt: skip
    # the cut that isn't the pair's own pick comes at the set's energy peak, with a note saying so
    peak = picks[7]
    assert peak.own == "long blend" and "energy peak" in (peak.reason or "")


def test_every_pick_plans_as_that_technique():
    tracks, analyses, trs, picks = _set()
    shown = setplan.annotate(tracks, analyses, trs)
    for a, b, p, tr, own in zip(tracks, tracks[1:], picks, shown, trs):
        plan = mixplan.generate(a, analyses[a.id], b, analyses[b.id], "x", technique=p.technique)
        assert plan["technique"] == tr["plan_technique"] == p.technique
        assert tr["plan_bars"] == (0 if p.technique in setplan.CUTS else plan["bars"])
        assert tr["notes"] == [*own["notes"], *([p.reason] if p.reason else [])]


def test_picks_depend_only_on_the_set():
    """Regenerating one transition (any technique) never feeds back: the other pairs keep their picks."""
    tracks, analyses, trs, picks = _set()
    a, b = tracks[3], tracks[4]
    mixplan.generate(a, analyses[a.id], b, analyses[b.id], "x", technique="quick cut")
    assert setplan.plan_set(tracks, analyses, trs) == picks
    assert setplan.plan_set(tracks, analyses) == picks  # without suggest_set's output too


def test_unanalysed_pairs_have_no_pick():
    tracks, analyses = set_tracks(SET_12[:4])
    analyses[tracks[2].id] = None
    picks = setplan.plan_set(tracks, analyses)
    assert [p.technique is None for p in picks] == [False, True, True]
    assert picks[0].technique == "long blend"


def test_choose_never_repeats_a_cut_and_breaks_up_blends():
    opts = {"long blend": 0.0, "echo out": 3.0, "quick cut": 5.0, "slam": 2.0}
    techs = setplan.choose([dict(opts) for _ in range(20)], [0.5] * 20)
    assert all(not (x == y and x not in setplan.BLENDS) for x, y in zip(techs, techs[1:]))
    gaps = [i for i, t in enumerate(techs) if t in setplan.BREAKERS]
    assert gaps and max(y - x for x, y in zip([-1, *gaps], [*gaps, len(techs)])) <= setplan._PACE_EVERY + 2


def test_choose_cuts_more_at_energy_peaks():
    opts = {"long blend": 0.0, "echo out": 3.0, "quick cut": 5.0}
    quiet = setplan.choose([dict(opts) for _ in range(12)], [0.0] * 12)
    peak = setplan.choose([dict(opts) for _ in range(12)], [1.0] * 12)
    assert quiet.count("quick cut") < peak.count("quick cut"), (quiet, peak)


def test_choose_takes_the_only_option():
    assert setplan.choose([{"slam": 0.0}, {"slam": 0.0}], [0.5, 0.5]) == ["slam", "slam"]
    assert setplan.choose([], []) == []
