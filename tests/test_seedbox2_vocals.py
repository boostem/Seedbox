"""Seedbox 2 vocal activity (#47): frame features on synthetic stems, hysteresis, phrases and their kinds, windows, the
planner's inputs and clash checks, the job and its API. The score on real audio is `python -m seedbox2.eval analysis
--analyser seedbox2-vocals` (it needs the models)."""

from __future__ import annotations

import numpy as np
import pytest

from musicreco import transitions
from seedbox2.analysis import vocals
from seedbox2.analysis.grid import Grid
from seedbox2.analysis.waveform import SAMPLE_RATE as SR
from seedbox2.eval import datasets
from seedbox2.eval.metrics import vocal_f

from .fixtures import DROP_HEAVY, DROP_HEAVY_B, Spec, analysis

BPM = 120.0
SPB = 60.0 / BPM


def voice(seconds: float, f0: float = 220.0, level: float = 0.2, seed: int = 0) -> np.ndarray:
    """A sung note: harmonics with vibrato."""
    t = np.arange(int(seconds * SR)) / SR
    phase = 2 * np.pi * (f0 * t + 3.0 * np.sin(2 * np.pi * 5.5 * t) / 5.5)
    return level * sum(np.sin(h * phase) / h for h in range(1, 12)).astype(np.float32) / 2


def spoken(seconds: float, level: float = 0.2, seed: int = 1) -> np.ndarray:
    """Noisy syllables four times a second: voice-band noise, no pitch to hold."""
    n = int(seconds * SR)
    rng = np.random.default_rng(seed)
    x = np.diff(rng.normal(size=n + 1)).astype(np.float32)
    env = 0.55 + 0.45 * np.sign(np.sin(2 * np.pi * 4.0 * np.arange(n) / SR))
    return level * x * env.astype(np.float32) * 0.5


def noise(seconds: float, level: float = 0.2, seed: int = 2) -> np.ndarray:
    return level * np.random.default_rng(seed).normal(size=int(seconds * SR)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), np.float32)


GRID = Grid.constant(0.0, BPM)


def test_voice_loud_against_the_mix_is_likely_and_quiet_flat_bleed_is_not() -> None:
    bed = noise(10.0, 0.2)
    f = vocals.frame_features(voice(10.0), bed)
    assert np.median(vocals.likelihood(f)) > vocals.FRAME_ON
    bleed = noise(10.0, 0.004, seed=3)
    f = vocals.frame_features(bleed, bed)
    assert np.median(vocals.likelihood(f)) < vocals.FRAME_OFF


def test_features_tell_a_voice_from_noise() -> None:
    bed = silence(5.0)
    v, n = vocals.frame_features(voice(5.0), bed), vocals.frame_features(noise(5.0), bed)
    assert np.median(v["harmonicity"]) > 0.7 > 0.3 > np.median(n["harmonicity"])
    assert np.median(v["flatness"]) < 0.3 < np.median(n["flatness"])


def test_kick_and_bass_bleed_do_not_read_as_pitch() -> None:
    t = np.arange(5 * SR) / SR
    bass = (0.2 * np.sin(2 * np.pi * 55.0 * t)).astype(np.float32)
    f = vocals.frame_features(bass + noise(5.0, 0.001), silence(5.0))
    assert np.median(f["harmonicity"]) < 0.7


def test_a_silent_stem_is_no_voice_at_all() -> None:
    f = vocals.frame_features(silence(4.0), noise(4.0))
    assert vocals.likelihood(f).max() == 0.0
    data = vocals.analyse_arrays(silence(8.0), noise(8.0), GRID)
    assert data["phrases"] == [] and "1" not in data["active"]
    assert [(w["start_s"], w["end_s"]) for w in data["instrumental"]] == [(0.0, pytest.approx(8.0, abs=0.1))]


