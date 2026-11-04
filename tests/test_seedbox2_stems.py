"""Seedbox 2's stems (#44): the transforms against torch, overlap-add, the files, the cache, the job and the API."""

from __future__ import annotations

import json
import os
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from musicreco.paths import ffmpeg_path
from seedbox2.analysis import stems as st
from seedbox2.analysis import waveform as wf
from seedbox2.jobs import Registry, Supervisor
from seedbox2.jobs.worker import Cancelled
from seedbox2.library import Library
from seedbox2.library.service import Settings
from seedbox2.models.registry import MODELS
from seedbox2.models.runtime import session_options
from seedbox2.server.events import EventBus

needs_ffmpeg = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")
SHARES = np.array([0.1, 0.2, 0.3, 0.4], np.float32)  # what the fake model gives each stem of the mix


class FakeSession:
    """The graph's signature, giving each stem a share of the mix's spectrogram (none through the time branch)."""

    def __init__(self) -> None:
        self.calls = 0

    def get_inputs(self) -> list[Any]:
        class Input:
            def __init__(self, name: str) -> None:
                self.name = name

        return [Input("input"), Input("x")]

    def run(self, _names: Any, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.calls += 1
        assert feed["input"].shape == (1, 2, st.SEGMENT) and feed["x"].shape == (1, 4, 2048, st.FRAMES)
        spec = SHARES[None, :, None, None, None] * feed["x"][:, None]
        return [spec, np.zeros((1, 4, 2, st.SEGMENT), np.float32)]


class Ctx:
    """What a job gets from its worker."""

    def __init__(self, lib: Library, settings_file: Path, session: Any = None) -> None:
        self.library = lib
        self.settings = Settings(settings_file)
        self.session = session
        self.asked: list[str] = []
        self.fractions: list[float] = []

    def session_or_none(self, name: str) -> Any:
        self.asked.append(name)
        return self.session(name) if callable(self.session) else self.session

    def progress(self, fraction: float, _message: str | None = None) -> None:
        self.fractions.append(fraction)


def music(seconds: float, seed: int = 0) -> np.ndarray:
    """Stereo noise under a slow tremolo: every sample differs, nothing is silent."""
    rng = np.random.default_rng(seed)
    n = int(seconds * st.SAMPLE_RATE)
    gain = 0.3 + 0.1 * np.sin(np.arange(n) / 5_000)
    return ((rng.standard_normal((2, n)) * 0.2).clip(-1, 1) * gain).astype(np.float32)


def write_wav(path: Path, x: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(st.SAMPLE_RATE)
        w.writeframes((x.T.clip(-1, 1) * 32767).astype("<i2").tobytes())
    return path


def decode(path: Path) -> np.ndarray:
    pcm = subprocess.run([ffmpeg_path(), "-nostdin", "-v", "error", "-i", str(path), "-ar", str(st.SAMPLE_RATE), "-ac",
                          "2", "-f", "f32le", "-"], capture_output=True, check=True).stdout  # fmt: skip
    return np.frombuffer(pcm, np.float32).reshape(-1, 2).T


@pytest.fixture
def cache(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SEEDBOX_SUPPORT_DIR", str(tmp_path / "support"))
    return tmp_path / "cache"


@pytest.fixture
def lib(tmp_path):
    library = Library.open(tmp_path / "library.sqlite3")
    yield library
    library.close()


# --- the transforms and overlap-add ----------------------------------------------------------------------------------


def test_spectrogram_shape_and_round_trip():
    x = music(st.SEGMENT / st.SAMPLE_RATE)
    z = st.spectrogram(x)
    assert z.shape == (4, 2048, st.FRAMES) and z.dtype == np.float32 and st.FRAMES == 336
    y = st.inverse_spectrogram(z, st.SEGMENT)
    # away from the ends, which HTDemucs gives empty frames, only the dropped Nyquist bin is missing
    middle = (y - x)[:, st.NFFT * 2:-st.NFFT * 2]
    assert np.sqrt(np.mean(middle**2)) < 0.02 * np.sqrt(np.mean(x**2))
    many = st.inverse_spectrogram(np.stack((z, z * 0.5, z * 0.25, z * 0)), st.SEGMENT)
    assert many.shape == (4, 2, st.SEGMENT)
    np.testing.assert_allclose(many[1], y * 0.5, atol=1e-5)


def test_the_transforms_match_torch():
    torch = pytest.importorskip("torch")
    x = music(st.SEGMENT / st.SAMPLE_RATE)
    t = torch.from_numpy(x)
    # demucs' HTDemucs._spec and _magnitude
    hop, pad = st.HOP, st.HOP // 2 * 3
    padded = torch.nn.functional.pad(t, (pad, pad + st.FRAMES * hop - st.SEGMENT), mode="reflect")
    z = torch.stft(padded, st.NFFT, hop, window=torch.hann_window(st.NFFT), win_length=st.NFFT, normalized=True,
                   center=True, return_complex=True, pad_mode="reflect")[..., :-1, 2:2 + st.FRAMES]  # fmt: skip
    want = torch.view_as_real(z).permute(0, 3, 1, 2).reshape(4, 2048, st.FRAMES).numpy()
    np.testing.assert_allclose(st.spectrogram(x), want, atol=3e-5)
    # _ispec
    full = torch.nn.functional.pad(torch.nn.functional.pad(z, (0, 0, 0, 1)), (2, 2))
    back = torch.istft(full, st.NFFT, hop, window=torch.hann_window(st.NFFT), win_length=st.NFFT, normalized=True,
                       length=hop * st.FRAMES + 2 * pad, center=True)[..., pad:pad + st.SEGMENT].numpy()  # fmt: skip
    np.testing.assert_allclose(st.inverse_spectrogram(want, st.SEGMENT), back, atol=3e-5)


@pytest.mark.parametrize("length", [1_000, st.STRIDE, st.SEGMENT - 1, st.SEGMENT, st.SEGMENT + 1, st.STRIDE * 2,
                                    st.STRIDE * 3 + 7, st.SEGMENT + st.STRIDE - 1])  # fmt: skip
@pytest.mark.parametrize("chunk", [4_096, st.STRIDE])
def test_overlap_add_puts_back_every_sample_whatever_the_length(length, chunk):
    x = np.random.default_rng(length).standard_normal((2, length)).astype(np.float32)
    segments = []

    def run(seg: np.ndarray) -> np.ndarray:
        assert seg.shape == (2, st.SEGMENT)
        segments.append(seg)
        return SHARES[:, None, None] * seg

    parts = list(st.overlap_add((x[:, i:i + chunk] for i in range(0, length, chunk)), run))
    assert all(p.shape[-1] <= st.STRIDE for p in parts[:-1])
    out = np.concatenate(parts, axis=-1)
    assert out.shape == (4, 2, length)
    np.testing.assert_allclose(out.sum(axis=0), x, atol=2e-5)
    assert len(segments) == len(range(0, length, st.STRIDE))  # as demucs.apply.apply_model


def test_overlap_add_blends_the_segments_with_the_triangle():
    length = st.STRIDE + 1_000
    seen = iter((np.ones((4, 2, st.SEGMENT), np.float32), np.full((4, 2, st.SEGMENT), 3, np.float32)))
    out = np.concatenate(list(st.overlap_add(iter((np.zeros((2, length), np.float32),)), lambda _: next(seen))), axis=-1)
    assert (out[0, 0, :st.STRIDE] == 1).all() and out[0, 0, st.STRIDE] > 1  # the second segment comes in where it starts
    weight = st.blend_weight()
    i = st.STRIDE + 500
    assert out[0, 0, i] == pytest.approx((weight[i] + 3 * weight[500]) / (weight[i] + weight[500]))


def test_nothing_comes_out_of_no_audio():
    assert list(st.overlap_add(iter(()), lambda x: x)) == []


# --- the files -------------------------------------------------------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize("fmt, tolerance", [("flac", 2e-3), ("opus", 0.05)])
def test_separate_writes_four_stems_that_add_up_to_the_track(tmp_path, fmt, tolerance):
    x = music(10)
    session = FakeSession()
    seen: list[float] = []
    info = st.separate(write_wav(tmp_path / "a.wav", x), tmp_path / "out", session, fmt=fmt, progress=seen.append,
                       duration_hint=10)  # fmt: skip
    assert info == {"samples": x.shape[1], "seconds": 10.0} and session.calls == 2
    files = wf.stem_files(tmp_path / "out")
    assert files is not None and {f.suffix for f in files.values()} == {f".{fmt}"}
    decoded = {k: decode(f) for k, f in files.items()}
    n = min(d.shape[1] for d in decoded.values())
    if fmt == "flac":  # sample accurate; Opus is 48 kHz and lossy
        assert n == x.shape[1]
    total = sum(decoded[k][:, :n] for k in decoded)
    middle = (total - x[:, :n])[:, st.NFFT * 4:-st.NFFT * 4]
    assert np.sqrt(np.mean(middle**2)) < tolerance * 10
    assert np.mean(decoded["vocals"][:, :n] ** 2) > np.mean(decoded["drums"][:, :n] ** 2) * 10  # 0.4 against 0.1
    assert seen and seen == sorted(seen) and seen[-1] <= 0.99


@needs_ffmpeg
def test_a_cancelled_separation_stops_its_ffmpegs_and_says_so(tmp_path):
    x = music(20)

    def stop(_fraction: float) -> None:
        raise Cancelled

    with pytest.raises(Cancelled):
        st.separate(write_wav(tmp_path / "a.wav", x), tmp_path / "out", FakeSession(), progress=stop, duration_hint=20)


@needs_ffmpeg
def test_audio_that_cant_be_decoded_is_an_error(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises(ValueError, match="could not decode"):
        st.separate(bad, tmp_path / "out", FakeSession())


# --- the cache -------------------------------------------------------------------------------------------------------


def fake_stems(lib: Library, hash_: str, *, size: int = 1_000, age: float = 0.0, duration: float = 60.0) -> Path:
    lib.upsert_track(hash_, duration)
    folder = st.folder_for(hash_)
    folder.mkdir(parents=True)
    for s in st.STEMS:
        (folder / f"{s}.flac").write_bytes(b"x" * (size // 4))
    lib.put_artifact(hash_, "stems", "htdemucs:test", folder)
    os.utime(folder, (time.time() - age, time.time() - age))
    return folder


def test_settings_have_defaults_and_ignore_nonsense(tmp_path):
    s = Settings(tmp_path / "s.json")
    assert st.read_settings(s) == {"limit_gb": 20.0, "format": "flac", "low_power": False} == st.read_settings(None)
    s.set("stems", {"limit_gb": -3, "format": "mp3", "low_power": 1})
    assert st.read_settings(s) == {"limit_gb": 20.0, "format": "flac", "low_power": True}
    s.set("stems", {"limit_gb": 1.5, "format": "opus"})
    assert st.read_settings(s)["limit_gb"] == 1.5 and st.read_settings(s)["format"] == "opus"


def test_eviction_goes_by_last_use_and_spares_what_is_kept(cache, lib):
    old = fake_stems(lib, "aa11", age=300)
    older = fake_stems(lib, "bb22", age=400)
    newest = fake_stems(lib, "cc33", age=10)
    oldest_kept = fake_stems(lib, "dd44", age=500)
    assert st.usage() == {"tracks": 4, "bytes": 4_000}
    st.touch(old)  # used just now: the newest
    assert st.evict(lib, 3_000, keep=["dd44"]) == ["bb22"]
    assert not older.exists() and lib.artifacts("bb22", "stems") == [] and st.current(lib, "bb22") is None
    assert st.evict(lib, 3_000) == [] and st.usage()["tracks"] == 3
    assert st.evict(lib, 1_000, keep=["dd44"]) == ["cc33", "aa11"]
    assert not newest.exists() and oldest_kept.exists()
    assert st.evict(lib, 0, keep=["dd44"]) == [] and st.usage()["bytes"] == 1_000  # the set's own stems may overflow


# --- the job ---------------------------------------------------------------------------------------------------------


def add_track(lib: Library, path: Path, seconds: float = 9, seed: int = 0) -> str:
    return lib.add_file(write_wav(path, music(seconds, seed)))["hash"]


@needs_ffmpeg
def test_the_job_separates_stores_and_then_answers_from_the_cache(cache, lib, tmp_path):
    h = add_track(lib, tmp_path / "a.wav")
    ctx = Ctx(lib, tmp_path / "settings.json", FakeSession())
    r = st.stems_job({"hash": h}, ctx)
    assert r["hash"] == h and r["format"] == "flac" and r["model"] == f"htdemucs:{MODELS['htdemucs'].version}"
    assert r["bytes"] > 0 and r["realtime_factor"] > 0 and r["evicted"] == []
    assert ctx.fractions[-1] == 1.0
    folder = st.current(lib, h)
    assert folder == st.folder_for(h) and set(wf.stem_files(folder)) == set(st.STEMS)
    assert not [d for d in folder.parent.iterdir() if st.TMP_MARK in d.name]
    ctx.session = None  # no model needed for what is cached
    assert st.stems_job({"hash": h}, ctx)["cached"] is True


@needs_ffmpeg
def test_the_job_uses_the_low_power_model_when_set_and_falls_back(cache, lib, tmp_path):
    h = add_track(lib, tmp_path / "a.wav")
    Settings(tmp_path / "settings.json").set("stems", {"low_power": True})
    ctx = Ctx(lib, tmp_path / "settings.json", lambda name: FakeSession() if name == "htdemucs" else None)
    r = st.stems_job({"hash": h}, ctx)
    assert ctx.asked == ["htdemucs_int8", "htdemucs"] and r["model"].startswith("htdemucs:")


def test_without_a_model_the_job_leaves_the_track_for_later(cache, lib, tmp_path):
    lib.upsert_track("abcd", 60.0)
    assert st.stems_job({"hash": "abcd"}, Ctx(lib, tmp_path / "s.json"))["skipped"] == "model_missing"
    with pytest.raises(ValueError, match="no track"):
        st.stems_job({"hash": "nope"}, Ctx(lib, tmp_path / "s.json"))


@needs_ffmpeg
def test_the_background_stops_at_the_limit_but_an_open_set_evicts(cache, lib, tmp_path):
    settings = tmp_path / "settings.json"
    Settings(settings).set("stems", {"limit_gb": 2_000 / st.GB + 1_000_000 / st.GB})
    h = add_track(lib, tmp_path / "a.wav")
    other = add_track(lib, tmp_path / "b.wav", seed=1)
    old = fake_stems(lib, "aa11", size=1_000, age=500)
    in_set = fake_stems(lib, "bb22", size=1_000, age=1_000)
    # 9 s estimates at 2.7 MB: no room for the background
    assert st.stems_job({"hash": h}, Ctx(lib, settings, FakeSession()))["skipped"] == "cache_full"
    assert st.current(lib, h) is None and old.exists()
    set_id = lib.save_set("s", ["bb22", h])
    Settings(settings).set("stems", {"limit_gb": 2_000 / st.GB})
    r = st.stems_job({"hash": h, "set": set_id}, Ctx(lib, settings, FakeSession()))
    assert r["evicted"] == ["aa11"] and not old.exists()  # the oldest (bb22) belongs to the set
    assert in_set.exists() and st.current(lib, h) is not None and st.current(lib, other) is None


@needs_ffmpeg
def test_a_failed_job_leaves_nothing_behind_and_clears_older_leftovers(cache, lib, tmp_path):
    h = add_track(lib, tmp_path / "a.wav")
    leftover = st.folder_for(h).with_name(f"aa{st.TMP_MARK}123")
    leftover.mkdir(parents=True)

    class Broken(FakeSession):
        def run(self, *_: Any) -> list[np.ndarray]:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        st.stems_job({"hash": h}, Ctx(lib, tmp_path / "s.json", Broken()))
    assert list(st.folder_for(h).parent.iterdir()) == [] and st.current(lib, h) is None


def test_the_view_shows_only_stems_that_are_there_or_coming():
    from seedbox2.library.view import describe

    t = {k: None for k in ("hash", "duration", "title", "artist", "album", "source", "added_at")} | {"tags": {}}
    assert describe(t, jobs={"stems": {"state": "done", "progress": 1}})["status"]["stems"] is False  # skipped, evicted
    assert describe(t, jobs={"stems": {"state": "failed", "progress": 0}})["status"]["stems"] == "failed"
    assert describe(t, artifacts=["stems:x"], jobs={"stems": {"state": "done", "progress": 1}})["status"]["stems"] is True


# --- sets, the queue and events --------------------------------------------------------------------------------------


class FakeJobs:
    def __init__(self) -> None:
        self.submitted: list[tuple] = []
        self.bumped: list[list[str]] = []

    def submit(self, kind: str, payload: dict, priority: str = "library", front: bool = False) -> dict:
        self.submitted.append((kind, payload, priority))
        return {"id": str(len(self.submitted)), "state": "queued"}

    def bump(self, ids: Any = ()) -> list[str]:
        self.bumped.append(list(ids))
        return list(ids)


def test_an_open_set_queues_what_it_lacks_in_running_order_first(cache, lib):
    fake_stems(lib, "bb22")
    for h in ("cc33", "aa11"):
        lib.upsert_track(h, 60.0)
    set_id = lib.save_set("s", ["cc33", "bb22", "aa11"])
    jobs = FakeJobs()
    rows = st.submit_for_set(jobs, lib, set_id)
    assert [s[1]["hash"] for s in jobs.submitted] == ["cc33", "aa11"] and len(rows) == 2
    assert {s[2] for s in jobs.submitted} == {"set"} and jobs.bumped == [["1", "2"]]
    assert all(s[1]["set"] == set_id for s in jobs.submitted)


def test_set_status_counts_tracks_and_progress(cache, lib):
    fake_stems(lib, "bb22")
    lib.upsert_track("cc33", 60.0)
    set_id = lib.save_set("s", ["bb22", "cc33"])
    job = lib.add_job("stems", {"hash": "cc33"})
    lib.update_job(job["id"], state="running", progress=0.5)
    s = st.set_status(lib, set_id)
    assert (s["ready"], s["ready_tracks"], s["total"], s["progress"]) == (False, 1, 2, 0.75)
    assert s["tracks"] == [{"hash": "bb22", "stems": True, "progress": None},
                           {"hash": "cc33", "stems": "running", "progress": 0.5}]  # fmt: skip
    fake_stems(lib, "cc33")
    s = st.set_status(lib, set_id)
    assert s["ready"] is True and s["progress"] == 1.0
    assert st.set_status(lib, 999) is None
    assert st.set_status(lib, lib.save_set("empty", []))["ready"] is False


def test_finished_stems_queue_their_waveform_rows_and_report_on_their_sets(cache, lib):
    fake_stems(lib, "bb22")
    set_id = lib.save_set("s", ["bb22"])
    jobs, seen = FakeJobs(), []

    class Events:
        def publish(self, type_: str, data: Any, key: str | None = None, terminal: bool = False) -> None:
            seen.append((type_, data, key))

    done = {"kind": "stems", "state": "done", "payload": {"hash": "bb22"}, "result": {"path": "/x"}}
    st.on_job_finished(jobs, lib, Events(), done)
    assert jobs.submitted == [("waveform", {"hash": "bb22"}, "library")]
    assert seen == [("stems.set", {"set_id": set_id, "ready": True, "ready_tracks": 1, "total": 1, "progress": 1.0},
                     f"stems.set:{set_id}")]  # fmt: skip
    # the same waveform needn't be redone for stems that were there already, for a skip or for a failure
    for row in ({**done, "result": {"path": "/x", "cached": True}}, {**done, "result": {"skipped": "cache_full"}},
                {**done, "state": "failed", "result": None}, {"kind": "waveform", "state": "done", "payload": {"hash": "bb22"}}):  # fmt: skip
        st.on_job_finished(jobs, lib, None, row)
    assert len(jobs.submitted) == 1


def test_missing_stems_are_queued_as_far_as_the_cache_goes_and_only_with_a_model(cache, lib, tmp_path):
    from seedbox2.models import store

    fake_stems(lib, "aa11")
    lib.upsert_track("bb22", 100.0)
    lib.upsert_track("cc33", 100.0)
    lib.upsert_track("dd44", 100.0)
    settings = Settings(tmp_path / "s.json")
    settings.set("stems", {"limit_gb": 2.5 * 100 * st.ESTIMATE_BYTES_PER_S["flac"] / st.GB})
    jobs = FakeJobs()
    assert st.queue_missing(jobs, lib, settings) == 0 and jobs.submitted == []
    spec = MODELS["htdemucs"]
    path = store.model_path(spec, store.models_dir())
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    installed = store.installed
    store.installed = lambda s, root: s.name == "htdemucs"
    try:
        assert st.queue_missing(jobs, lib, settings) == 2
    finally:
        store.installed = installed
    assert [(s[1]["hash"], s[2]) for s in jobs.submitted] == [("bb22", "background"), ("cc33", "background")]


# --- the worker, the supervisor and the API --------------------------------------------------------------------------


def reporting(payload: dict, ctx: Any) -> dict:
    ctx.progress(0.5)
    time.sleep(0.1)
    return {"settings": type(ctx.settings).__name__}


def test_job_progress_says_which_track_and_listeners_hear_how_jobs_end(tmp_path):
    reg = Registry()
    reg.add("test_reporting", reporting, pool="heavy")
    events = EventBus()
    seen = []
    orig = events.publish

    def publish(type_: str, data: Any = None, **kw: Any) -> None:
        seen.append((type_, data))
        orig(type_, data, **kw)

    events.publish = publish  # type: ignore[method-assign]
    library = Library.open(tmp_path / "library.sqlite3")
    jobs = Supervisor(library, events, registry=reg)
    heard = []
    jobs.add_listener(lambda row: heard.append(row))
    jobs.add_listener(lambda row: 1 / 0)  # a broken listener costs nothing
    jobs.start()
    try:
        row = jobs.wait(jobs.submit("test_reporting", {"hash": "abc"})["id"], 30)
    finally:
        jobs.stop()
        library.close()
    assert row["state"] == "done" and row["result"] == {"settings": "Settings"}
    progress = [d for t, d in seen if t == "job.progress"]
    assert progress and progress[0]["hash"] == "abc" and progress[0]["kind"] == "test_reporting"
    assert [r["id"] for r in heard] == [row["id"]] and heard[0]["state"] == "done"


def test_low_memory_sessions_turn_the_planner_off():
    pytest.importorskip("onnxruntime")
    o = session_options(2, low_memory=True)
    assert (o.enable_mem_reuse, o.enable_mem_pattern, o.enable_cpu_mem_arena) == (False, False, False)
    o = session_options(2)
    assert o.enable_mem_reuse and o.enable_cpu_mem_arena
    assert MODELS["htdemucs"].low_memory and MODELS["htdemucs_int8"].low_memory and not MODELS["beat_this"].low_memory


@needs_ffmpeg
def test_the_api_reports_opens_serves_and_limits(cache, tmp_path):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    app = create_app(library_path=tmp_path / "support" / "library.sqlite3")
    with TestClient(app) as client:
        assert client.get("/api/analysis/stems").json() == {"limit_gb": 20.0, "format": "flac", "low_power": False,
                                                            "usage": {"tracks": 0, "bytes": 0}}  # fmt: skip
        lib = app.state.library  # opened by that request
        fake_stems(lib, "aa11", size=4_000, age=100)
        fake_stems(lib, "bb22", size=4_000, age=50)
        lib.upsert_track("cc33", 60.0)
        set_id = lib.save_set("s", ["aa11", "bb22"])
        status = client.get(f"/api/sets/{set_id}/stems").json()
        assert status["ready"] is True and status["total"] == 2
        assert client.get("/api/sets/999/stems").status_code == 404
        assert client.post("/api/sets/999/stems").status_code == 404
        assert client.post(f"/api/sets/{set_id}/stems").json()["ready"] is True  # nothing to queue, no workers started
        assert app.state.jobs is None or app.state.jobs.status()["queued"] == 0

        before = st.folder_for("aa11").stat().st_mtime
        r = client.get("/api/tracks/aa11/stems/vocals")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/flac" and len(r.content) == 1_000
        assert st.folder_for("aa11").stat().st_mtime > before  # counts as used
        assert client.get("/api/tracks/aa11/stems/kazoo").status_code == 404
        assert client.get("/api/tracks/cc33/stems/vocals").status_code == 404

        # bb22 is now the least recently used; a lower limit evicts at once
        r = client.post("/api/analysis/stems/settings", json={"limit_gb": 4_000 / st.GB, "format": "opus"}).json()
        assert r["usage"] == {"tracks": 1, "bytes": 4_000} and r["format"] == "opus" and r["low_power"] is False
        assert st.current(lib, "bb22") is None and st.current(lib, "aa11") is not None
        assert client.post("/api/analysis/stems/settings", json={"format": "mp3"}).status_code == 422
        assert json.loads((tmp_path / "support" / "settings.json").read_text())["stems"]["format"] == "opus"


@pytest.mark.models
@needs_ffmpeg
def test_the_real_model_separates_a_bass_tone_from_a_click_track(tmp_path):
    from seedbox2.models import load_session, store

    store.download(MODELS["htdemucs"], store.models_dir())
    t = np.arange(10 * st.SAMPLE_RATE) / st.SAMPLE_RATE
    bass = 0.3 * np.sin(2 * np.pi * 55 * t)
    kicks = 0.6 * np.exp(-((t * 2) % 1) * 40) * np.sin(2 * np.pi * 110 * t * np.exp(-((t * 2) % 1) * 3))
    x = np.stack((bass + kicks, bass + kicks)).astype(np.float32)
    info = st.separate(write_wav(tmp_path / "a.wav", x), tmp_path / "out", load_session("htdemucs", pool="heavy"))
    assert info["samples"] == x.shape[1]
    out = {k: decode(f) for k, f in wf.stem_files(tmp_path / "out").items()}
    power = {k: float(np.mean(v**2)) for k, v in out.items()}
    assert power["bass"] > power["vocals"] * 10 and power["drums"] > power["vocals"] * 10, power
    residual = sum(out.values())[:, :x.shape[1]] - x
    assert np.mean(residual**2) < 0.01 * np.mean(x**2)  # the stems add up to the mix, within -20 dB
