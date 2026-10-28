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
CASES = {
    "same tempo blend": (A, B_SAME, None),
    "small gap blend": (A, B_FASTER, None),
    "big gap echo out": (A, B_FAST, None),
    "filter sweep": (A, B_SAME, "filter sweep"),
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
        out[name] = (plan, mixrender._render_audio(plan, paths[0], paths[1], SR))
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
    # B is silent until its fader opens at master beat 0
    i0 = int(r["beat_times_s"][int(-np.ceil(r["start_beat"]))] * SR)
    assert np.abs(r["stems"]["B"][:, : max(0, i0 - int(0.01 * SR))]).max() < 1e-3


def test_tempo_follows_the_plan(renders: dict) -> None:
    plan, r = renders["big gap echo out"]
    gaps = np.diff(r["beat_times_s"])
    assert gaps[0] == pytest.approx(60.0 / A.bpm, rel=1e-3)
    assert gaps[-1] == pytest.approx(60.0 / B_FAST.bpm, rel=1e-3)