def test_hysteresis_holds_a_value_hovering_at_the_threshold() -> None:
    x = np.array([0.1, 0.7, 0.5, 0.45, 0.5, 0.45, 0.3, 0.5])
    assert vocals.hysteresis(x, 0.6, 0.4).tolist() == [False, True, True, True, True, True, False, False]


def test_activity_lands_on_the_right_beats() -> None:
    v = np.concatenate([silence(8.0), voice(8.0), silence(8.0)])
    data = vocals.analyse_arrays(v, noise(24.0, 0.05), GRID, stems_model="m:1")
    on = np.array([c == "1" for c in data["active"]])
    assert data["first_beat"] == 0 and data["stems_model"] == "m:1"
    assert not on[:15].any() and on[17:31].all() and not on[33:].any()
    assert vocal_f([(8.0, 16.0)], vocals.spans_s(data), 24.0) > 0.95
    [p] = data["phrases"]
    assert p["start_beat"] == pytest.approx(16, abs=1) and p["end_beat"] == pytest.approx(32, abs=1)
    assert p["start_s"] == pytest.approx(8.0, abs=SPB) and p["kind"] == "lead"


def test_short_gaps_stay_inside_a_phrase_and_long_ones_split_it() -> None:
    on = np.zeros(40, bool)
    on[0:8] = on[10:14] = on[20:24] = True
    f = {k: np.zeros(40 * 22) for k in ("vocal_db", "mix_db", "side", "harmonicity")}
    active = np.ones(40 * 22, bool)
    edges = np.arange(41) * 22 * vocals.FRAME_S
    out = vocals.phrases(on, 4, edges, f, active)
    assert [(p["start_beat"], p["end_beat"]) for p in out] == [(4, 18), (24, 28)]


@pytest.mark.parametrize("make, kind", [(lambda n: voice(n), "lead"), (lambda n: spoken(n), "rap")])
def test_kinds_sung_and_spoken(make, kind: str) -> None:
    data = vocals.analyse_arrays(make(16.0), noise(16.0, 0.03), GRID)
    assert [p["kind"] for p in data["phrases"]] == [kind]


def test_kind_chop_a_short_shot_and_a_stutter() -> None:
    one_shot = np.concatenate([silence(8.0), voice(SPB * 1.5), silence(8.0)])
    data = vocals.analyse_arrays(one_shot, noise(one_shot.size / SR, 0.03), GRID)
    assert [p["kind"] for p in data["phrases"]] == ["chop"]
    frames = np.zeros(400, bool)
    frames[::20] = frames[1::20] = frames[2::20] = True
    f = {k: np.zeros(400) for k in ("vocal_db", "mix_db", "side", "harmonicity")}
    assert vocals.classify(16, 400 * vocals.FRAME_S, f, frames, 0.0) == "chop"


def test_kind_backing_is_quiet_against_the_lead_or_wide() -> None:
    f = {"vocal_db": np.full(100, -20.0), "mix_db": np.zeros(100), "side": np.zeros(100),
         "harmonicity": np.full(100, 0.9)}  # fmt: skip
    active = np.ones(100, bool)
    assert vocals.classify(16, 2.3, f, active, -3.0) == "backing"
    assert vocals.classify(16, 2.3, f, active, -20.0) == "lead"
    f = {**f, "side": np.full(100, 0.5), "vocal_db": np.full(100, -10.0)}
    assert vocals.classify(16, 2.3, f, active, -5.0) == "backing"
    assert vocals.classify(16, 2.3, f, active, -10.0) == "lead"


def test_windows_acapella_and_instrumental() -> None:
    v = np.concatenate([silence(8.0), voice(8.0), voice(8.0)])
    bed = np.concatenate([noise(8.0, 0.1), silence(8.0), noise(8.0, 0.1)])
    data = vocals.analyse_arrays(v, bed, GRID)
    [a] = data["acapella"]
    assert a["start_s"] == pytest.approx(8.0, abs=SPB * 1.5) and a["end_s"] == pytest.approx(16.0, abs=SPB * 1.5)
    [i] = data["instrumental"]
    assert i["start_s"] == 0.0 and i["end_s"] == pytest.approx(8.0, abs=SPB * 1.5)
    assert all(w["start_beat"] < w["end_beat"] for w in data["acapella"] + data["instrumental"])


