"""Seedbox 2's sections (#46): bar features from stem files, boundaries, labels, mood, fill-ins, where to mix, the job."""

from __future__ import annotations

import tracemalloc
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.io import wavfile

from musicreco.paths import ffmpeg_path
from seedbox2.analysis import sections as sc
from seedbox2.analysis import stems as st
from seedbox2.analysis.grid import Grid
from seedbox2.eval import metrics
from seedbox2.eval.analysers import ANALYSERS, Estimate
from seedbox2.eval.datasets import Reference
from seedbox2.jobs import REGISTRY
from seedbox2.library import Library
from seedbox2.library.service import Settings

needs_ffmpeg = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")

SR = sc.SAMPLE_RATE
BPM = 140.0
BEAT = 60.0 / BPM
BAR = 4 * BEAT
#          kind        bars  drums  bass  other  vocals
ARRANGEMENT = [
    ("intro", 16, True, False, False, False),
    ("body", 32, True, True, True, True),
    ("breakdown", 16, False, False, True, False),
    ("build", 8, "roll", False, True, False),
    ("drop", 32, True, True, True, False),
    ("outro", 16, True, False, False, False),
]
FILL_BEFORE = 48  # bar: the two beats before this phrase start have a drum fill


def _hit(x: np.ndarray, at: float, freq: float | None, seconds: float, gain: float, rng: np.random.Generator) -> None:
    i = int(at * SR)
    n = min(int(seconds * SR), x.size - i)
    if n <= 0:
        return
    env = np.exp(-np.arange(n) / (0.25 * seconds * SR))
    src = np.sin(2 * np.pi * freq * np.arange(n) / SR) if freq else rng.standard_normal(n)
    x[i:i + n] += gain * env * src


def synth(arrangement=ARRANGEMENT, fill_before: int | None = FILL_BEFORE) -> dict[str, np.ndarray]:
    """Four stems: four-on-the-floor kicks with hats on the off-beats, a bass line, a chord pad, a sung tone."""
    bars = sum(a[1] for a in arrangement)
    size = int((bars * BAR + 1.0) * SR)
    rng = np.random.default_rng(46)
    out = {s: np.zeros(size, np.float32) for s in sc.STEMS}
    t = np.arange(size) / SR
    pad = 0.08 * (np.sin(2 * np.pi * 220.0 * t) + np.sin(2 * np.pi * 261.63 * t) + np.sin(2 * np.pi * 329.63 * t))
    voice = 0.2 * np.sin(2 * np.pi * 440.0 * t + 3 * np.sin(2 * np.pi * 5 * t))
    bar = 0
    for _kind, length, drums, bass, other, vocals in arrangement:
        a, b = int(bar * BAR * SR), int((bar + length) * BAR * SR)
        if other:
            out["other"][a:b] = pad[a:b]
        if vocals:
            out["vocals"][a:b] = voice[a:b]
        for beat in range(bar * 4, (bar + length) * 4):
            at = beat * BEAT
            if drums is True:
                _hit(out["drums"], at, 60.0, 0.2, 0.7, rng)
                _hit(out["drums"], at + BEAT / 2, None, 0.05, 0.15, rng)
            elif drums == "roll":  # a snare roll: each bar denser and louder
                progress = (beat - bar * 4) / (length * 4)
                hits = 1 + int(3 * progress)
                for k in range(hits):
                    _hit(out["drums"], at + k * BEAT / hits, None, 0.08, 0.3 + 0.3 * progress, rng)
            if bass:
                _hit(out["bass"], at + BEAT / 2, 55.0, BEAT / 2, 0.4, rng)
        bar += length
    if fill_before is not None:
        for k in range(8):  # eight sixteenths over the last two beats
            _hit(out["drums"], (fill_before * 4 - 2) * BEAT + k * BEAT / 4, None, 0.05, 0.5, rng)
    return out


def truth(arrangement=ARRANGEMENT) -> list[tuple[str, int, int]]:
    out, bar = [], 0
    for a in arrangement:
        out.append((a[0], bar, bar + a[1]))
        bar += a[1]
    return out


def beats_result(duration: float) -> dict:
    return {"segments": [{"start_beat": 0.0, "start_s": 0.0, "bpm": BPM}], "bpm": BPM, "duration_s": duration}


def write_stems(folder: Path, stems: dict[str, np.ndarray]) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    for s, x in stems.items():
        files[s] = folder / f"{s}.wav"
        wavfile.write(files[s], SR, x)
    return files


@pytest.fixture(scope="module")
def features(tmp_path_factory: pytest.TempPathFactory) -> sc.BarFeatures:
    if ffmpeg_path() is None:
        pytest.skip("needs ffmpeg")
    stems = synth()
    files = write_stems(tmp_path_factory.mktemp("stems"), stems)
    return sc.bar_features(files, beats_result(stems["drums"].size / SR))


