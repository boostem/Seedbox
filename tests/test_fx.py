"""Deck effects (spinback, brake, stutter): the plan events, the planner's cut endings that use them, the renderer's
DSP on known signals, and rendered transitions where A ends in each."""

from __future__ import annotations

import copy
import dataclasses

import numpy as np
import pytest
from scipy.io import wavfile

from musicreco import mixplan, mixrender
from musicreco.paths import ffmpeg_path

from .fixtures import Spec, analysis, pad_track, track

SR = 44100
BPM = 120.0  # 0.5 s a beat
A_PAD = Spec("fx_a", bpm=124.0, bars=48, intro_bars=8, outro_bars=16)
B_PAD = Spec("fx_b", bpm=124.0, bars=40, intro_bars=16, outro_bars=8)


def _generate(tech: str, a: Spec = A_PAD, b: Spec = B_PAD) -> dict:
    return mixplan.generate(track(a), analysis(a), track(b), analysis(b), "f", technique=tech)


# --- plan ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("tech", "fx"), [
    ("spinback", [("spinback", -1.0, 0.0, None)]),
    ("brake", [("brake", -2.0, 0.0, None)]),
    ("stutter", [("stutter", -4.0, -2.0, 0.5), ("stutter", -2.0, -1.0, 0.25), ("stutter", -1.0, 0.0, 0.125)]),
])  # fmt: skip
def test_fx_techniques_end_a_in_their_effect_on_a_cut(tech: str, fx: list) -> None:
    plan = _generate(tech)
    assert plan["technique"] == tech and tech in mixplan.CUTS
    assert [(e["type"], e["start_beat"], e["end_beat"], e.get("div")) for e in plan["a"]["fx"]] == fx
    assert plan["b"]["fx"] == []
    assert mixplan.lane_value(plan["a"]["lanes"]["volume"], 0.0) == 0.0  # A is cut on the 1
    assert mixplan.lane_value(plan["b"]["lanes"]["volume"], 0.0) == 1.0  # B is straight in
    assert {"beat": fx[0][1], "label": tech} in plan["markers"]
    assert f"→ {tech}: " in plan["notes"][0]
    assert f"A fx: {tech}" in mixplan.summary(plan)


def test_slow_brake_is_one_beat() -> None:
    slow = [dataclasses.replace(s, id=f"{s.id}_slow", bpm=90.0) for s in (A_PAD, B_PAD)]
    plan = _generate("brake", *slow)
    assert [(e["start_beat"], e["end_beat"]) for e in plan["a"]["fx"]] == [(-1.0, 0.0)]


def test_validate_normalises_fx() -> None:
    plan = copy.deepcopy(_generate("quick cut"))
    plan["a"]["fx"] = [
        {"type": "stutter", "start_beat": -2, "end_beat": 0, "div": 9},
        {"type": "brake", "start_beat": -4, "end_beat": -2},
    ]
    del plan["b"]["fx"]
    v = mixplan.validate(plan)
    assert v["a"]["fx"] == [
        {"type": "brake", "start_beat": -4.0, "end_beat": -2.0},
        {"type": "stutter", "start_beat": -2.0, "end_beat": 0.0, "div": 1.0},
    ]
    assert v["b"]["fx"] == []


@pytest.mark.parametrize("fx", [
    [{"type": "scratch", "start_beat": 0, "end_beat": 1}],
    [{"type": "brake", "start_beat": 1, "end_beat": 1}],
    [{"type": "brake", "start_beat": 0, "end_beat": 40}],
    [{"type": "brake", "start_beat": 0, "end_beat": 2}, {"type": "spinback", "start_beat": 1, "end_beat": 3}],
    "brake",
], ids=["unknown type", "empty", "too long", "overlap", "not a list"])  # fmt: skip
def test_validate_rejects_bad_fx(fx: object) -> None:
    plan = copy.deepcopy(_generate("quick cut"))
    plan["a"]["fx"] = fx
    with pytest.raises(ValueError):
        mixplan.validate(plan)


# --- DSP -----------------------------------------------------------------------------------------------------------


def _clock() -> mixrender._Clock:
    return mixrender._Clock([{"beat": 0.0, "value": BPM, "curve": "hold"}], 0.0, 16.0, 0.0)


def _at(beat: float) -> int:
    return int(round(beat * 60.0 / BPM * SR))


def _peak_hz(x: np.ndarray) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return float(np.fft.rfftfreq(x.size, 1 / SR)[np.argmax(spec)])