def test_silence_is_no_instrumental_window() -> None:
    v = np.concatenate([silence(8.0), voice(8.0)])
    bed = np.concatenate([silence(8.0), noise(8.0, 0.1)])
    assert vocals.analyse_arrays(v, bed, GRID)["instrumental"] == []


def test_beats_before_the_first_detection_count() -> None:
    grid = Grid.constant(4.0, BPM)  # beat 0 at 4 s: a sung intro sits on negative beats
    data = vocals.analyse_arrays(voice(8.0), noise(8.0, 0.02), grid)
    assert data["first_beat"] == -8 and data["phrases"][0]["start_beat"] == -8


def test_a_tempo_change_stays_on_its_beats() -> None:
    grid = Grid([{"start_beat": 0, "start_s": 0.0, "bpm": 120.0}, {"start_beat": 16, "start_s": 8.0, "bpm": 60.0}])
    v = np.concatenate([silence(8.0), voice(8.0)])
    data = vocals.analyse_arrays(v, noise(16.0, 0.02), grid)
    assert data["phrases"][0]["start_beat"] == pytest.approx(16, abs=1)
    assert data["phrases"][0]["end_beat"] == 24


# --- the planner's inputs ---------------------------------------------------------------------------------------------


def _analysis(bars: int = 32, vocal: float = 0.0) -> dict:
    return {"bar_count": bars, "first_downbeat_s": 0.0, "bar_s": 2.0, "vocal_per_bar": [vocal] * bars,
            "intro": {"bars": 8, "start_s": 0.0, "end_s": 16.0, "has_vocals": vocal >= 0.5},
            "outro": {"bars": 0, "start_s": 64.0, "end_s": 64.0, "has_vocals": vocal >= 0.5}}  # fmt: skip


def _data(activity: list[float]) -> dict:
    return {"segments": GRID.to_json(), "first_beat": 0, "activity": activity, "phrases": [], "acapella": [],
            "instrumental": [], "active": ""}  # fmt: skip


def test_plan_input_replaces_the_heuristic() -> None:
    an = _analysis(vocal=0.9)  # the heuristic hears a synth lead as vocals all the way through
    activity = [0.0] * 32 + [1.0] * 32 + [0.0] * 64
    out = vocals.plan_input(an, _data(activity))
    assert out["vocal_per_bar"][:8] == [0.0] * 8 and out["vocal_per_bar"][8:16] == [1.0] * 8
    assert out["vocal_per_bar"][16:] == [0.0] * 16
    assert out["intro"]["has_vocals"] is False and out["outro"]["has_vocals"] is False
    assert out["vocal_source"] == "stems"
    assert an["vocal_per_bar"] == [0.9] * 32  # a copy


def test_plan_input_marks_a_vocal_edge() -> None:
    out = vocals.plan_input(_analysis(), _data([1.0] * 16 + [0.0] * 92 + [1.0] * 20))
    assert out["intro"]["has_vocals"] is True  # 4 of its 8 bars
    assert out["outro"]["has_vocals"] is True  # a 0-bar edge looks at the last 8 bars, as structure.py does


def test_plan_input_without_data_keeps_the_heuristic() -> None:
    an = _analysis(vocal=0.9)
    assert vocals.plan_input(an, None) is an


def test_bars_past_the_data_are_not_sung() -> None:
    out = vocals.plan_input(_analysis(), _data([1.0] * 16))
    assert out["vocal_per_bar"][:4] == [1.0] * 4 and out["vocal_per_bar"][5:] == [0.0] * 27


