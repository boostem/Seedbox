"""A crate from tunebat in Seedbox 2: the seed with every recommendation, the related ones marked, and the download of
the ticked ones into a crate folder that is then imported."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from musicreco.models import DownloadResult, Track, TrackDetail
from seedbox2.library.tunebat_crate import TunebatCrates, UnknownCrate
from seedbox2.library.tunebat_try import TunebatTry
from seedbox2.server.app import create_app


def track(id_, name, camelot="8A", bpm=124.0, artists=("Artist",)):
    return Track(id=id_, name=name, artists=list(artists), album="Album", release_date="2020", duration_ms=200_000,
                 popularity=50, key="A minor", camelot=camelot, bpm=bpm, energy=0.8, danceability=0.7, cover_url=None)  # fmt: skip


SEED = track("seed", "Seed Song")
RELATED = track("near", "Near", camelot="9A", bpm=126.0)  # +1 on the wheel, 1.6 % faster
CLASH = track("far", "Far", camelot="3B", bpm=124.0)  # a clashing key
SLOW = track("slow", "Slow", camelot="8A", bpm=90.0)  # the same key, far too slow


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def get_track(self, track_id):
        self.calls += 1
        if self.error:
            raise self.error
        return TrackDetail(seed=SEED, recommended=[CLASH, RELATED, SEED, SLOW, RELATED])


class Music:
    def __init__(self, root, rows=()):
        self.music_dir = root
        self.imported = []
        self.lib = SimpleNamespace(view_rows=lambda: list(rows))

    def import_files(self, paths):
        self.imported.append(paths)


def settings():
    return SimpleNamespace(bpm_tolerance=0.10, workers=2, tolerance_s=15.0)


@pytest.fixture
def ffmpeg(monkeypatch):
    from musicreco import paths as binaries

    monkeypatch.setattr(binaries, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")


def make(tmp_path, rows=(), run_batch=None, complete_batch=None, client=None):
    events = []
    tunebat = TunebatTry(None, lambda *_: None, client=client or FakeClient())
    tunebat._last_call = -10.0
    crates = TunebatCrates(tunebat, Music(tmp_path / "music", rows), lambda kind, data: events.append((kind, data)),
                           run_batch=run_batch, complete_batch=complete_batch or (lambda *a, **k: None),
                           settings=settings)  # fmt: skip
    return crates, events


def wait_for(events, state):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(d["state"] == state for _, d in events):
            return events[-1][1]
        time.sleep(0.01)
    raise AssertionError(f"no {state}: {events}")


def test_open_ranks_and_marks_the_related_ones(tmp_path):
    crates, _ = make(tmp_path, rows=[{"title": "Slow (Extended Mix)", "artist": "Artist"}])
    crate = crates.open("seed")
    assert crate["seed"]["id"] == "seed" and crate["folder"] == "Artist - Seed Song"
    ids = [r["track"]["id"] for r in crate["recs"]]
    assert sorted(ids) == ["far", "near", "slow"]  # no seed, no repeats
    assert ids[0] == "near"  # the only one that passes ranks first
    by_id = {r["track"]["id"]: r for r in crate["recs"]}
    assert by_id["near"]["related"] and not by_id["far"]["related"] and not by_id["slow"]["related"]
    assert by_id["slow"]["in_library"] and not by_id["near"]["in_library"]
    assert by_id["far"]["move"] == "clash"
    crates.open("seed")
    assert crates.tunebat._client.calls == 1  # asked once per run


def test_tunebat_down_is_a_502(tmp_path, monkeypatch):
    from musicreco.tunebat import TunebatError

    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(tmp_path / "music"))
    app = create_app(library_path=tmp_path / "library.sqlite3", models_dir=tmp_path / "models")
    with TestClient(app) as c:
        crates, _ = make(tmp_path, client=FakeClient(TunebatError("HTTP 403")))
        app.state.tunebat_crates = crates
        r = c.get("/api/tunebat/crates/seed")
        assert r.status_code == 502 and "try again" in r.json()["detail"]
        assert c.post("/api/tunebat/crates/other/download", json={"ids": ["x"]}).status_code == 404
        assert c.post("/api/tunebat/crates/downloads/nope/cancel").status_code == 404


def test_download_needs_the_crate_and_its_tracks(tmp_path):
    crates, _ = make(tmp_path)
    with pytest.raises(UnknownCrate):
        crates.download("seed", ["near"])
    crates.open("seed")
    with pytest.raises(UnknownCrate):
        crates.download("seed", ["near", "stranger"])
    with pytest.raises(ValueError):
        crates.download("seed", [])


def test_download_runs_the_batch_into_the_crate_folder_and_imports_it(tmp_path, ffmpeg):
    calls = {}

    def run_batch(tracks, seed, seed_dir, *, workers, on_update, tolerance_s, cancel):
        calls["batch"] = ([t.id for t in tracks], seed_dir, workers, tolerance_s)
        for t in tracks:
            on_update(t, "downloading")
        return [DownloadResult(track=t, status="downloaded" if t.id != "far" else "skipped", path=seed_dir / f"{t.id}.mp3",
                               reason=None if t.id != "far" else "no match") for t in tracks]  # fmt: skip

    def complete_batch(seed, recs, scores, ticked, seed_dir, results, *, bpm_tolerance):
        calls["complete"] = (seed.id, sorted(t.id for t in recs), sorted(ticked), bpm_tolerance)

    crates, events = make(tmp_path, run_batch=run_batch, complete_batch=complete_batch)
    crates.open("seed")
    job = crates.download("seed", ["far", "near", "seed"])
    assert [i["id"] for i in job["items"]] == ["seed", "near", "far"]  # the seed first, then best first
    done = wait_for(events, "done")
    folder = tmp_path / "music" / "Artist - Seed Song"
    assert calls["batch"] == (["seed", "near", "far"], folder, 2, 15.0)
    assert calls["complete"] == ("seed", ["far", "near", "slow"], ["far", "near", "seed"], 0.10)
    assert crates.music.imported == [[folder]]
    assert {i["id"]: i["status"] for i in done["items"]} == {"seed": "downloaded", "near": "downloaded", "far": "skipped"}
    assert done["path"] == str(folder)
    assert all(kind == "tunebat.crate" for kind, _ in events)
    assert crates.jobs()[0]["id"] == job["id"]


def test_a_failed_batch_says_so(tmp_path, ffmpeg):
    def run_batch(*_a, **_k):
        raise OSError("disk full")

    crates, events = make(tmp_path, run_batch=run_batch)
    crates.open("seed")
    crates.download("seed", ["seed"])
    assert "disk full" in wait_for(events, "failed")["error"]
    assert crates.music.imported == []


def test_api_opens_and_downloads(tmp_path, monkeypatch, ffmpeg):
    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(tmp_path / "music"))

    def run_batch(tracks, seed, seed_dir, **_k):
        return [DownloadResult(track=t, status="downloaded") for t in tracks]

    app = create_app(library_path=tmp_path / "library.sqlite3", models_dir=tmp_path / "models")
    with TestClient(app) as c:
        crates, events = make(tmp_path, run_batch=run_batch)
        app.state.tunebat_crates = crates
        crate = c.get("/api/tunebat/crates/seed").json()
        assert crate["recs"][0]["related"] is True
        r = c.post("/api/tunebat/crates/seed/download", json={"ids": ["seed", "near"]})
        assert r.status_code == 202 and r.json()["state"] == "queued"
        wait_for(events, "done")
        assert c.get("/api/tunebat/crates/downloads").json()[0]["state"] == "done"
