"""Seedbox 2's energy, loudness and vibe (#48): the meters on known signals, the features' direction, the CLAP front
ends, the windows, the search and filters, the jobs and the API."""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from musicreco.paths import ffmpeg_path
from seedbox2.analysis import clap, energy, loudness, vibe
from seedbox2.library import Library
from seedbox2.library.view import to_camelot

needs_ffmpeg = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")
SR = 48_000


def tone(seconds: float, freq: float = 997.0, dbfs: float = -20.0, sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    x = 10 ** (dbfs / 20) * np.sin(2 * np.pi * freq * t)
    return np.stack([x, x], axis=1).astype(np.float32)


def meter(x: np.ndarray, chunk: int = 7_001) -> dict:
    m = loudness.Meter()
    for i in range(0, x.shape[0], chunk):
        m.feed(x[i : i + chunk])
    return m.result()


# --- BS.1770 --------------------------------------------------------------------------------------------------------


def test_a_stereo_997_hz_tone_at_minus_23_dbfs_reads_minus_23_lufs():
    r = meter(tone(10, dbfs=-23.0))
    assert r["integrated_lufs"] == pytest.approx(-23.0, abs=0.1)  # EBU Tech 3341 case 1
    assert np.array(r["lufs"][5:]) == pytest.approx(-23.0, abs=0.1)


def test_chunk_size_does_not_change_the_result():
    x = tone(5) + tone(5, 220.0, -14.0)
    a, b = meter(x, 4_800), meter(x, 13_337)
    assert a["integrated_lufs"] == b["integrated_lufs"] and a["lufs"] == b["lufs"]


def test_the_relative_gate_ignores_a_quiet_stretch():
    loud, quiet = tone(20, dbfs=-20.0), tone(20, dbfs=-50.0)
    assert meter(np.concatenate([loud, quiet]))["integrated_lufs"] == pytest.approx(meter(loud)["integrated_lufs"], abs=0.1)


def test_silence_has_no_loudness():
    r = meter(np.zeros((SR * 2, 2), np.float32))
    assert r["integrated_lufs"] is None and set(r["lufs"]) == {loudness.SILENCE}


def test_the_true_peak_sees_between_the_samples():
    # a tone at a quarter of the rate with samples at ±45°: its samples peak at 0.707 of the real peak
    t = np.arange(SR * 2)
    x = (0.9 * np.sin(2 * np.pi * t / 4 + np.pi / 4)).astype(np.float32)
    r = meter(np.stack([x, x], axis=1))
    assert r["true_peak_dbtp"] == pytest.approx(20 * np.log10(0.9), abs=0.3)
    assert 20 * np.log10(np.abs(x).max()) < r["true_peak_dbtp"] - 2


def test_a_stretch_measures_only_itself_and_section_peaks_stay_apart():
    x = np.concatenate([tone(10, dbfs=-30.0), tone(10, dbfs=-12.0)])
    r = meter(x)
    a, b = loudness.sections(r, [{"kind": "intro", "start_s": 0, "end_s": 10}, {"kind": "drop", "start_s": 10, "end_s": 20}])
    assert a["kind"] == "intro" and a["integrated_lufs"] == pytest.approx(-30.0, abs=0.3)
    assert b["integrated_lufs"] == pytest.approx(-12.0, abs=0.3)
    assert b["true_peak_dbtp"] == pytest.approx(-12.0, abs=0.3)
    assert a["true_peak_dbtp"] < -25 < -15 < b["true_peak_dbtp"]


def test_overlap_gain_matches_the_tracks_and_leaves_headroom():
    quiet, loud = meter(tone(10, dbfs=-20.0)), meter(tone(10, dbfs=-14.0))
    assert loudness.overlap_gain_db(loud, (2, 8), quiet, (2, 8)) == pytest.approx(6.0, abs=0.2)
    assert loudness.overlap_gain_db(quiet, (2, 8), loud, (2, 8)) == pytest.approx(-6.0, abs=0.2)
    hot = meter(tone(10, dbfs=-3.0))
    louder = meter(tone(10, dbfs=-1.0))
    assert loudness.overlap_gain_db(louder, (2, 8), hot, (2, 8)) == pytest.approx(2.0, abs=0.2)
    limited = {**hot, "peak_dbtp": [-0.5] * 10}
    assert loudness.overlap_gain_db(louder, (2, 8), limited, (2, 8)) == pytest.approx(0.5, abs=0.01)
    silent = meter(np.zeros((SR * 10, 2), np.float32))
    assert loudness.overlap_gain_db(loud, (2, 8), silent, (2, 8)) is None


# --- energy ---------------------------------------------------------------------------------------------------------


def _kick(seconds: float, bpm: float = 125.0, level: float = 0.8) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    beat = 60 / bpm
    phase = t % beat
    x = level * np.exp(-phase * 25) * np.sin(2 * np.pi * 60 * phase)
    hat = 0.3 * np.exp(-((t + beat / 2) % beat) * 80) * np.random.default_rng(1).standard_normal(t.size)
    return np.stack([x + hat, x + hat], axis=1).astype(np.float32)


def _pad(seconds: float, level: float = 0.02) -> np.ndarray:
    return tone(seconds, 330.0, 20 * np.log10(level))


def _measure(x: np.ndarray, beats: dict | None = None) -> dict:
    meter_ = loudness.Meter()
    spectral = energy._Spectral()
    for i in range(0, x.shape[0], SR):
        part = x[i : i + SR]
        meter_.feed(part)
        spectral.feed(part.mean(axis=1))
    return energy.analyse(spectral.result(), meter_.result(), beats)


def test_a_busy_loud_track_rates_higher_than_a_quiet_pad():
    busy, calm = _measure(_kick(60)), _measure(_pad(60))
    assert busy["rating"] > calm["rating"]
    assert 1 <= calm["rating"] <= 10 and 1 <= busy["rating"] <= 10
    assert busy["energy"] == pytest.approx((energy.to_rating(busy["score"]) - 1) / 9, abs=1e-3)
    assert busy["track_features"]["onsets"] > calm["track_features"]["onsets"]
    assert busy["track_features"]["drums"] > calm["track_features"]["drums"]
    assert busy["track_features"]["bass"] > calm["track_features"]["bass"]


def test_the_curve_follows_the_arrangement_on_the_beat_grid():
    bpm = 120.0
    x = np.concatenate([_pad(16), _kick(32, bpm), _pad(16)])
    beats = {"segments": [{"start_beat": 0, "start_s": 0.0, "bpm": bpm}]}
    d = _measure(x, beats)
    assert d["grid"] == "beats" and d["source"] == "mix"
    assert d["bar_s"][:3] == pytest.approx([0.0, 2.0, 4.0]) and len(d["curve"]) == len(d["bar_s"]) - 1 == 32
    curve = np.array(d["curve"])
    assert curve[10:22].mean() > curve[:6].mean() + 0.2 and curve[10:22].mean() > curve[-6:].mean() + 0.2
    assert ((0 <= curve) & (curve <= 1)).all()
    assert set(d["features"]) == set(energy.FEATURES)


def test_silent_bars_have_no_energy_and_no_grid_means_fixed_bars():
    d = _measure(np.concatenate([np.zeros((SR * 10, 2), np.float32), _kick(30)]))
    assert d["grid"] == "fixed"
    assert d["curve"][0] == 0.0 and max(d["curve"]) > 0.2


def test_a_broken_grid_falls_back_to_fixed_bars():
    bars, source = energy.bar_times({"segments": [{"nonsense": 1}]}, 20.0)
    assert source == "fixed" and bars[0] == 0.0 and bars[-1] == 20.0


def test_music_before_the_first_bar_is_still_measured():
    bars, source = energy.bar_times({"segments": [{"start_beat": 0, "start_s": 1.3, "bpm": 120.0}]}, 20.0)
    assert source == "beats" and bars[0] == 0.0 and bars[-1] == 20.0 and np.all(np.diff(bars) > 0)


@needs_ffmpeg
def test_stems_shape_the_bars_without_moving_the_rating(tmp_path):
    x = _kick(40)
    half = x.shape[0] // 2
    files = {}
    for name, gain in (("drums", np.r_[np.full(half, 0.01), np.ones(x.shape[0] - half)]), ("bass", np.ones(x.shape[0]))):
        path = tmp_path / f"{name}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
            w.writeframes((np.clip(x[:, 0] * gain, -1, 1) * 32767).astype(np.int16).tobytes())
        files[name] = path
    meter_, spectral = loudness.Meter(), energy._Spectral()
    meter_.feed(x)
    spectral.feed(x.mean(axis=1))
    frames, curve = spectral.result(), meter_.result()
    mix = energy.analyse(frames, curve, None)
    d = energy.analyse(frames, curve, None, files)
    assert d["source"] == "stems" and mix["source"] == "mix"
    drums = np.array(d["features"]["drums"])
    n = drums.size
    assert drums[n // 2 + 1 :].mean() > drums[: n // 2 - 1].mean() + 0.5, "the bars follow the stem"
    assert np.mean(mix["features"]["drums"][n // 2 + 1 :]) == pytest.approx(np.mean(mix["features"]["drums"][: n // 2 - 1]), abs=0.1)
    assert abs(d["score"] - mix["score"]) < 0.05, "and the track's level follows the mix"


def _eval():
    from seedbox2.eval import energy as evaluate

    return evaluate


def test_the_fit_recovers_known_weights_and_stays_non_negative():
    ev = _eval()
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 1, (60, 5))
    w = np.array([0.5, 0.2, 0.0, 0.3, 0.0])
    y = 9 * (x @ w) + 1 + rng.normal(0, 0.05, 60)
    c = ev.fit(x, y)
    got = np.array([c["weights"][k] for k in energy.FEATURES])
    assert got == pytest.approx(w, abs=0.05) and (got >= 0).all() and got.sum() == pytest.approx(1, abs=1e-3)
    assert c["ratings"] == 60
    assert ev.leave_one_out(x, y)["spearman"] > 0.95


def test_the_scale_puts_the_5th_and_95th_percentiles_on_3_and_9():
    ev = _eval()
    x = np.random.default_rng(1).uniform(0, 1, (200, 5))
    w = energy.CALIBRATION["weights"]
    a, b = ev.scale_from(x, w)
    s = x @ np.array([w[k] for k in energy.FEATURES])
    assert a * np.percentile(s, 5) + b == pytest.approx(3.0, abs=0.01)
    assert a * np.percentile(s, 95) + b == pytest.approx(9.0, abs=0.01)


# --- CLAP's front ends ----------------------------------------------------------------------------------------------


def test_log_mel_shape_and_a_tone_lands_in_its_band():
    t = np.arange(clap.WINDOW_SAMPLES) / clap.SAMPLE_RATE
    mel = clap.log_mel((0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32))
    assert mel.shape == (1001, 64) and mel.dtype == np.float32
    peak = mel[500].argmax()
    bank = clap.mel_filters()
    assert bank[int(1000 / (clap.SAMPLE_RATE / clap.N_FFT)), peak] > 0


def test_a_short_window_is_repeated_and_padded():
    x = np.ones(clap.WINDOW_SAMPLES // 3 + 7, np.float32)
    out = clap.fit_window(x)
    assert out.size == clap.WINDOW_SAMPLES and out[0] == 1 and out[x.size * 2 - 1] == 1 and out[-1] == 0
    assert clap.fit_window(np.zeros(0)).size == clap.WINDOW_SAMPLES
    assert clap.fit_window(np.ones(clap.WINDOW_SAMPLES * 2)).size == clap.WINDOW_SAMPLES


def _tokenizer_file(tmp_path: Path) -> Path:
    vocab = {"<s>": 0, "<pad>": 1, "</s>": 2, "<unk>": 3, "w": 4, "a": 5, "r": 6, "m": 7, "wa": 8, "rm": 9, "warm": 10,
             "Ġ": 11, "d": 12, "k": 13, "Ġd": 14, "Ġdark": 15, "ar": 16, "ark": 17}  # fmt: skip
    merges = ["w a", "r m", "wa rm", "Ġ d", "a r", "ar k", "Ġd ark"]
    path = tmp_path / "tokenizer.json"
    path.write_text(json.dumps({"model": {"vocab": vocab, "merges": merges}}))
    return path


def test_the_tokenizer_merges_by_rank_and_wraps(tmp_path):
    tk = clap.Tokenizer.load(_tokenizer_file(tmp_path))
    assert tk.encode("warm") == [0, 10, 2]
    assert tk.encode("warm dark") == [0, 10, 15, 2]
    assert tk.encode("q") == [0, 3, 2]
    assert len(tk.encode("warm " * 200)) == clap.MAX_TOKENS


# --- windows --------------------------------------------------------------------------------------------------------


def _energy_data(curve: list[float], bar: float = 2.0) -> dict:
    return {"bar_s": [i * bar for i in range(len(curve) + 1)], "curve": curve}


def _shape() -> list[float]:
    return [0.0] * 2 + [0.2] * 30 + [0.9] * 30 + [0.3] * 15 + [0.7] * 30 + [0.2] * 30 + [0.0] * 3


def test_windows_cover_the_intro_the_drop_and_the_outro():
    w = {x["name"]: x["start_s"] for x in vibe.choose_windows(_energy_data(_shape()))}
    assert set(w) == {"intro", "main", "outro", "breakdown", "second"}
    assert w["intro"] == 4.0  # after the silence
    assert 64 <= w["main"] <= 114
    assert w["outro"] + 10 <= 280 and w["outro"] > 200
    assert 124 <= w["breakdown"] <= 154 + 20
    assert abs(w["second"] - w["main"]) >= vibe.MIN_APART_S


def test_sections_choose_the_windows_when_there_are_any():
    sections = [{"kind": "intro", "start_s": 0, "end_s": 60}, {"kind": "drop", "start_s": 160, "end_s": 200},
                {"kind": "outro", "start_s": 230, "end_s": 280}]  # fmt: skip
    w = {x["name"]: x["start_s"] for x in vibe.choose_windows(_energy_data(_shape()), sections)}
    assert 160 <= w["main"] <= 190 and w["outro"] == 230.0


def test_a_short_track_has_fewer_windows():
    assert vibe.choose_windows(_energy_data([0.5, 0.5])) == [{"name": "main", "start_s": 0.0}]
    names = [x["name"] for x in vibe.choose_windows(_energy_data([0.5] * 10))]
    assert "main" in names and len(names) <= 3 and "breakdown" not in names


def test_regions_split_the_curve_around_its_busy_part():
    r = vibe.regions(_energy_data([0.1] * 8 + [0.8] * 40 + [0.1] * 8))
    assert [x["kind"] for x in r] == ["intro", "main", "outro"]
    assert r[0]["end_s"] == r[1]["start_s"] == 16.0 and r[1]["end_s"] == 96.0 and r[2]["end_s"] == 112.0


# --- search ---------------------------------------------------------------------------------------------------------


def _unit(*values: float) -> np.ndarray:
    v = np.zeros(clap.DIM, np.float32)
    v[: len(values)] = values
    return clap.normalise(v)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    library = Library.open(tmp_path / "library.sqlite3")
    yield library
    library.close()


def _add(lib: Library, tmp_path: Path, name: str, vectors: dict[str, np.ndarray], bpm: float | None, key: str | None) -> str:
    hash_ = name * 40
    lib.upsert_track(hash_, 300.0, title=name, artist="A")
    if bpm is not None:
        lib.put_analysis(hash_, "beats", "1", {"bpm": bpm})
    if key is not None:
        lib.put_analysis(hash_, "key", "1", {"camelot": key})
    windows = [{"name": n, "start_s": float(i)} for i, n in enumerate(vectors)]
    path = vibe.artifact_path(hash_)
    vibe.write_embeddings(path, windows, np.stack(list(vectors.values())))
    lib.put_artifact(hash_, vibe.ARTIFACT_KIND, vibe.model_version(), path)
    return hash_


def test_nearest_tracks_compare_like_windows_and_filter_by_bpm_and_key(lib, tmp_path):
    seed = _add(lib, tmp_path, "a", {"intro": _unit(1, 0), "main": _unit(0, 1)}, 124.0, "8A")
    near = _add(lib, tmp_path, "b", {"intro": _unit(1, 0.1), "main": _unit(0, 1)}, 125.0, "9A")
    swapped = _add(lib, tmp_path, "c", {"intro": _unit(0, 1), "main": _unit(1, 0)}, 124.0, "8A")
    fast = _add(lib, tmp_path, "d", {"intro": _unit(1, 0), "main": _unit(0, 1)}, 140.0, "8A")
    clash = _add(lib, tmp_path, "e", {"intro": _unit(1, 0), "main": _unit(0, 1)}, 124.0, "3B")
    half = _add(lib, tmp_path, "f", {"intro": _unit(1, 0.2), "main": _unit(0, 1)}, 62.0, "8B")
    unknown = _add(lib, tmp_path, "9", {"intro": _unit(1, 0), "main": _unit(0, 1)}, None, None)
    index = vibe.Index()

    out = vibe.similar(lib, index, seed)
    assert [r["hash"] for r in out["results"]] == [near, half, swapped]
    assert out["results"][0]["similarity"] > out["results"][-1]["similarity"]
    assert out["filtered"] == 3 and out["seed"]["key"] == "8A" and out["seed"]["bpm"] == 124.0

    everything = vibe.similar(lib, index, seed, use_bpm=False, use_key=False)
    assert {r["hash"] for r in everything["results"]} == {near, swapped, fast, clash, half, unknown}
    assert everything["results"][-1]["hash"] == swapped

    only_main = vibe.similar(lib, index, seed, window="main", use_bpm=False, use_key=False, limit=2)
    assert len(only_main["results"]) == 2 and swapped not in {r["hash"] for r in only_main["results"]}
    with pytest.raises(vibe.VibeError):
        vibe.similar(lib, index, seed, window="outro")


def test_a_track_without_embeddings_has_no_neighbours(lib, tmp_path):
    lib.upsert_track("z" * 40, 100.0)
    assert vibe.similar(lib, vibe.Index(), "z" * 40) is None


def test_the_index_notices_new_and_replaced_tracks(lib, tmp_path):
    a = _add(lib, tmp_path, "a", {"main": _unit(1, 0)}, 120.0, "8A")
    index = vibe.Index()
    index.refresh(lib)
    vectors = index.vectors
    index.refresh(lib)
    assert index.vectors is vectors
    b = _add(lib, tmp_path, "b", {"main": _unit(0, 1), "intro": _unit(1, 1)}, 120.0, "8A")
    index.refresh(lib)
    assert set(index.hashes) == {a, b} and index.vectors.shape == (3, clap.DIM)
    assert index.best_per_track(_unit(0, 1))[index.hashes.index(b)] == pytest.approx(1.0)


class FakeJobs:
    """Runs `clap_text`'s effect at once: writes the vector the fake encoder gives."""

    def __init__(self, vector: np.ndarray | None) -> None:
        self.vector = vector
        self.submitted: list[dict] = []

    def submit(self, kind: str, payload: dict, **_: Any) -> dict:
        self.submitted.append({"kind": kind, **payload})
        return {"id": "j1", "state": "queued"}

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        text = self.submitted[-1]["text"]
        if self.vector is None:
            return {"state": "done", "result": {"skipped": "model_missing"}}
        path = vibe.text_path(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, self.vector)
        return {"state": "done", "result": {"path": str(path)}}


def test_text_search_ranks_by_the_best_window_and_filters(lib, tmp_path):
    match = _add(lib, tmp_path, "a", {"intro": _unit(0, 1), "main": _unit(1, 0)}, 124.0, "8A")
    other = _add(lib, tmp_path, "b", {"main": _unit(0, 1)}, 124.0, "8A")
    other_tempo = _add(lib, tmp_path, "c", {"main": _unit(1, 0)}, 150.0, "8A")
    jobs = FakeJobs(_unit(1, 0))
    index = vibe.Index()
    out = vibe.search(jobs, lib, index, "  warm   sunset ")
    assert out["text"] == "warm sunset" and [r["hash"] for r in out["results"]][-1] == other
    assert {r["hash"] for r in out["results"][:2]} == {match, other_tempo}
    assert jobs.submitted == [{"kind": "clap_text", "text": "warm sunset"}]
    assert vibe.search(jobs, lib, index, "warm sunset")["results"]
    assert len(jobs.submitted) == 1, "the second search reads the cached vector"
    filtered = vibe.search(jobs, lib, index, "warm sunset", bpm=124.0, camelot="9A", limit=1)
    assert [r["hash"] for r in filtered["results"]] == [match]
    with pytest.raises(vibe.VibeError):
        vibe.search(jobs, lib, index, "   ")


def test_text_search_says_when_the_models_are_missing(lib):
    with pytest.raises(vibe.ModelNotReady):
        vibe.text_vector(FakeJobs(None), "dark")


def test_tempo_compatibility_includes_half_and_double_time():
    assert vibe.tempo_compatible(124, 128) and vibe.tempo_compatible(174, 87) and vibe.tempo_compatible(87, 174)
    assert not vibe.tempo_compatible(124, 140)
    assert vibe.tempo_compatible(124, 140, 15)


def test_prompt_and_the_text_cache_key_ignore_spacing():
    assert vibe.prompt(" vocal   house ") == "this is vocal house music"
    assert vibe.text_path("vocal house", Path("/x")) == vibe.text_path(" vocal  house", Path("/x"))
    assert vibe.text_path("dark", Path("/x")) != vibe.text_path("warm", Path("/x"))


def test_camelot_filter_comes_from_any_key_spelling():
    assert to_camelot("A minor") == "8A"


# --- jobs and API ---------------------------------------------------------------------------------------------------


def _write_audio(path: Path, x: np.ndarray) -> None:
    subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0", str(path)],
                   input=x.tobytes(), check=True)  # fmt: skip


class _Ctx:
    def __init__(self, library: Library, session: Any = None) -> None:
        self.library = library
        self.session = session
        self.settings = None
        self.fractions: list[float] = []

    def progress(self, fraction: float, message: str | None = None) -> None:
        self.fractions.append(fraction)

    def check(self) -> None:
        pass

    def session_or_none(self, name: str) -> Any:
        return self.session


class FakeAudioSession:
    def get_inputs(self) -> list[Any]:
        return [type("I", (), {"name": "input_features"})()]

    def get_outputs(self) -> list[Any]:
        return [type("O", (), {"name": "audio_embeds"})()]

    def run(self, _names: Any, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        assert feed["input_features"].shape == (1, 1, clap.FRAMES, clap.N_MELS)
        v = np.zeros((1, clap.DIM), np.float32)
        v[0, 0], v[0, 1] = 3.0, float(feed["input_features"].mean())
        return [v]


@pytest.fixture
def track(lib, tmp_path):
    path = tmp_path / "song.flac"
    _write_audio(path, np.concatenate([_pad(45), _kick(120), _pad(45)]))
    return lib.add_file(path, title="Song")["hash"]


@needs_ffmpeg
def test_the_energy_job_stores_energy_and_loudness_then_clap_embeds(lib, track):
    ctx = _Ctx(lib, FakeAudioSession())
    out = energy.energy_job({"hash": track}, ctx)
    assert out["source"] == "mix" and out["grid"] == "fixed" and out["integrated_lufs"] < 0
    e = lib.analysis(track, energy.ANALYSER, energy.VERSION)["data"]
    assert e["energy"] == pytest.approx((e["rating"] - 1) / 9, abs=0.06)
    assert len(lib.analysis(track, energy.LOUDNESS_ANALYSER, energy.VERSION)["data"]["lufs"]) == pytest.approx(2100, abs=5)
    assert lib.view_rows(hashes=[track])[0]["energy"] == e["energy"]
    assert ctx.fractions[-1] == 1.0

    done = vibe.clap_job({"hash": track}, ctx)
    assert set(done["windows"]) >= {"intro", "main", "outro"}
    data = vibe.read_embeddings(vibe.current(lib, track))
    assert data["vectors"].shape == (len(done["windows"]), clap.DIM)
    assert np.linalg.norm(data["vectors"], axis=1) == pytest.approx(1.0)
    main = data["starts"][data["names"].index("main")]
    assert 40 <= main <= 160
    assert vibe.clap_job({"hash": track}, ctx)["cached"] is True


@needs_ffmpeg
def test_clap_waits_for_its_model(lib, track):
    energy.energy_job({"hash": track}, _Ctx(lib))
    assert vibe.clap_job({"hash": track}, _Ctx(lib))["skipped"] == "model_missing"
    assert vibe.current(lib, track) is None


class ListenerJobs:
    def __init__(self) -> None:
        self.kinds: list[tuple[str, str]] = []

    def submit(self, kind: str, payload: dict, **_: Any) -> dict:
        self.kinds.append((kind, payload["hash"]))
        return {"id": "x", "state": "queued"}


def test_energy_follows_beats_and_comes_back_for_grids_and_stems(lib, monkeypatch):
    h = "d" * 40
    lib.upsert_track(h, 10.0)
    jobs = ListenerJobs()
    row = {"kind": "beats", "state": "done", "payload": {"hash": h}, "result": {}}
    energy.on_job_finished(jobs, lib, row)
    assert jobs.kinds == [("energy", h)]
    energy.on_job_finished(jobs, lib, {**row, "state": "failed"})
    assert len(jobs.kinds) == 2, "a track without beats still gets energy, on fixed bars"
    jobs.kinds.clear()

    lib.put_analysis(h, energy.ANALYSER, energy.VERSION, {"grid": "fixed", "source": "mix"})
    energy.on_job_finished(jobs, lib, row)
    assert jobs.kinds == [("energy", h)]
    jobs.kinds.clear()

    lib.put_analysis(h, energy.ANALYSER, energy.VERSION, {"grid": "beats", "source": "mix"})
    energy.on_job_finished(jobs, lib, row)
    assert jobs.kinds == []
    monkeypatch.setattr(energy.stems, "current", lambda _l, _h: Path("/stems"))
    energy.on_job_finished(jobs, lib, {**row, "kind": "stems"})
    assert jobs.kinds == [("energy", h)]
    jobs.kinds.clear()

    lib.put_analysis(h, energy.ANALYSER, energy.VERSION, {"grid": "beats", "source": "stems"})
    energy.on_job_finished(jobs, lib, {**row, "kind": "stems"})
    energy.on_job_finished(jobs, lib, {"kind": "waveform", "state": "done", "payload": {"hash": h}})
    assert jobs.kinds == []


def test_clap_follows_energy_only_with_its_model(lib, monkeypatch):
    h = "e" * 40
    lib.upsert_track(h, 10.0)
    jobs = ListenerJobs()
    row = {"kind": "energy", "state": "done", "payload": {"hash": h}}
    vibe.on_job_finished(jobs, lib, row)
    assert jobs.kinds == []
    monkeypatch.setattr(vibe, "audio_ready", lambda: True)
    vibe.on_job_finished(jobs, lib, {**row, "state": "failed"})
    assert jobs.kinds == []
    vibe.on_job_finished(jobs, lib, row)
    assert jobs.kinds == [("clap", h)]


def test_queue_missing_fills_in_old_tracks(lib, monkeypatch):
    a, b = "a" * 40, "b" * 40
    lib.upsert_track(a, 10.0), lib.upsert_track(b, 10.0)
    lib.put_analysis(a, energy.ANALYSER, energy.VERSION, {"curve": []})
    jobs = ListenerJobs()
    assert energy.queue_missing(jobs, lib) == 0, "b's beats haven't run: its energy follows them"
    lib.put_analysis(b, "beats", "1", {"bpm": 120.0})
    assert energy.queue_missing(jobs, lib) == 1 and jobs.kinds == [("energy", b)]
    jobs.kinds.clear()
    assert vibe.queue_missing(jobs, lib) == 0
    monkeypatch.setattr(vibe, "audio_ready", lambda: True)
    assert vibe.queue_missing(jobs, lib) == 1 and jobs.kinds == [("clap", a)]


@needs_ffmpeg
def test_the_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    app = create_app(library_path=tmp_path / "support" / "library.sqlite3")
    with TestClient(app) as client:
        library = app.state.library if app.state.library is not None else None
        if library is None:
            from seedbox2.server.deps import get_library

            library = get_library(type("R", (), {"app": app})())
        nobody = "0" * 40
        for route in ("energy", "loudness", "vibe", "similar"):
            assert client.get(f"/api/analysis/{nobody}/{route}").status_code == 404

        h = "a" * 40
        library.upsert_track(h, 100.0, title="A", artist="X")
        energy_data = {"bar_s": [0.0, 50.0, 100.0], "curve": [0.2, 0.8], "rating": 7, "energy": 0.667}
        library.put_analysis(h, energy.ANALYSER, energy.VERSION, energy_data)
        library.put_analysis(h, energy.LOUDNESS_ANALYSER, energy.VERSION,
                             {"hop_s": 0.1, "lufs": [-14.0] * 996, "peak_dbtp": [-1.0] * 100, "integrated_lufs": -14.0,
                              "true_peak_dbtp": -1.0})  # fmt: skip
        assert client.get(f"/api/analysis/{h}/energy").json()["rating"] == 7
        loud = client.get(f"/api/analysis/{h}/loudness").json()
        assert loud["sections"]
        assert loud["sections"][-1]["integrated_lufs"] == pytest.approx(-14.0, abs=0.1)

        assert client.get(f"/api/analysis/{h}/similar").status_code == 409
        assert client.get(f"/api/analysis/{h}/vibe").status_code == 503, "the model isn't downloaded here"

        path = vibe.artifact_path(h)
        vibe.write_embeddings(path, [{"name": "main", "start_s": 5.0}], _unit(1, 0)[None])
        library.put_artifact(h, vibe.ARTIFACT_KIND, vibe.model_version(), path)
        win = client.get(f"/api/analysis/{h}/vibe").json()
        assert win["windows"] == [{"name": "main", "start_s": 5.0, "seconds": 10}]
        same = client.get(f"/api/analysis/{h}/similar", params={"limit": 3}).json()
        assert same["results"] == [] and same["seed"]["hash"] == h

        assert client.post("/api/analysis/vibe/search", json={"text": ""}).status_code == 422
        assert client.post("/api/analysis/vibe/search", json={"text": "warm", "key": "nope"}).status_code == 422