def _on_grid_of(spec: Spec, value: float) -> dict:
    """Vocal data with every beat of the track at `value`, on the track's own grid."""
    beats = spec.bars * 4
    grid = Grid.constant(spec.first_downbeat_s, spec.bpm)
    return {**_data([value] * beats), "segments": grid.to_json()}


def _clash(a: dict, b: dict) -> list[int]:
    blend = transitions._best_blend(transitions.make_grid(a), transitions.make_grid(b), key_clash=False)
    assert blend is not None
    return blend.clash


def test_clash_check_reads_the_new_data() -> None:
    """The planner itself: a pair the heuristic says clashes, so the blend is dropped for a drop swap. The vocal stem
    says neither deck is singing (synth leads), and the blend comes back; both singing throughout, it stays a clash."""
    a, b = analysis(DROP_HEAVY), analysis(DROP_HEAVY_B)
    heuristic = _clash(a, b)
    assert len(heuristic) > 0
    plan = transitions._plan(transitions.make_grid(a), transitions.make_grid(b), key_clash=False)
    assert plan.technique == "drop swap"

    clear_a, clear_b = (vocals.plan_input(an, _on_grid_of(s, 0.0)) for an, s in ((a, DROP_HEAVY), (b, DROP_HEAVY_B)))
    assert _clash(clear_a, clear_b) == []
    plan = transitions._plan(transitions.make_grid(clear_a), transitions.make_grid(clear_b), key_clash=False)
    assert plan.technique != "drop swap"

    sung_a, sung_b = (vocals.plan_input(an, _on_grid_of(s, 1.0)) for an, s in ((a, DROP_HEAVY), (b, DROP_HEAVY_B)))
    assert len(_clash(sung_a, sung_b)) > 0


# --- the job --------------------------------------------------------------------------------------------------------


class FakeLibrary:
    def __init__(self, analyses: dict | None = None, folder=None, model: str = "htdemucs:1") -> None:
        self.rows, self.folder, self.model, self.put = dict(analyses or {}), folder, model, []

    def analysis(self, hash_: str, analyser: str, version: str | None = None) -> dict | None:
        data = self.rows.get(analyser)
        return None if data is None else {"data": data}

    def artifacts(self, hash_: str, kind: str) -> list[dict]:
        return [{"path": str(self.folder), "model_version": self.model}] if self.folder else []

    def put_analysis(self, hash_: str, analyser: str, version: str, data: dict) -> None:
        self.rows[analyser] = data
        self.put.append(analyser)


class FakeCtx:
    def __init__(self, lib: FakeLibrary) -> None:
        self.library = lib

    def progress(self, _f: float) -> None:
        pass


class FakeJobs:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, kind: str, payload: dict, **kw) -> dict:
        self.submitted.append((kind, payload["hash"], kw.get("priority")))
        return {"id": 1, "state": "queued"}


@pytest.fixture
def stem_folder(tmp_path):
    from scipy.io import wavfile

    parts = (("vocals", voice(8.0)), ("drums", noise(8.0, 0.05)), ("bass", silence(8.0)), ("other", silence(8.0)))
    for name, x in parts:
        wavfile.write(tmp_path / f"{name}.wav", SR, x)
    return tmp_path


BEATS = {"segments": GRID.to_json()}


def test_job_stores_the_analysis_and_does_not_repeat_it(stem_folder) -> None:
    lib = FakeLibrary({"beats": BEATS}, stem_folder)
    out = vocals.vocals_job({"hash": "h"}, FakeCtx(lib))
    assert lib.put == ["vocals"] and out["phrases"] == 1 and lib.rows["vocals"]["stems_model"] == "htdemucs:1"
    assert vocals.vocals_job({"hash": "h"}, FakeCtx(lib))["cached"] is True and lib.put == ["vocals"]


