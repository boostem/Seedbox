"""seedbox2.eval.mixmetrics on hand-built renders with known faults (offset beats, two basslines, two vocals, a
crossfade dip, clipping, a stretch), and `python -m seedbox2.eval mixes` end to end."""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

from musicreco.paths import ffmpeg_path
from seedbox2.eval import mixes
from seedbox2.eval.mixmetrics import Render, measure

ROOT = Path(__file__).resolve().parent.parent
SR = 22050
N = 20 * SR  # 20 s renders


def _track(key="8A", bass=True, vocals=False, seed=0, bpm=124.0):
    bars = 12
    x = mixes.synth_track(bpm, bars, key, 0 if bass else bars, 0, [(0, bars)] if vocals else [], SR, seed)
    return x[:, :N].astype(np.float64)


def _render(a, b, ga=None, gb=None, **kw):
    ga = np.ones(N) if ga is None else ga
    gb = np.ones(N) if gb is None else gb
    da, db = a * ga, b * gb
    return Render(mix=da + db, decks={"A": da, "B": db}, sr=SR, **kw)


def _delay(x, ms):
    k = round(ms / 1000 * SR)
    return np.concatenate([np.zeros((2, k)), x[:, : N - k]], axis=1)


def _ramps(t0=5.0, t1=15.0):
    """A on 0..t1 and fading out over [t0, t1], B fading in over it and on after; linear (0..1) ramps."""
    t = np.arange(N) / SR
    up = np.clip((t - t0) / (t1 - t0), 0, 1)
    return 1 - up, up


def test_beat_alignment_measures_offset():
    a, b = _track(seed=1), _track("9A", seed=2)
    aligned = measure(_render(a, b))["beat"]
    late = measure(_render(a, _delay(b, 20)))["beat"]
    assert aligned["median_ms"] < 1.0
    assert abs(late["median_ms"] - 20) < 1.5
    assert late["mean_signed_ms"] > 0  # B late
    assert late["windows"] >= 10


def test_bass_overlap_two_basslines_vs_hand_off():
    both = measure(_render(_track(seed=1), _track(seed=2)))["bass"]
    a = _track(seed=1)
    a_cut = signal.sosfiltfilt(signal.butter(4, 300, "high", fs=SR, output="sos"), a)  # A's bass EQ killed
    one = measure(_render(a_cut, _track(seed=2)))["bass"]
    assert both["seconds"] > 15 and both["db_s"] > 100
    assert one["seconds"] < 0.5


def test_vocal_overlap():
    assert measure(_render(_track(vocals=True, seed=1), _track(vocals=True, seed=2)))["vocals"]["seconds"] > 10
    assert measure(_render(_track(vocals=True, seed=1), _track(seed=2)))["vocals"]["seconds"] < 0.5
    # vocal stems, when there are any, win over the proxy
    a, b = _track(seed=1), _track(seed=2)
    stems = {"A": {"vocals": a}, "B": {"vocals": np.zeros_like(b)}}
    v = measure(_render(a, b, stems=stems))["vocals"]
    assert v["source"] == "stems" and v["seconds"] == 0


def test_linear_crossfade_dips_equal_power_does_not():
    a, b = _track(seed=1), _track("3B", seed=2, bpm=97.0)  # off-grid: the kicks don't add up in phase
    ga, gb = _ramps()
    lin = measure(_render(a, b, ga, gb))["loudness"]
    eq = measure(_render(a, b, np.sqrt(ga), np.sqrt(gb)))["loudness"]
    assert lin["dip_lu"] < -2.0
    assert eq["dip_lu"] > -1.0 and eq["bump_lu"] < 1.0
    checks = {c["id"]: c["status"] for c in measure(_render(a, b, ga, gb))["checks"]}
    assert checks["loudness"] in ("warn", "fail")


