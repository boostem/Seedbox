"""A crate from tunebat in Seedbox 2: the seed with every recommendation, the related ones marked, and the download of
the ticked ones into a crate folder that is then imported."""

from __future__ import annotations

import shutil
import time
from types import SimpleNamespace

import pytest
from mutagen.id3 import ID3
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

    def import_now(self, paths):
        self.imported.append(("now", paths))


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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_each_track_is_in_the_library_and_queued_as_soon_as_it_downloads(tmp_path, ffmpeg):
    """A batch of 3 where the first finishes first: it has its row and its analyses queued while the other two are
    still downloading; after the batch, one row per track and the crate and set hold all three in order."""
    from musicreco import pipeline
    from seedbox2.jobs.registry import Registry
    from seedbox2.library import Library
    from seedbox2.library.importer import queue_analysis
    from seedbox2.library.service import LocalMusic, Settings
    from tests.test_seedbox2_library import _mp3

    class Jobs:  # the supervisor's submit, recording each (kind, hash)
        def __init__(self):
            self.registry = Registry()
            self.registry.add("beats", "x:y", pool="light", per_track=True)
            self.registry.add("stems", "x:y", pool="heavy", per_track=True)
            self.submitted = []

        def submit(self, kind, payload, *, priority):
            self.submitted.append((kind, payload["hash"]))
            return {"kind": kind}

    lib, jobs = Library.open(tmp_path / "library.sqlite3"), Jobs()
    root = tmp_path / "music"
    music = LocalMusic(lib, settings=Settings(tmp_path / "settings.json"), music_dir=root,
                       on_new=lambda hashes: queue_analysis(jobs, hashes))  # fmt: skip
    seen_mid_batch = {}

    def run_batch(tracks, seed, seed_dir, *, workers, on_update, tolerance_s, cancel):
        results = []
        for n, t in enumerate(tracks):
            path = _mp3(seed_dir / f"{t.id}.mp3", freq=300 + 100 * n, tags={"title": t.name, "artist": "Artist",
                                                                             "spotify_id": t.id})  # fmt: skip
            on_update(t, "downloaded")
            results.append(DownloadResult(track=t, status="downloaded", path=path))
            if n == 0:  # tracks 2 and 3 are still to come: wait for track 1's import
                deadline = time.monotonic() + 20
                while not lib.tracks() and time.monotonic() < deadline:
                    time.sleep(0.02)
                seen_mid_batch["rows"] = [r["title"] for r in lib.tracks()]
                seen_mid_batch["jobs"] = list(jobs.submitted)
        return results

    tunebat = TunebatTry(None, lambda *_: None, client=FakeClient())
    tunebat._last_call = -10.0
    events = []
    crates = TunebatCrates(tunebat, music, lambda kind, data: events.append((kind, data)), run_batch=run_batch,
                           complete_batch=pipeline.complete_batch, settings=settings)  # fmt: skip
    crates.open("seed")
    crates.download("seed", ["seed", "near", "far"])
    done = wait_for(events, "done")
    assert music.wait_idle(30)

    assert seen_mid_batch["rows"] == ["Seed Song"]  # track 1 has its row before tracks 2 and 3 are there
    first = lib.tracks(search="Seed Song")[0]["hash"]
    assert sorted(seen_mid_batch["jobs"]) == [("beats", first), ("stems", first)]  # and its analyses are queued
    rows = lib.tracks()
    assert sorted(r["title"] for r in rows) == ["Far", "Near", "Seed Song"]  # one row per track
    assert {r["source"] for r in rows} == {"seedbox1"}  # the folder import attached tunebat's crate data
    hashes = {r["hash"] for r in rows}
    by_title = {r["title"]: r["hash"] for r in rows}  # each item says which track it became, to follow its analysis
    assert {i["name"]: i["hash"] for i in done["items"]} == {"Seed Song": by_title["Seed Song"], "Near": by_title["Near"],
                                                             "Far": by_title["Far"]}  # fmt: skip
    assert sorted(jobs.submitted) == sorted((k, h) for h in hashes for k in ("beats", "stems"))  # queued once each
    folder = root / "Artist - Seed Song"
    numbered = sorted(folder.glob("*.mp3"), key=lambda f: int(str(ID3(f)["TRCK"]).split("/")[0]))
    by_number = [lib.hash_file(f).hash for f in numbered]  # complete_batch's set order, in the files' tags
    (crate,) = lib.crates()
    assert crate["name"] == "Artist - Seed Song" and crate["track_count"] == 3
    assert [t["hash"] for t in lib.crate(crate["id"])["tracks"]] == by_number
    (set_,) = lib.sets()
    assert [t["hash"] for t in lib.set(set_["id"])["tracks"]] == by_number
    crates.close()
    lib.close()