@pytest.fixture(scope="module")
def result(features: sc.BarFeatures) -> dict:
    return sc.decide(features)


# --- bar features ----------------------------------------------------------------------------------------------------


def test_bar_features_follow_the_grid_and_say_which_stem_plays(features):
    n = sum(a[1] for a in ARRANGEMENT)
    assert features.n in (n, n + 1) and features.first_bar_beat == 0
    assert np.allclose(features.bar_s[:n], np.arange(n) * BAR, atol=1e-6)
    on = features.on()[:n]
    bar = 0
    for _kind, length, drums, bass, other, vocals in ARRANGEMENT:
        mid = range(bar + 1, bar + length - 1)
        for stem, want in zip(range(4), (bool(drums), bass, other, vocals)):
            if drums == "roll" and stem == 0:
                continue  # it starts too quiet to count
            assert on[mid, stem].mean() == float(want), (_kind, sc.STEMS[stem])
        bar += length


def test_onset_density_and_chroma(features):
    body = range(20, 44)
    assert features.kick[body].mean() == pytest.approx(1.0, abs=0.15)  # one kick per beat
    assert features.onsets[body, 0].mean() >= 1.0
    assert features.onsets[20:44, 1].mean() == pytest.approx(1.0, abs=0.3)  # one bass note per beat
    assert features.drum_grid.shape == (features.n * 4, 4)
    pitch_classes = np.argsort(features.chroma[52:60].mean(axis=0))[-3:]  # the pad alone in the breakdown: A, C, E
    assert sorted(pitch_classes.tolist()) == [0, 3, 7]


def test_a_grid_starting_after_zero_keeps_the_music_before_it(tmp_path):
    if ffmpeg_path() is None:
        pytest.skip("needs ffmpeg")
    grid = Grid([{"start_beat": 0.0, "start_s": 10 * BAR, "bpm": BPM}])
    bar_s, first = sc.bars_of(grid, 20 * BAR)
    assert first == -40 and bar_s[0] == pytest.approx(0.0) and bar_s.size == 21


# --- boundaries ------------------------------------------------------------------------------------------------------


def test_boundaries_land_within_a_bar_of_the_arrangement(result):
    got = {s["start_bar"] for s in result["sections"]}
    for _kind, start, _end in truth()[1:]:
        assert min(abs(start - g) for g in got) <= 1, (start, sorted(got))
    assert len(got) <= len(ARRANGEMENT) + 2
    assert result["sections"][0]["start_bar"] == 0 and result["sections"][-1]["end_bar"] == result["bar_count"]
    assert all(s["end_bar"] - s["start_bar"] >= sc.MIN_SEGMENT for s in result["sections"])


def test_novelty_peaks_where_stems_come_in_and_go_out(features):
    nov = sc.novelty(features)
    bounds = [s for _, s, _ in truth()[1:]]
    middle = [b + 8 for b in bounds[:-1] if b + 8 not in bounds]
    assert min(nov[bounds]) > max(nov[middle])


def test_phrase_offset_finds_a_shifted_grid():
    nov = np.zeros(129)
    nov[3::8] = 1.0
    assert sc.phrase_offset(nov) == 3
    assert sc.phrase_offset(np.zeros(129)) == 0
    assert sc.boundary_strength(np.r_[np.zeros((20, 2)), np.ones((20, 2))], [0, 20, 40])[20] == pytest.approx(np.sqrt(2))


def test_grid_cost_prefers_big_multiples():
    assert list(sc.grid_cost(np.array([64, 16, 8, 4, 2, 3]))) == [0.0, 0.1, 0.2, 0.5, 0.9, 1.5]