def test_clipping_and_true_peak():
    a, b = _track(seed=1), _track(seed=2)
    r = _render(a * 3, b * 3)
    lo = measure(r)["loudness"]
    assert lo["clipped_samples"] > 0 and lo["true_peak_dbtp"] > 0
    assert {c["id"]: c["status"] for c in measure(r)["checks"]}["loudness"] == "fail"


def test_masking_index():
    a = _track(seed=1)
    same = measure(_render(a, a))["masking"]
    one = measure(_render(a, _track(bass=False, seed=2)))["masking"]
    assert same["bass"] > 0.95
    assert one["bass"] < same["bass"] - 0.2


def test_no_overlap_is_a_cut():
    a, b = _track(seed=1), _track(seed=2)
    t = np.arange(N) / SR
    m = measure(_render(a, b, (t < 10).astype(float), (t >= 10).astype(float)))
    assert m["overlap"]["seconds"] == 0
    assert m["beat"]["median_ms"] is None and m["masking"]["bass"] is None


def test_key_and_stretch_from_plan():
    deck = {"play_from_beat": 0.0, "play_to_beat": 64.0}
    plan = {"window": {"start_beat": 0.0, "end_beat": 64.0}, "key_lock": True,
            "tempo": [{"beat": 0.0, "value": 124.0, "curve": "hold"}],
            "a": deck | {"native_bpm": 124.0}, "b": deck | {"native_bpm": 120.0}}
    a, b = _track(seed=1), _track(seed=2)
    k = measure(_render(a, b, plan=plan, keys={"A": "8A", "B": "10A"}))["key"]
    assert k["b_stretch"] == pytest.approx(124 / 120, abs=1e-4)
    assert k["max_stretch_pct"] == pytest.approx(3.33, abs=0.01)
    assert (k["b_semitones"], k["key_steps"], k["key_move"]) == (0.0, 2, "+2 boost")
    # no key lock: B is pitched up with the tempo, ~0.57 semitones, rounds to +1 = 7 steps round the wheel
    k = measure(_render(a, b, plan=plan | {"key_lock": False}, keys={"A": "8A", "B": "10A"}))["key"]
    assert k["b_semitones"] == pytest.approx(0.57, abs=0.01)
    assert k["key_b_heard"] == "5A" and k["key_steps"] == 3


def test_checks_shape():
    m = measure(_render(_track(seed=1), _delay(_track(seed=2), 30)))
    ids = [c["id"] for c in m["checks"]]
    assert ids == ["beat", "bass", "vocals", "loudness", "key", "masking"]
    assert all(c["status"] in ("pass", "warn", "fail", "na") for c in m["checks"])
    assert m["checks"][0]["status"] == "fail"
    json.dumps(m)  # JSON-ready


def test_pairs_toml(tmp_path):
    (tmp_path / "pairs.toml").write_text('[[pair]]\na = "x/a.mp3"\nb = "b.mp3"\nkey_a = "8A"\nbars = 16\n')
    (p,) = mixes.load_pairs(tmp_path / "pairs.toml")
    assert p.a == tmp_path / "x/a.mp3" and p.key_a == "8A" and p.bars == 16 and p.name == "a → b"


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")
def test_eval_mixes_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(mixes, "SYNTHETIC", mixes.SYNTHETIC[:1])
    out = tmp_path / "out.json"
    assert mixes.main(["--work", str(tmp_path / "w"), "--json", str(out)]) == 0
    (row,) = json.loads(out.read_text())
    s1 = row["engines"]["seedbox1"]["metrics"]
    assert s1["overlap"]["seconds"] > 4 and s1["beat"]["median_ms"] < 5
    assert s1["loudness"]["clipped_samples"] == 0
    s2 = row["engines"]["seedbox2"]["metrics"]
    assert s2["overlap"]["seconds"] > 4 and s2["loudness"]["clipped_samples"] == 0


def test_eval_usage():
    out = subprocess.run([sys.executable, "-m", "seedbox2.eval"], cwd=ROOT, capture_output=True, text=True,
                         check=False)
    assert out.returncode == 2 and "mixes" in out.stderr
