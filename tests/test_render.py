"""mixrender._render_audio on short click tracks with known grids: beats land on the master clock on both decks, the
mix doesn't clip, the stems are there, and the echo / reverb returns are clearly audible where A is thrown out."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from scipy.io import wavfile

from musicreco import mixplan, mixrender
from musicreco.paths import ffmpeg_path

from .fixtures import Spec, analysis, click_offset, click_track, pad_track, track

pytestmark = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")

SR = 44100
A = Spec("click_a", bpm=124.0, bars=40, intro_bars=8, outro_bars=16)
B_SAME = Spec("click_b", bpm=124.0, bars=40, intro_bars=16, outro_bars=8, first_downbeat_s=0.25)
B_FASTER = Spec("click_b_faster", bpm=126.5, bars=40, intro_bars=16, outro_bars=8)
B_FAST = Spec("click_b_fast", bpm=140.0, bars=40, intro_bars=16, outro_bars=8)
A_BUILD = Spec("click_a_build", bpm=124.0, bars=40, intro_bars=8, outro_bars=8, outro_drums=False, drops=[24])
B_DROP = Spec("click_b_drop", bpm=124.0, bars=40, intro_bars=8, drops=[16])
A_OUTRO_8 = Spec("click_a_outro_8", bpm=124.0, bars=40, intro_bars=8, outro_bars=8)  # A loops its outro
A_MUSIC_OUTRO = Spec("click_a_music", bpm=124.0, bars=40, intro_bars=8, outro_bars=16, outro_drums=False)
B_INTRO_8 = Spec("click_b_intro_8", bpm=124.0, bars=40, intro_bars=8, outro_bars=8, first_downbeat_s=0.25)  # B loops
CASES = {
    "same tempo blend": (A, B_SAME, None),
    "small gap blend": (A, B_FASTER, None),
    "big gap echo out": (A, B_FAST, None),
    "filter sweep": (A, B_SAME, "filter sweep"),
    "slam into drop": (A_BUILD, B_DROP, None),
    "big gap slam": (A_BUILD, B_FAST, None),
    "a loop blend": (A_OUTRO_8, B_SAME, None),
    "b loop blend": (A_MUSIC_OUTRO, B_INTRO_8, None),
    "loop roll": (A, B_SAME, "loop roll"),
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
        assert "A reverb" in r["stems"]
    if any(k["value"] > 0 for k in plan["a"]["lanes"]["echo_send"]):
        assert "A echo" in r["stems"]
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


# A with a sung phrase ending 8 bars into the blend, and both decks carrying a sustained chord for the sends to catch.
# Their files run 4 bars past the analysed last bar, so A has audio to feed its sends past the exit.
A_PAD = Spec("pad_a", bpm=124.0, bars=48, intro_bars=8, outro_bars=16, vocals=[(20, 40)])
B_PAD = Spec("pad_b", bpm=124.0, bars=40, intro_bars=16, outro_bars=8)
B_PAD_FAST = Spec("pad_b_fast", bpm=140.0, bars=40, intro_bars=16, outro_bars=8)
SEND_CASES = {
    "blend": (A_PAD, B_PAD, None, {}),
    "blend, sends post-fader": (A_PAD, B_PAD, None, {"echo_tap": "post_fader", "reverb_tap": "post_fader"}),
    "echo out": (A_PAD, B_PAD_FAST, None, {}),
    "reverb wash": (A_PAD, B_PAD, "reverb wash", {}),
    "loop roll": (A_PAD, B_PAD, "loop roll", {}),
}
# minimum level of a return over the 4 beats after A's exit, dB against A's untouched dry signal (before this
# change a blend had echo -24 dB, reverb -23.5 dB; now about -7 and -5)
ECHO_MIN_DB, REVERB_MIN_DB = -14.0, -12.0


@pytest.fixture(scope="module")
def send_renders(tmp_path_factory: pytest.TempPathFactory) -> dict:
    d = tmp_path_factory.mktemp("pads")
    out = {}
    for name, (a, b, tech, deck) in SEND_CASES.items():
        paths = []
        for s, pitch in ((a, 1000.0), (b, 3000.0)):
            p = d / f"{s.id}.wav"
            if not p.exists():
                wavfile.write(p, SR, pad_track(dataclasses.replace(s, bars=s.bars + 4), SR, pitch).T)
            paths.append(p)
        an_a, an_b = analysis(a), analysis(b)
        for s, an in ((a, an_a), (b, an_b)):
            an["duration_s"] += 4 * s.bar_s
        plan = mixplan.generate(track(a), an_a, track(b), an_b, "f", technique=tech)
        plan["a"].update(deck)
        out[name] = (plan, mixrender._render_audio(plan, paths[0], paths[1], SR))
    return out


def _level_db(r: dict, stem: str, b0: float, b1: float) -> float:
    """RMS (dB) of a stem over master beats [b0, b1) against A's dry stem over the window's first 4 beats."""
    clock, x = r["clock"], r["stems"].get(stem)

    def rms(y: np.ndarray, c0: float, c1: float) -> float:
        i0, i1 = (int(round(float(clock.time(c)) * SR)) for c in (c0, c1))
        return 10 * np.log10(np.mean(y[:, i0:i1].astype(np.float64) ** 2) + 1e-20)

    w0 = r["start_beat"]
    return (rms(x, b0, b1) if x is not None else -200.0) - rms(r["stems"]["A"], w0, w0 + 4)


def _exit(plan: dict) -> float:
    return max(m["beat"] for m in plan["markers"] if m["label"] in ("A out", "cut"))


@pytest.mark.parametrize("name", ["blend", "echo out", "reverb wash", "loop roll"])
def test_returns_are_audible_after_the_throw(send_renders: dict, name: str) -> None:
    plan, r = send_renders[name]
    E = _exit(plan)
    assert plan["a"]["play_to_beat"] == E + 2  # A keeps feeding its sends past the exit
    assert _level_db(r, "A reverb", E, E + 4) > REVERB_MIN_DB
    if name != "reverb wash":
        assert _level_db(r, "A echo", E, E + 4) > ECHO_MIN_DB
    assert _level_db(r, "A", E + 0.1, E + 4) < -60  # while A's own fader is down


def test_reverb_wash_builds_before_the_cut(send_renders: dict) -> None:
    _, r = send_renders["reverb wash"]
    levels = [_level_db(r, "A reverb", b, b + 4) for b in (-32, -16, -4)]
    assert levels[0] < levels[1] < levels[2] and levels[2] > REVERB_MIN_DB


def test_mid_blend_echo_throw_is_audible(send_renders: dict) -> None:
    plan, r = send_renders["blend"]
    throws = [m["beat"] for m in plan["markers"] if m["label"] == "echo throw"]
    assert throws, "A's vocal phrase ends inside the blend: expected an echo throw there"
    t = throws[0] + 1  # the 1 the throw goes into
    assert _level_db(r, "A echo", t, t + 3) > ECHO_MIN_DB
    assert _level_db(r, "A echo", t - 8, t - 2) < -40  # nothing on the echo before it


def test_pre_eq_sends_keep_what_the_eq_cuts(send_renders: dict) -> None:
    plan, pre = send_renders["blend"]
    _, post = send_renders["blend, sends post-fader"]
    E = _exit(plan)
    for stem in ("A echo", "A reverb"):
        assert _level_db(pre, stem, E, E + 4) > _level_db(post, stem, E, E + 4) + 6, stem


def test_loops_play_on_the_beat(renders: dict) -> None:
    """A loops its outro past the end of its file and B loops its intro from its cue: both still click on every
    master beat (test_beats_line_up_on_both_decks), A keeps playing after its file ends and B is silent before 0."""
    plan, r = renders["a loop blend"]
    assert plan["bars"] == 16 and plan["a"]["loops"] and not plan["b"]["loops"]
    lp = plan["a"]["loops"][0]
    t = np.asarray(r["beat_times_s"])
    first = np.ceil(r["start_beat"])
    looped = np.arange(lp["start_beat"] + 1, 4 * plan["bars"] - 16)  # A at full volume inside its loop
    assert float(mixplan.deck_time(plan["a"], looped[-1])) < A_OUTRO_8.duration_s
    assert lp["start_beat"] + plan["a"]["beat0_s"] * 124.0 / 60.0 >= A_OUTRO_8.bars * 4, "the loop starts at A's end"
    err = [click_offset(r["stems"]["A"], SR, t[int(b - first)]) for b in looped]
    assert max(err) < 0.003

    plan, r = renders["b loop blend"]
    assert plan["bars"] == 16 and plan["b"]["loops"] and not plan["a"]["loops"]
    assert plan["b"]["loops"][0]["end_beat"] == 32.0 and plan["b"]["play_from_beat"] == 0.0
    assert float(mixplan.deck_time(plan["b"], 0.0)) == pytest.approx(B_INTRO_8.first_downbeat_s, abs=1e-3)
    assert float(mixplan.deck_time(plan["b"], 32.0)) == pytest.approx(B_INTRO_8.first_downbeat_s, abs=1e-3)


def test_loop_roll_repeats_a_downbeat_down_to_half_beats(renders: dict) -> None:
    """A 16-bar blend ending in a 4-bar roll: A's roll source (a downbeat) clicks again on the last half beat, where
    A's own music has no click, and not between the beats of the 1-beat step before it."""
    plan, r = renders["loop roll"]
    E = 4.0 * plan["bars"]
    assert plan["bars"] == 16 and [lp["length_beats"] for lp in plan["a"]["loops"]] == [8.0, 4.0, 2.0, 1.0, 0.5]
    t = np.asarray(r["beat_times_s"])
    first = np.ceil(r["start_beat"])

    def at(beat: float) -> float:
        i = int(beat - first)
        return float(t[i] + (beat - first - i) * (t[i + 1] - t[i]))

    assert click_offset(r["stems"]["A"], SR, at(E - 0.5), search_s=0.02) < 0.003  # the ½-beat roll
    assert click_offset(r["stems"]["A"], SR, at(E - 1.5), search_s=0.02) > 0.01  # the 1-beat step: nothing there