def test_new_stems_redo_it(stem_folder) -> None:
    lib = FakeLibrary({"beats": BEATS}, stem_folder)
    vocals.vocals_job({"hash": "h"}, FakeCtx(lib))
    lib.model = "htdemucs:2"
    vocals.vocals_job({"hash": "h"}, FakeCtx(lib))
    assert lib.put == ["vocals", "vocals"] and lib.rows["vocals"]["stems_model"] == "htdemucs:2"


def test_job_waits_for_stems_and_beats(stem_folder, tmp_path) -> None:
    assert vocals.vocals_job({"hash": "h"}, FakeCtx(FakeLibrary({"beats": BEATS})))["skipped"] == "needs stems"
    assert vocals.vocals_job({"hash": "h"}, FakeCtx(FakeLibrary({}, stem_folder)))["skipped"] == "needs beats"


@pytest.mark.parametrize("kind", ["stems", "beats"])
def test_either_arriving_last_queues_the_job(stem_folder, kind: str) -> None:
    jobs = FakeJobs()
    row = {"kind": kind, "state": "done", "payload": {"hash": "h"}, "result": {}}
    vocals.on_job_finished(jobs, FakeLibrary({"beats": BEATS}, stem_folder), row)
    assert jobs.submitted == [("vocals", "h", "library")]


def test_nothing_queued_until_both_are_there_or_for_other_jobs(stem_folder) -> None:
    jobs = FakeJobs()
    done = {"kind": "stems", "state": "done", "payload": {"hash": "h"}, "result": {}}
    vocals.on_job_finished(jobs, FakeLibrary({}, stem_folder), done)
    vocals.on_job_finished(jobs, FakeLibrary({"beats": BEATS}), done)
    vocals.on_job_finished(jobs, FakeLibrary({"beats": BEATS}, stem_folder), {**done, "state": "failed"})
    vocals.on_job_finished(jobs, FakeLibrary({"beats": BEATS}, stem_folder), {**done, "kind": "waveform"})
    assert jobs.submitted == []


def test_cached_stems_do_not_repeat_a_finished_analysis(stem_folder) -> None:
    jobs = FakeJobs()
    row = {"kind": "stems", "state": "done", "payload": {"hash": "h"}, "result": {"cached": True}}
    vocals.on_job_finished(jobs, FakeLibrary({"beats": BEATS, "vocals": {}}, stem_folder), row)
    assert jobs.submitted == []


# --- the evaluation ---------------------------------------------------------------------------------------------------


