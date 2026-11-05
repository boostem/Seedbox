"""seedbox2.mix.render: the same plans, the same files, the same sound as Seedbox 1's renderer, with a fraction of it in
memory."""

from __future__ import annotations

import copy
import subprocess
import tracemalloc
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from musicreco import mixplan, mixrender
from musicreco.paths import ffmpeg_path
from seedbox2.mix import convert
from seedbox2.mix import render as renderer

from .fixtures import Spec, analysis, click_track, pad_track, track

pytestmark = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")

SR = 44100
A = Spec("r_a", bpm=124.0, bars=40, intro_bars=8, outro_bars=16)
B_SAME = Spec("r_b", bpm=124.0, bars=40, intro_bars=16, outro_bars=8, first_downbeat_s=0.25)
B_FASTER = Spec("r_c", bpm=126.5, bars=40, intro_bars=16, outro_bars=8)
B_FAST = Spec("r_d", bpm=140.0, bars=40, intro_bars=16, outro_bars=8)
A_OUTRO_8 = Spec("r_e", bpm=124.0, bars=40, intro_bars=8, outro_bars=8)
CASES = {
    "same tempo": (A, B_SAME, None),
    "small gap, stretched": (A, B_FASTER, None),
    "big gap echo out": (A, B_FAST, None),
    "filter sweep": (A, B_SAME, "filter sweep"),
    "a loop": (A_OUTRO_8, B_SAME, None),
    "loop roll": (A, B_SAME, "loop roll"),
}


@pytest.fixture(scope="module")
def folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("render")


@pytest.fixture(autouse=True)
def cache(folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(folder))


def _music(s: Spec, seed: int) -> np.ndarray:
    """Clicks over pads, so that every band has something in it."""
    x = click_track(s, SR, 1000.0 + 700 * seed)
    y = pad_track(s, SR, 220.0 + 40 * seed)
    n = min(x.shape[1], y.shape[1])
    return (0.5 * x[:, :n] + 0.5 * y[:, :n]).astype(np.float32)


def _files(folder: Path, a: Spec, b: Spec) -> tuple[Path, Path]:
    out = []
    for seed, s in enumerate((a, b)):
        p = folder / f"{s.id}.wav"
        if not p.exists():
            wavfile.write(p, SR, _music(s, seed).T)
        out.append(p)
    return out[0], out[1]


def assert_same_sound(new: dict, old: dict) -> None:
    rms = np.sqrt(np.mean(old["mix"] ** 2))
    assert new["mix"].shape == old["mix"].shape
    assert new["mix"].dtype == np.float32
    assert np.sqrt(np.mean((new["mix"] - old["mix"]) ** 2)) < rms * 10 ** (-120 / 20)  # 120 dB down
    assert np.abs(new["mix"] - old["mix"]).max() < 1e-4
    names = "duration_s start_beat beat_times_s lufs true_peak_db leveller_db limiter_db trim_db track_trim_db warnings"
    for k in names.split():
        assert new[k] == old[k], k


@pytest.mark.parametrize("case", CASES)
def test_sounds_like_seedbox1(case: str, folder: Path):
    a, b, tech = CASES[case]
    paths = _files(folder, a, b)
    plan = mixplan.generate(track(a), analysis(a), track(b), analysis(b), "f", technique=tech)
    new = renderer.render_audio(plan, *paths, SR, keep_stems=True)
    old = mixrender.render_audio(plan, *paths, SR)
    assert_same_sound(new, old)
    assert sorted(new["stems"]) == sorted(old["stems"])
    np.testing.assert_allclose(new["gain"], old["gain"], atol=1e-6)


def _plan(a: Spec = A, b: Spec = B_FASTER, tech: str | None = None) -> dict:
    return mixplan.generate(track(a), analysis(a), track(b), analysis(b), "f", technique=tech)


def test_without_key_lock(folder: Path):
    plan = _plan()
    plan["key_lock"] = False
    paths = _files(folder, A, B_FASTER)
    assert_same_sound(renderer.render_audio(plan, *paths), mixrender.render_audio(plan, *paths))


def test_deck_effects_and_a_piecewise_grid(folder: Path):
    plan = copy.deepcopy(_plan(A, B_SAME))
    plan["a"]["fx"] = [{"type": "stutter", "start_beat": -8.0, "end_beat": -4.0, "div": 0.5}]
    plan["b"]["grid"] = [
        {"start_beat": -64.0, "start_s": plan["b"]["beat0_s"] - 64 * 60 / 124.0, "bpm": 124.0},
        {"start_beat": 8.0, "start_s": plan["b"]["beat0_s"] + 8 * 60 / 124.0, "bpm": 123.0},
    ]
    paths = _files(folder, A, B_SAME)
    assert_same_sound(renderer.render_audio(plan, *paths), mixrender.render_audio(plan, *paths))