def test_brake_slows_the_pitch_to_a_stop_and_stays_silent() -> None:
    t = np.arange(_at(10)) / SR
    x = np.stack([np.sin(2 * np.pi * 1000 * t)] * 2).astype(np.float32)
    y = mixrender._deck_fx(x, [{"type": "brake", "start_beat": 4.0, "end_beat": 6.0}], _clock(), 0, SR)
    assert np.array_equal(y[:, : _at(4)], x[:, : _at(4)])  # untouched before
    n = 2048
    early, late = _peak_hz(y[0, _at(4) : _at(4) + n]), _peak_hz(y[0, _at(5.5) - n // 2 : _at(5.5) + n // 2])
    assert early == pytest.approx(1000, rel=0.08)
    assert late == pytest.approx(250, rel=0.15)  # 3/4 through: a quarter of the speed
    assert np.abs(y[:, _at(6) :]).max() == 0.0


def test_spinback_reads_the_record_backwards_to_a_stop() -> None:
    n = _at(10)
    ramp = np.arange(n, dtype=np.float64) / n
    x = np.stack([ramp, ramp]).astype(np.float32)
    y = mixrender._deck_fx(x, [{"type": "spinback", "start_beat": 4.0, "end_beat": 5.0}], _clock(), 0, SR)
    i0, i1 = _at(4), _at(5)
    u = (np.arange(i0, i1) - i0) / (i1 - i0)
    pos = y[0, i0:i1] / np.maximum((1 - u) ** 1.5, 1e-9) * n  # undo the fade: the record position it read
    keep = u < 0.9
    assert np.all(np.diff(pos[keep]) < 0)  # backwards all the way
    assert pos[0] == pytest.approx(i0, abs=2)
    assert pos[keep][-1] == pytest.approx(i0 - mixrender.SPINBACK_SPEED / 3 * (i1 - i0), rel=0.01)
    speed = -np.diff(pos[keep])
    assert speed[0] == pytest.approx(mixrender.SPINBACK_SPEED, rel=0.01) and speed[-1] < 0.05
    assert np.abs(y[:, i1:]).max() == 0.0


def test_stutter_repeats_its_first_slice_gated() -> None:
    n = _at(10)
    ramp = np.arange(n, dtype=np.float64) / n
    x = np.stack([ramp, ramp]).astype(np.float32)
    y = mixrender._deck_fx(x, [{"type": "stutter", "start_beat": 4.0, "end_beat": 6.0, "div": 0.5}], _clock(), 0, SR)
    rep = _at(0.5)
    i0 = _at(4)
    first = y[0, i0 : i0 + rep]
    for k in range(1, 4):
        assert np.allclose(y[0, i0 + k * rep : i0 + (k + 1) * rep], first, atol=1e-6)
    on = int(mixrender.STUTTER_DUTY * rep)
    mid = slice(i0 + int(0.01 * SR), i0 + on - int(0.01 * SR))
    assert np.allclose(y[0, mid], x[0, mid], atol=1e-6)  # the slice itself, open gate
    assert np.abs(y[:, i0 + on + 10 : i0 + rep]).max() == 0.0  # closed gate
    assert np.allclose(y[0, _at(6) + 200 :], x[0, _at(6) + 200 :])  # back where the record would have been


# --- rendered ------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fx_renders(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if ffmpeg_path() is None:
        pytest.skip("needs ffmpeg")
    d = tmp_path_factory.mktemp("fx")
    paths = []
    for s, pitch in ((A_PAD, 1000.0), (B_PAD, 3000.0)):
        p = d / f"{s.id}.wav"
        wavfile.write(p, SR, pad_track(s, SR, pitch).T)
        paths.append(p)
    return {t: (plan := _generate(t), mixrender.render_audio(plan, *paths, SR)) for t in mixplan.FX_TYPES}


def _db(r: dict, stem: str, b0: float, b1: float) -> float:
    clock, x = r["clock"], r["stems"].get(stem)
    if x is None:
        return -200.0
    i0, i1 = (int(round(float(clock.time(b)) * SR)) for b in (b0, b1))
    return float(10 * np.log10(np.mean(x[:, i0:i1].astype(np.float64) ** 2) + 1e-20))


@pytest.mark.parametrize("tech", mixplan.FX_TYPES)
def test_rendered_fx_endings(fx_renders: dict, tech: str) -> None:
    plan, r = fx_renders[tech]
    a0 = plan["a"]["fx"][0]["start_beat"]
    plain = _db(r, "A", a0 - 4, a0)
    assert _db(r, "A", 0.05, 2.0) < -80  # A is out after the cut
    assert _db(r, "B", 0.05, 4.0) > -30  # B is in
    send = "A echo" if tech == "stutter" else "A reverb"
    assert _db(r, send, 0.0, 2.0) > plain - 20  # the tail rings over the cut
    if tech == "stutter":  # the gate closes for the last 30 % of every repeat (the high-pass rings on a little)
        for b in np.arange(-4.0, -2.0, 0.5):
            assert _db(r, "A", b + 0.36, b + 0.48) < _db(r, "A", b + 0.02, b + 0.32) - 20
    else:  # fading as the record slows
        assert _db(r, "A", a0 * 0.25, 0.0) < _db(r, "A", a0, a0 * 0.75) - 6
    assert np.abs(r["mix"]).max() <= 1.0