def test_the_dp_takes_o_of_n_memory():
    """Seedbox 1's tables for 2,000 bars would take over 200 MB: (n+1)² x 7 doubles."""
    rng = np.random.default_rng(1)
    n = 2000
    g = rng.standard_normal((n, 30)) * 0.05
    g[n // 2:] += 2.0
    nov = np.zeros(n + 1)
    tracemalloc.start()
    bounds = sc.segment(g, nov, 0)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    assert any(abs(b - n // 2) <= 1 for b in bounds)


# --- labels ----------------------------------------------------------------------------------------------------------


def test_labels_from_the_stems(result):
    kinds = [s["kind"] for s in result["sections"]]
    assert kinds == [a[0] for a in ARRANGEMENT], [(s["kind"], s["start_bar"]) for s in result["sections"]]


def test_a_radio_edit_that_stays_full_has_no_intro_or_outro(tmp_path):
    if ffmpeg_path() is None:
        pytest.skip("needs ffmpeg")
    arrangement = [("body", 48, True, True, True, True)]
    stems = synth(arrangement, fill_before=None)
    r = sc.analyse(write_stems(tmp_path, stems), beats_result(stems["drums"].size / SR))
    assert {s["kind"] for s in r["sections"]} == {"body"}
    assert r["mix_in"] and r["mix_out"]  # there is still somewhere to mix, with vocals, at a lower score
    assert r["mix_out"][0]["vocals"] and r["mix_out"][0]["score"] < 0.7


def test_mood_rises_with_tempo_and_drive(features):
    assert sc.mood(features)[0] in ("high", "mid")
    calm = sc.BarFeatures(**{**features.__dict__, "bpm": 72.0, "onsets": features.onsets * 0.2})
    calm.db = calm.db.copy()
    calm.db[:, 1] = -80.0  # no bass
    assert sc.mood(calm)[0] == "low"
    assert sc.mood(calm)[1] < sc.mood(features)[1]


# --- fill-ins --------------------------------------------------------------------------------------------------------


def test_a_fill_is_found_with_its_length_and_only_there(result):
    bar = {f["bar"]: f for f in result["fills"]}
    assert FILL_BEFORE in bar, result["fills"]
    fill = bar[FILL_BEFORE]
    assert fill["beats"] == 2 and fill["start_beat"] == FILL_BEFORE * 4 - 2
    assert fill["start_s"] == pytest.approx((FILL_BEFORE * 4 - 2) * BEAT, abs=0.01)
    assert fill["strength"] > 0.3
    # the snare roll into the drop and drums coming back may count; the plain groove may not
    assert not [f for f in result["fills"] if f["bar"] not in (FILL_BEFORE, 64, 72, 104)]


# --- where to mix ----------------------------------------------------------------------------------------------------


def test_mix_in_and_mix_out_candidates_are_scored_and_sorted(result):
    assert result["mix_in"] == sorted(result["mix_in"], key=lambda c: -c["score"])
    assert result["mix_out"] == sorted(result["mix_out"], key=lambda c: -c["score"])
    best_in, best_out = result["mix_in"][0], result["mix_out"][0]
    assert best_in["section"] == "intro" and best_in["bar"] == 0 and best_in["bars"] >= 16
    assert best_in["drums"] and not best_in["vocals"] and not best_in["bass"]
    assert best_out["section"] == "outro" and best_out["bar"] == 104 and not best_out["vocals"]
    assert all(0 <= c["score"] <= 1 for c in result["mix_in"] + result["mix_out"])


# --- the job and the eval --------------------------------------------------------------------------------------------


class Ctx:
    def __init__(self, lib: Library, settings_file: Path) -> None:
        self.library = lib
        self.settings = Settings(settings_file)
        self.fractions: list[float] = []

    def progress(self, fraction: float, _message: str | None = None) -> None:
        self.fractions.append(fraction)


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    library = Library.open(tmp_path / "library.sqlite3")
    yield library
    library.close()


@needs_ffmpeg
def test_the_job_uses_stems_and_beats_when_it_has_them(lib, tmp_path):
    arrangement = [a for a in ARRANGEMENT if a[0] in ("intro", "drop")]
    stems = synth(arrangement, fill_before=None)
    mix = sum(stems.values())
    wav = tmp_path / "mix.wav"
    wavfile.write(wav, SR, mix)
    h = lib.add_file(wav)["hash"]
    lib.put_analysis(h, "beats", "1", beats_result(mix.size / SR))
    folder = st.folder_for(h)
    write_stems(folder, stems)
    lib.put_artifact(h, "stems", "htdemucs:test", folder)
    ctx = Ctx(lib, tmp_path / "s.json")
    done = sc.sections_job({"hash": h}, ctx)
    assert done["source"] == "stems" and ctx.fractions
    data = lib.analysis(h, sc.ANALYSER, sc.VERSION)["data"]
    assert data["source"] == "stems" and len(data["activity"]["drums"]) == data["bar_count"]
    assert data["mood"] in ("high", "mid", "low")


def test_without_stems_the_job_falls_back_to_seedbox1(lib, tmp_path, monkeypatch):
    from musicreco import structure

    wav = tmp_path / "mix.wav"
    wavfile.write(wav, SR, np.zeros(SR, np.float32))
    h = lib.add_file(wav)["hash"]
    monkeypatch.setattr(structure, "analyze", lambda _p: {
        "duration_s": 60.0, "bar_count": 30, "bar_s": 2.0, "first_downbeat_s": 0.0, "phrase_offset": 0,
        "sections": [{"kind": "intro", "start_bar": 0, "end_bar": 8, "start_s": 0.0, "end_s": 16.0},
                     {"kind": "body", "start_bar": 8, "end_bar": 30, "start_s": 16.0, "end_s": 60.0}],
        "intro": {"bars": 8, "has_vocals": False, "drums_only": True},
        "outro": {"bars": 0, "has_vocals": True, "drums_only": False}})  # fmt: skip
    assert sc.sections_job({"hash": h}, Ctx(lib, tmp_path / "s.json"))["source"] == "seedbox1"
    data = lib.analysis(h, sc.ANALYSER, sc.VERSION)["data"]
    assert [s["kind"] for s in data["sections"]] == ["intro", "body"]
    assert data["mix_in"][0]["section"] == "intro" and data["mix_out"][0]["bar"] == 22
    assert data["activity"] is None and data["fills"] == []


class Jobs:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []

    def submit(self, kind: str, payload: dict, **_kw: Any) -> dict:
        self.submitted.append((kind, payload))
        return {"id": 1, "state": "queued", "kind": kind}


def test_the_listener_waits_for_beats_and_stems(lib):
    lib.upsert_track("aa11", 60.0)
    jobs = Jobs()

    def finished(kind: str, state: str = "done", **result: Any) -> None:
        sc.on_job_finished(jobs, lib, {"kind": kind, "state": state, "payload": {"hash": "aa11"}, "result": result})

    finished("stems", path="x")  # no beats yet
    finished("waveform")
    assert jobs.submitted == []
    lib.put_analysis("aa11", "beats", "1", beats_result(60.0))
    finished("beats")  # no stems, none skipped: they are still coming
    assert jobs.submitted == []
    finished("stems", state="failed")
    assert jobs.submitted == []
    finished("stems", skipped="cache_full")  # they aren't coming: use what there is
    assert jobs.submitted == [("sections", {"hash": "aa11"})]
    lib.put_analysis("aa11", sc.ANALYSER, sc.VERSION, {"source": "seedbox1"})
    finished("stems", skipped="cache_full")
    assert len(jobs.submitted) == 1  # nothing new
    folder = st.folder_for("aa11")
    folder.mkdir(parents=True)
    for stem in sc.STEMS:
        (folder / f"{stem}.flac").write_bytes(b"x")
    lib.put_artifact("aa11", "stems", "htdemucs:test", folder)
    finished("stems", path=str(folder))  # stems arrived: they replace the fallback
    assert len(jobs.submitted) == 2
    lib.put_analysis("aa11", sc.ANALYSER, sc.VERSION, {"source": "stems"})
    finished("beats")
    assert len(jobs.submitted) == 2


def test_the_job_type_is_registered():
    assert REGISTRY.get("sections").pool == "light"


def test_features_survive_a_save(features, tmp_path):
    features.save(tmp_path / "f.npz")
    back = sc.BarFeatures.load(tmp_path / "f.npz")
    assert back.bpm == features.bpm and back.first_bar_beat == features.first_bar_beat
    assert sc.decide(back)["sections"] == sc.decide(features)["sections"]


def test_the_eval_scores_our_sections(result):
    assert "seedbox2-sections" in ANALYSERS
    ref = Reference("x", "t", Path("t.wav"), downbeats=[i * BAR for i in range(128)], duration_s=result["duration_s"],
                    sections=[(s * BAR, k) for k, s, _ in truth()] + [(120 * BAR, "end")])  # fmt: skip
    marks = [(s["start_s"], s["kind"]) for s in result["sections"]] + [(result["duration_s"], "end")]
    assert metrics.boundary_f(ref, Estimate(duration_s=result["duration_s"], sections=marks)) == pytest.approx(1.0)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from seedbox2.server import start_server

    from .fixtures import Spec, click_track

    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))  # no models or stems: the Seedbox 1 fallback
    library = Library.open(tmp_path / "library.sqlite3")
    wav = tmp_path / "song.wav"
    wavfile.write(wav, 22050, click_track(Spec("served", bpm=126.0, bars=24, intro_bars=4, outro_bars=4,
                                               first_downbeat_s=0.4), 22050, 1500.0).T)  # fmt: skip
    track = library.add_file(wav)
    library.close()
    handle = start_server(library_path=tmp_path / "library.sqlite3")
    handle.hash = track["hash"]
    yield handle
    handle.stop()


@needs_ffmpeg
def test_the_endpoint_queues_the_job_then_serves_the_sections(server):
    import json
    import time
    import urllib.error
    import urllib.request

    def get(url: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - localhost
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    url = f"{server.url}/api/analysis/{server.hash}/sections"
    status, body = get(url)
    assert status == 202 and body["job"]["kind"] == "sections"
    deadline = time.monotonic() + 60
    while (got := get(url))[0] == 202 and time.monotonic() < deadline:
        time.sleep(0.2)
    assert got[0] == 200 and got[1]["source"] == "seedbox1" and got[1]["sections"]
    assert get(f"{server.url}/api/analysis/{'0' * 40}/sections")[0] == 404