def test_a_v3_plan_plays_like_the_plan_it_came_from(folder: Path):
    plan = _plan()
    paths = _files(folder, A, B_FASTER)
    v3 = convert.load(plan).model_dump(mode="json")
    assert v3["version"] == 3
    np.testing.assert_array_equal(renderer.render_audio(v3, *paths)["mix"], renderer.render_audio(plan, *paths)["mix"])


def test_same_plan_same_audio(folder: Path):
    plan = _plan(A, B_SAME)
    paths = _files(folder, A, B_SAME)
    again = renderer.render_audio(plan, *paths)
    np.testing.assert_array_equal(renderer.render_audio(plan, *paths)["mix"], again["mix"])


def test_a_compressed_file_renders_like_its_wav(folder: Path):
    """MP3 seeks, M4A doesn't (it decodes from the start and throws away what's before the window): both give what a
    whole-file render of the same file gives."""
    plan = _plan(A, B_SAME)
    a, b = _files(folder, A, B_SAME)
    for ext, args in ((".mp3", ["-c:a", "libmp3lame", "-q:a", "2"]), (".m4a", ["-c:a", "aac"])):
        coded = []
        for p in (a, b):
            q = p.with_suffix(ext)
            if not q.exists():
                r = subprocess.run([ffmpeg_path(), "-nostdin", "-loglevel", "error", "-y", "-i", str(p), *args, str(q)],
                                   capture_output=True)  # fmt: skip
                if r.returncode:
                    pytest.skip(f"this ffmpeg can't write {ext}")
            coded.append(q)
        assert_same_sound(renderer.render_audio(plan, *coded), mixrender.render_audio(plan, *coded))


def test_a_deck_past_its_file_warns(folder: Path):
    plan = copy.deepcopy(_plan(A, B_SAME))
    plan["a"]["beat0_s"] = 1000.0
    paths = _files(folder, A, B_SAME)
    new, old = renderer.render_audio(plan, *paths), mixrender.render_audio(plan, *paths)
    assert any("plays outside its audio file" in w for w in new["warnings"])
    assert new["warnings"] == old["warnings"]


def test_a_file_that_is_gone_or_broken_says_so(folder: Path):
    plan = _plan(A, B_SAME)
    _, b = _files(folder, A, B_SAME)
    with pytest.raises(ValueError, match="could not decode"):
        renderer.render_audio(plan, folder / "gone.wav", b)
    bad = folder / "bad.mp3"
    bad.write_bytes(b"not audio" * 1000)
    with pytest.raises(ValueError, match="could not decode bad.mp3"):
        renderer.render_audio(plan, bad, b)


def test_too_short_a_window_is_refused(folder: Path):
    plan = copy.deepcopy(_plan(A, B_SAME))
    plan["window"]["end_beat"] = plan["window"]["start_beat"] + 0.1
    with pytest.raises(ValueError):
        renderer.render_audio(plan, *_files(folder, A, B_SAME))


def test_track_loudness_is_measured_once(folder: Path, monkeypatch: pytest.MonkeyPatch):
    a, _ = _files(folder, A, B_SAME)
    p = folder / "once.wav"
    p.write_bytes(a.read_bytes())
    calls = []
    real = renderer.stream.measure_track
    monkeypatch.setattr(renderer.stream, "measure_track", lambda *args: calls.append(1) or real(*args))
    first = renderer.track_lufs(p)
    assert renderer.track_lufs(p) == first
    assert len(calls) == 1
    p.write_bytes(a.read_bytes() + b"\0\0\0\0")  # another file now
    renderer.track_lufs(p)
    assert len(calls) == 2


def test_render_writes_an_m4a(folder: Path):
    out = folder / "out.m4a"
    progress: list[float] = []
    r = renderer.render(_plan(A, B_SAME), *_files(folder, A, B_SAME), out, progress=progress.append)
    assert out.stat().st_size > 10000
    assert progress == sorted(progress) and progress[-1] == 1.0
    assert "mix" not in r and r["duration_s"] > 10


def test_holds_the_window_not_the_tracks(folder: Path):
    """Two 150 s tracks, a window of 16 bars at their own tempo. Seedbox 1 holds 106 MB of decoded tracks before it
    starts; here the whole Python heap stays within a few copies of the 31 s window (11 MB each)."""
    long_a = Spec("l_a", bpm=124.0, bars=78, intro_bars=8, outro_bars=16)
    long_b = Spec("l_b", bpm=124.0, bars=78, intro_bars=16, outro_bars=8)
    paths = _files(folder, long_a, long_b)
    plan = copy.deepcopy(_plan(long_a, long_b))
    w0 = plan["window"]["start_beat"]
    plan["window"]["end_beat"] = w0 + 64
    renderer.track_lufs(paths[0]), renderer.track_lufs(paths[1])
    tracemalloc.start()
    try:
        r = renderer.render_audio(plan, *paths)
        peak = tracemalloc.get_traced_memory()[1] / 2**20
    finally:
        tracemalloc.stop()
    assert r["mix"].shape[1] == pytest.approx(64 * 60 / 124 * SR, abs=2)
    assert peak < 70