def test_stem_spans_from_a_true_vocal_stem() -> None:
    sr = 8000
    x = np.zeros(sr * 10)
    x[sr * 2 : sr * 4] = 0.3
    x[sr * 4 + sr // 10 : sr * 6] = 0.3  # a breath: bridged
    x[sr * 8 : sr * 9] = 0.3
    assert datasets.stem_spans(x, sr) == [(2.0, 6.0), (8.0, 9.0)]
    assert datasets.stem_spans(np.zeros(sr), sr) == []


def test_read_lab_and_jamendo_test_split(tmp_path) -> None:
    base = tmp_path / "jamendo"
    (base / "filelists").mkdir(parents=True)
    (base / "audio").mkdir()
    (base / "labels").mkdir()
    (base / "filelists" / "test").write_text("a song.mp3\nmissing.ogg\n")
    (base / "audio" / "a song.mp3").write_bytes(b"x")
    (base / "audio" / "held for fitting.mp3").write_bytes(b"x")
    (base / "labels" / "a song.lab").write_text("0.0 1.5 nosing\n1.5 3.0 sing\n3.0 4.0 nosing\n")
    [ref] = datasets.load("jamendo", tmp_path)
    assert ref.track == "a song" and ref.vocals == [(1.5, 3.0)]


def test_musdb18_caches_the_spans(tmp_path) -> None:
    import wave

    folder = tmp_path / "test" / "A Track"
    folder.mkdir(parents=True)
    (folder / "mixture.wav").write_bytes(b"x")
    pcm = np.zeros((44100 * 4, 2), np.int16)
    pcm[44100:88200] = 8000
    with wave.open(str(folder / "vocals.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(pcm.tobytes())
    [ref] = datasets.load("musdb18", tmp_path)
    assert ref.vocals == [(1.0, 2.0)]
    (folder / "vocals.wav").unlink()
    (folder / "vocals.wav").write_bytes(b"gone")  # read from the cache this time
    assert datasets.load("musdb18", tmp_path)[0].vocals == [(1.0, 2.0)]


# --- the API and the library ------------------------------------------------------------------------------------------


class ApiLibrary(FakeLibrary):
    def track(self, hash_: str) -> dict | None:
        return {"hash": hash_} if hash_ == "h" else None


def test_endpoint_404_waiting_queued_and_served(stem_folder, monkeypatch) -> None:
    from fastapi import HTTPException

    from seedbox2.server.routers import analysis as router

    jobs = FakeJobs()
    monkeypatch.setattr(router, "get_jobs", lambda _request: jobs)
    with pytest.raises(HTTPException):
        router.track_vocals("nope", None, ApiLibrary())
    waiting = router.track_vocals("h", None, ApiLibrary({"beats": BEATS}))
    assert waiting.status_code == 202 and jobs.submitted == []
    queued = router.track_vocals("h", None, ApiLibrary({"beats": BEATS}, stem_folder))
    assert queued.status_code == 202 and [j[0] for j in jobs.submitted] == ["vocals"]
    assert router.track_vocals("h", None, ApiLibrary({"vocals": {"phrases": []}})) == {"phrases": []}


def test_queue_missing_finds_tracks_with_stems_and_beats_but_no_vocals(tmp_path) -> None:
    from seedbox2.library.repo import Library

    lib = Library.open(tmp_path / "lib.sqlite3")
    for h in ("a" * 40, "b" * 40, "c" * 40, "d" * 40):
        lib.upsert_track(h, duration=60.0)
    a, b, c, d = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    for h in (a, b, c):
        lib.put_artifact(h, "stems", "htdemucs:1", tmp_path)
    for h in (a, b, d):
        lib.put_analysis(h, "beats", "1", {"segments": []})
    lib.put_analysis(b, vocals.ANALYSER, vocals.VERSION, {})
    jobs = FakeJobs()
    assert vocals.queue_missing(jobs, lib) == 1  # a: b is done, c has no beats, d no stems
    assert jobs.submitted == [("vocals", a, "background")]


def test_a_killed_separation_does_not_look_finished(tmp_path, monkeypatch) -> None:
    """The analyser's stems cache: all four files of an interrupted run exist, truncated, and must not be reused."""
    from seedbox2.analysis import beats, stems
    from seedbox2.eval.analysers import seedbox2_vocals

    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("seedbox2.models.load_session", lambda _name: None)
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"x")

    def killed(_path, out, _session):
        out.mkdir(parents=True)
        for name in ("vocals", "drums", "bass", "other"):
            (out / f"{name}.flac").write_bytes(b"truncated")
        raise KeyboardInterrupt

    monkeypatch.setattr(stems, "separate", killed)
    with pytest.raises(KeyboardInterrupt):
        seedbox2_vocals(audio)
    calls = []

    def fine(path, out, _session):
        calls.append(path)
        out.mkdir(parents=True, exist_ok=True)
        from scipy.io import wavfile

        parts = (("vocals", voice(8.0)), ("drums", noise(8.0, 0.05)), ("bass", silence(8.0)), ("other", silence(8.0)))
        for name, x in parts:
            wavfile.write(out / f"{name}.wav", SR, x)

    monkeypatch.setattr(stems, "separate", fine)
    monkeypatch.setattr(beats, "analyse", lambda *_a, **_k: {"segments": GRID.to_json()})
    assert seedbox2_vocals(audio).vocals
    assert calls == [audio], "the interrupted run's files were not reused"
