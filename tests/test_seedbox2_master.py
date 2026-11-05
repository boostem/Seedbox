"""The master chain (#58): track gain, glue, limiter, make-up to a target, what an export measures. That it plays the same
live is in `test_seedbox2_engine_parity.py`."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import numpy as np
import pytest
from scipy import signal
from scipy.io import wavfile

from musicreco import mixrender
from seedbox2.analysis import loudness
from seedbox2.library.service import Settings
from seedbox2.mix import master, offline
from seedbox2.mix import plan as v3

SR = mixrender.SR
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _noise(seconds: float, level_db: float, seed: int = 0) -> np.ndarray:
    x = np.random.default_rng(seed).normal(size=(2, int(seconds * SR)))
    return x / np.sqrt((x**2).mean()) * 10 ** (level_db / 20)


def _rms_db(x: np.ndarray) -> float:
    return float(10 * np.log10((x**2).mean()))


# --- track gain -------------------------------------------------------------------------------------------------------


def _curve(*parts: tuple[float, float]) -> dict:
    """A loudness analysis: (seconds, LUFS), in order."""
    return {"lufs": [lufs for seconds, lufs in parts for _ in range(int(seconds * 10))]}


def test_a_quiet_intro_does_not_raise_the_track():
    with_intro = loudness.track_gain_db(_curve((60.0, -26.0), (180.0, -8.0)))
    without = loudness.track_gain_db(_curve((180.0, -8.0)))
    assert with_intro == pytest.approx(-4.0, abs=0.1)
    assert with_intro == pytest.approx(without, abs=0.1)


def test_the_body_is_the_loudest_quarter_not_one_short_spike():
    assert 0.0 < loudness.track_gain_db(_curve((200.0, -14.0), (8.0, -6.0))) < 2.0  # the spike alone would say -6


def test_sections_decide_when_there_are_some():
    data = _curve((60.0, -20.0), (60.0, -10.0))
    spans = [{"kind": "intro", "start_s": 0.0, "end_s": 60.0}, {"kind": "drop", "start_s": 60.0, "end_s": 120.0}]
    assert loudness.track_gain_db(data, spans) == pytest.approx(-2.0, abs=0.1)


def test_track_gain_is_limited_and_silence_has_none():
    assert loudness.track_gain_db(_curve((60.0, -50.0))) == loudness.TRACK_GAIN_LIMIT_DB
    assert loudness.track_gain_db(_curve((60.0, -120.0))) is None
    assert loudness.track_gain_db({"lufs": []}) is None


def test_gain_from_audio_agrees_with_the_analysis():
    x = np.concatenate([_noise(40.0, -40.0), _noise(120.0, -10.0, 1)], axis=1).astype(np.float32)
    meter = loudness.Meter()
    meter.feed(signal.resample_poly(x, 160, 147, axis=1).T.astype(np.float32))
    assert offline.track_gain_db(x, SR) == pytest.approx(loudness.track_gain_db(meter.result()), abs=0.3)


# --- glue -------------------------------------------------------------------------------------------------------------


def test_glue_is_two_to_one_above_the_knee():
    x = _noise(5.0, master.GLUE_THRESHOLD_DB + 10.0)
    glue = master.Glue()
    gain = glue.gain(x)
    assert 20 * np.log10(gain[-SR:].mean()) == pytest.approx(-5.0, abs=0.4)


def test_glue_leaves_quiet_audio_alone():
    x = _noise(5.0, master.GLUE_THRESHOLD_DB - 10.0)
    assert np.all(master.Glue().gain(x) == 1.0)


def test_glue_is_slow():
    quiet, loud = _noise(2.0, -30.0), _noise(2.0, -2.0, 1)
    gain = master.Glue().gain(np.concatenate([quiet, loud], axis=1))
    edge = quiet.shape[1]
    db = 20 * np.log10(gain)
    settled = db[-SR // 2 :].mean()
    assert db[edge + int(0.005 * SR)] > 0.3 * settled  # 5 ms in it has barely moved
    assert db[edge + int(0.2 * SR)] < 0.8 * settled


# --- limiter ----------------------------------------------------------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize("kind", ["noise", "square", "near_nyquist"])
def test_the_limiter_holds_the_true_peak(kind):
    n = 3 * SR
    t = np.arange(n) / SR
    if kind == "noise":
        x = _noise(3.0, 0.0)
    elif kind == "square":
        x = np.stack([np.sign(np.sin(2 * np.pi * 997 * t))] * 2) * 1.4
    else:
        x = np.stack([np.sin(2 * np.pi * 0.45 * SR * t + 0.4)] * 2) * 1.2
    out, report = master.Master(-14.0, 0.0)(x.astype(np.float32))
    assert report["limiter_db"] > 0
    assert mixrender.true_peak_db(out) <= master.CEILING_DBTP + 0.1


def test_the_limiter_passes_what_is_under_the_ceiling_untouched_and_on_time():
    x = _noise(1.0, -30.0)
    limiter = master.Limiter()
    out = np.concatenate([limiter.run(x), limiter.flush()], axis=1)[:, limiter.latency :]
    assert limiter.deepest_db == 0.0
    assert np.allclose(out, x)


def test_chunks_change_nothing():
    x = _noise(4.0, -3.0).astype(np.float32)
    whole, _ = master.Master(-9.0)(x)
    chain = master.Master(-9.0)
    pieces, at, sizes = [], 0, [1, 63, 64, 65, 128, 1000, 5000, 50000]
    while at < x.shape[1]:
        size = sizes[len(pieces) % len(sizes)]
        pieces.append(chain.process(x[:, at : at + size]))
        at += size
    pieces.append(chain.flush())
    assert np.allclose(np.concatenate(pieces, axis=1)[:, chain.limiter.latency :], whole, atol=1e-5)


# --- the target -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("target", sorted(master.TARGETS))
def test_a_measured_makeup_meets_the_target(target):
    lufs = master.TARGETS[target]
    x = (np.concatenate([_noise(10.0, -14.0), _noise(20.0, -11.0, 1)], axis=1)).astype(np.float32)
    makeup = master.find_makeup(x, lufs)
    out, _ = master.Master(lufs, makeup)(x)
    assert mixrender.integrated_lufs(out, SR) == pytest.approx(lufs, abs=0.15)
    assert mixrender.true_peak_db(out) <= master.CEILING_DBTP + 0.1


def test_the_targets_stay_a_loudness_apart_open_loop():
    x = _noise(20.0, -24.0).astype(np.float32)
    streaming, _ = master.Master(-14.0)(x)
    club, _ = master.Master(-9.0)(x)
    assert mixrender.integrated_lufs(club, SR) > mixrender.integrated_lufs(streaming, SR) + 2


def test_the_target_comes_from_settings(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    assert master.status(settings)["target"] == "streaming"
    assert master.from_settings(settings).target_lufs == -14.0
    assert master.set_target(settings, "club")["target_lufs"] == -9.0
    assert master.from_settings(settings).target_lufs == -9.0
    assert json.loads((tmp_path / "settings.json").read_text())["master"]["target"] == "club"
    settings.set("master", {"target": "louder"})
    assert master.status(settings)["target"] == "streaming"


def test_the_settings_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    app = create_app(library_path=tmp_path / "support" / "library.sqlite3")
    with TestClient(app) as client:
        assert client.get("/api/mixes/master").json()["target_lufs"] == -14.0
        assert client.post("/api/mixes/master", json={"target": "club"}).json()["target_lufs"] == -9.0
        assert client.get("/api/mixes/master").json()["target"] == "club"
        assert client.post("/api/mixes/master", json={"target": "louder"}).status_code == 422


# --- what an export says ----------------------------------------------------------------------------------------------


def _ebur128(path) -> dict:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-filter_complex", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True, check=True)  # fmt: skip
    tail = r.stderr[r.stderr.rindex("Summary:") :]
    return {"integrated_lufs": float(re.search(r"I:\s+(-?[\d.]+) LUFS", tail)[1]),
            "lra_lu": float(re.search(r"LRA:\s+(-?[\d.]+) LU", tail)[1]),
            "true_peak_dbtp": float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", tail)[1])}  # fmt: skip


@needs_ffmpeg
def test_the_measurements_agree_with_ffmpeg(tmp_path):
    x = np.concatenate([_noise(30.0, -24.0), _noise(30.0, -14.0, 1), _noise(30.0, -19.0, 2)], axis=1)
    path = tmp_path / "mix.wav"
    wavfile.write(path, SR, x.T.astype(np.float32))
    ours, theirs = master.measure(x), _ebur128(path)
    assert ours["integrated_lufs"] == pytest.approx(theirs["integrated_lufs"], abs=0.15)
    assert ours["lra_lu"] == pytest.approx(theirs["lra_lu"], abs=0.3)
    assert ours["true_peak_dbtp"] == pytest.approx(theirs["true_peak_dbtp"], abs=0.2)


def test_the_tracklist_says_what_was_measured():
    text = master.tracklist([(0.0, "A - One"), (3725.0, "B - Two")], {"integrated_lufs": -14.04, "lra_lu": 5.26, "true_peak_dbtp": -1.61}, -14.0)
    assert text.splitlines()[:2] == ["00:00  A - One", "1:02:05  B - Two"]
    for line in ("Target: -14 LUFS", "Integrated: -14.0 LUFS", "Loudness range: 5.3 LU", "True peak: -1.6 dBTP"):
        assert line in text


def _kf(value: float, beat: float = 0.0) -> dict:
    return {"beat": beat, "value": value, "curve": "hold"}


def _deck(track_id: str, start_s: float, frm: float, to: float) -> dict:
    lanes = {name: [_kf(v3.LANE_DEFAULTS[name])] for name in v3.LANES} | {"volume": [_kf(1.0)]}
    return {"track_id": track_id, "grid": [{"start_beat": 0.0, "start_s": start_s, "bpm": 120.0}], "play_from_beat": frm,
            "play_to_beat": to, "lanes": lanes}  # fmt: skip


def _handover() -> v3.MixPlan:
    """A plays to beat 16 (8 s in), over the start of B, whose beat 32 is 16 s into the file."""
    return v3.validate({"technique": "long blend", "bars": 4, "window": {"start_beat": -32.0, "end_beat": 64.0},
                        "tempo": [_kf(120.0)], "a": _deck("a", 40.0, -32.0, 16.0), "b": _deck("b", 0.0, 0.0, 64.0)})  # fmt: skip


def _write(tmp_path, a: np.ndarray, b: np.ndarray) -> tuple:
    paths = (tmp_path / "a.wav", tmp_path / "b.wav")
    for path, x in zip(paths, (a, b), strict=True):
        wavfile.write(path, SR, x.T.astype(np.float32))
    return paths


@needs_ffmpeg
def test_a_quiet_intro_comes_in_at_its_own_level_and_the_drop_lands_on_the_reference(tmp_path):
    """B's first 16 s, all the overlap hears, are 14 dB under its drop. Seedbox 1's overlap trim boosts them, and so the drop."""
    paths = _write(tmp_path, _noise(120.0, -20.0), np.concatenate([_noise(16.0, -34.0, 1), _noise(180.0, -20.0, 2)], axis=1))
    plan = _handover()
    audio_a, audio_b = offline.deck_audio(plan, *paths)
    r = offline.render(plan, *paths, audio_a, audio_b)
    old = mixrender.render_audio(offline.to_seedbox1(plan), *paths, SR)
    t_intro, t_drop = (16 + 4) * SR, (32 + 4) * SR  # seconds into the window: B starts at 16 s, its drop at 32 s
    drop = lambda render: mixrender.integrated_lufs(render["stems"]["B"][:, t_drop : t_drop + 8 * SR], SR)
    intro = mixrender.integrated_lufs(r["stems"]["B"][:, t_intro : t_intro + 8 * SR], SR)
    assert drop(r) == pytest.approx(master.DECK_LUFS, abs=0.5)
    assert drop(r) - intro == pytest.approx(14.0, abs=0.5)
    assert drop(old) > drop(r) + 4


@needs_ffmpeg
def test_an_export_writes_audio_and_a_tracklist_measured_on_target(tmp_path):
    paths = _write(tmp_path, _noise(120.0, -14.0), _noise(120.0, -16.0, 1))
    out = tmp_path / "mix.m4a"
    report = offline.export(_handover(), *paths, out, names=("Artist - A", "Artist - B"), target_lufs=-9.0)
    assert out.stat().st_size > 0
    assert report["measured"]["integrated_lufs"] == pytest.approx(-9.0, abs=0.2)
    assert report["measured"]["true_peak_dbtp"] <= master.CEILING_DBTP + 0.1
    text = (tmp_path / "mix.txt").read_text()
    lines = text.splitlines()
    assert lines[0] == "00:00  Artist - A" and lines[1] == "00:16  Artist - B"
    assert f"Integrated: {report['measured']['integrated_lufs']:.1f} LUFS" in text
    assert "Loudness range:" in text and "True peak:" in text
