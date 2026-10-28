"""mixrender._render_audio on short click tracks with known grids: beats land on the master clock on both decks, the
mix doesn't clip, the stems are there."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.io import wavfile

from musicreco import mixplan, mixrender
from musicreco.paths import ffmpeg_path

from .fixtures import Spec, analysis, click_offset, click_track, track

pytestmark = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")

SR = 44100
A = Spec("click_a", bpm=124.0, bars=40, intro_bars=8, outro_bars=16)
B_SAME = Spec("click_b", bpm=124.0, bars=40, intro_bars=16, outro_bars=8, first_downbeat_s=0.25)
B_FASTER = Spec("click_b_faster", bpm=126.5, bars=40, intro_bars=16, outro_bars=8)
B_FAST = Spec("click_b_fast", bpm=140.0, bars=40, intro_bars=16, outro_bars=8)
A_BUILD = Spec("click_a_build", bpm=124.0, bars=40, intro_bars=8, outro_bars=8, outro_drums=False, drops=[24])
B_DROP = Spec("click_b_drop", bpm=124.0, bars=40, intro_bars=8, drops=[16])
CASES = {
    "same tempo blend": (A, B_SAME, None),
    "small gap blend": (A, B_FASTER, None),
    "big gap echo out": (A, B_FAST, None),
    "filter sweep": (A, B_SAME, "filter sweep"),
    "slam into drop": (A_BUILD, B_DROP, None),
    "big gap slam": (A_BUILD, B_FAST, None),
}


@pytest.fixture(scope="module")
def renders(tmp_path_factory: pytest.TempPathFactory) -> dict:
    d = tmp_path_factory.mktemp("clicks")
    out = {}
    for name, (a, b, tech) in CASES.items():
        paths = []
        for s, pitch in ((a, 1000.0), (b, 3000.0)):
            p = d / f"{s.id}.wav"
            if not p.exists():
                wavfile.write(p, SR, click_track(s, SR, pitch).T)
            paths.append(p)
        plan = mixplan.generate(track(a), analysis(a), track(b), analysis(b), "f", technique=tech)
        out[name] = (plan, mixrender._render_audio(plan, paths[0], paths[1], SR) | {"paths": paths})
    return out


def _deck_beats(plan: dict, r: dict, deck: str) -> np.ndarray:
    """Master beats where `deck` plays at full volume (where its clicks are clear in its stem)."""
    beats = np.arange(np.ceil(r["start_beat"]), plan["window"]["end_beat"])
    d = plan[deck]
    vol = mixplan.lane_value(d["lanes"]["volume"], beats)
    return beats[(vol > 0.99) & (beats >= d["play_from_beat"] + 1) & (beats <= d["play_to_beat"] - 1)]


@pytest.mark.parametrize("name", CASES)
def test_beats_line_up_on_both_decks(renders: dict, name: str) -> None:
    plan, r = renders[name]
    t_beats = np.asarray(r["beat_times_s"])
    first = np.ceil(r["start_beat"])
    for deck, stem in (("a", "A"), ("b", "B")):
        beats = _deck_beats(plan, r, deck)
        assert beats.size >= 8, f"deck {deck} barely plays at full volume"
        expect = t_beats[(beats - first).astype(int)]
        err = np.array([click_offset(r["stems"][stem], SR, t) for t in expect])
        assert np.max(err) < 0.003, f"deck {deck}: a beat is {1000 * np.max(err):.1f} ms off the master clock"


@pytest.mark.parametrize("name", CASES)
def test_no_clipping(renders: dict, name: str) -> None:
    _, r = renders[name]
    assert np.all(np.isfinite(r["mix"]))
    assert np.abs(r["mix"]).max() <= 1.0
    assert r["true_peak_db"] <= mixrender.CEILING_DBTP + 0.3


@pytest.mark.parametrize("name", CASES)
def test_stems_present(renders: dict, name: str) -> None:
    plan, r = renders[name]
    assert {"A", "B"} <= set(r["stems"])
    for stem in r["stems"].values():
        assert stem.shape == r["mix"].shape and np.abs(stem).max() > 1e-3
    if any(k["value"] > 0 for k in plan["a"]["lanes"]["reverb_send"]):
        assert "reverb" in r["stems"]
    assert r["duration_s"] == pytest.approx(r["mix"].shape[1] / SR)
    # B is silent until it starts: on master beat 0, or a bar before for a slam's pre-roll
    i0 = int(r["beat_times_s"][int(min(0.0, plan["b"]["play_from_beat"]) - np.ceil(r["start_beat"]))] * SR)
    assert np.abs(r["stems"]["B"][:, : max(0, i0 - int(0.01 * SR))]).max() < 1e-3


def test_tempo_follows_the_plan(renders: dict) -> None:
    plan, r = renders["big gap echo out"]
    gaps = np.diff(r["beat_times_s"])
    assert gaps[0] == pytest.approx(60.0 / A.bpm, rel=1e-3)
    assert gaps[-1] == pytest.approx(60.0 / B_FAST.bpm, rel=1e-3)


def test_slams_are_planned(renders: dict) -> None:
    assert renders["slam into drop"][0]["technique"] == renders["big gap slam"][0]["technique"] == "slam"
    assert renders["slam into drop"][0]["b"]["play_from_beat"] == -4.0  # B's pre-roll bar


def test_big_gap_slam_jumps_tempo_without_stretching(renders: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both decks play at their own tempo on either side of the cut: neither goes through the time stretch (no
    stretch artefacts), and the beat grid jumps from A's tempo to B's on the 1."""
    import pedalboard

    def no_stretch(*args: object, **kw: object) -> None:
        raise AssertionError("a deck was time-stretched")

    plan, r = renders["big gap slam"]
    monkeypatch.setattr(pedalboard, "time_stretch", no_stretch)
    again = mixrender._render_audio(plan, *r["paths"], SR)
    assert np.array_equal(again["mix"], r["mix"])
    assert not [w for w in again["warnings"] if "stretch" in w]
    gaps = np.diff(r["beat_times_s"])
    cut = int(-np.ceil(r["start_beat"]))
    assert gaps[cut - 1] == pytest.approx(60.0 / A_BUILD.bpm, rel=1e-3)
    assert gaps[cut] == pytest.approx(60.0 / B_FAST.bpm, rel=1e-3)
