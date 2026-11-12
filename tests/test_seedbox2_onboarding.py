"""#80: when the first-run flow shows, that models wait for it, and its tunebat search and one-track download."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from seedbox2.models import manager as manager_mod
from seedbox2.server.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(tmp_path / "music"))
    app = create_app(library_path=tmp_path / "library.sqlite3", models_dir=tmp_path / "models")
    with TestClient(app) as c:
        yield c


def test_first_run_until_setup_is_finished(client, tmp_path):
    assert client.get("/api/onboarding").json() == {"first_run": True}
    assert client.post("/api/onboarding", json={"done": True}).json() == {"first_run": False}
    assert client.get("/api/onboarding").json() == {"first_run": False}
    assert client.post("/api/onboarding", json={"done": False}).json() == {"first_run": True}


def test_no_first_run_with_tracks_in_the_library(client):
    lib = client.app.state.library
    if lib is None:
        client.get("/api/library")
        lib = client.app.state.library
    lib.upsert_track("a" * 40, title="Already here", artist="Someone", duration=10.0)
    assert client.get("/api/onboarding").json() == {"first_run": False}


@pytest.mark.parametrize("onboarded", [False, True])
def test_models_wait_for_setup(tmp_path, monkeypatch, onboarded):
    started = threading.Event()
    monkeypatch.setattr(manager_mod.ModelManager, "start", lambda self: started.set())
    if onboarded:
        (tmp_path / "settings.json").write_text('{"onboarded": true}')
    app = create_app(library_path=tmp_path / "library.sqlite3", models_dir=tmp_path / "models", download_models=True)
    with TestClient(app):
        assert started.wait(1.0 if not onboarded else 10.0) is onboarded


# --- tunebat -----------------------------------------------------------------------------------------------------------


@dataclass
class FakeTrack:
    id: str = "t1"
    name: str = "Song"
    artists: list = field(default_factory=lambda: ["Artist"])
    album: str = "Album"
    duration_ms: int = 200_000
    bpm: float = 124.0
    camelot: str = "8A"
    cover_url: str | None = None


@dataclass
class FakePage:
    items: list
    total_count: int = 1


class FakeClient:
    def __init__(self, error=None):
        self.error = error

    def search(self, term):
        if self.error:
            raise self.error
        return FakePage([FakeTrack()])


@pytest.fixture
def tunebat(tmp_path):
    from seedbox2.library.tunebat_try import TunebatTry

    class Music:
        imported: list = []

        def upload_path(self, name):
            return tmp_path / name

        def import_files(self, paths):
            self.imported.append(paths)

        def import_now(self, paths):
            self.imported.append(("now", paths))

    events = []
    return TunebatTry(Music(), lambda kind, data: events.append((kind, data)), client=FakeClient()), events


def wait_for(events, state):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(d["state"] == state for _, d in events):
            return
        time.sleep(0.01)
    raise AssertionError(f"no {state}: {events}")


def test_search_returns_rows(tunebat):
    try_, _ = tunebat
    result = try_.search("song")
    assert result["total"] == 1
    assert result["items"][0] == {"id": "t1", "name": "Song", "artists": ["Artist"], "album": "Album", "duration": 200.0,
                                  "bpm": 124.0, "camelot": "8A", "cover_url": None}  # fmt: skip


def test_search_failure_says_so(client, monkeypatch):
    from musicreco.tunebat import TunebatError
    from seedbox2.library.tunebat_try import TunebatTry

    client.app.state.tunebat = TunebatTry(None, lambda *_: None, client=FakeClient(TunebatError("HTTP 403")))
    r = client.get("/api/tunebat/search", params={"q": "song"})
    assert r.status_code == 502
    assert "try again" in r.json()["detail"]
    assert client.get("/api/tunebat/search", params={"q": "  "}).status_code == 422


def test_download_needs_a_search_first(client):
    from seedbox2.library.tunebat_try import TunebatTry

    client.app.state.tunebat = TunebatTry(None, lambda *_: None, client=FakeClient())
    assert client.post("/api/tunebat/download", json={"id": "nope"}).status_code == 404


def test_download_tags_and_imports(tunebat, tmp_path, monkeypatch):
    from mutagen.id3 import ID3

    from musicreco import paths as binaries
    from musicreco import youtube
    from musicreco.models import MatchResult

    try_, events = tunebat
    try_.search("song")
    monkeypatch.setattr(binaries, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(youtube, "find_match", lambda t: MatchResult(track=t, match=object(), closest=None, reason=None))

    def fake_download(_candidate, dest):
        dest.write_bytes(b"\xff\xfb\x90\x00" * 64)
        return dest

    monkeypatch.setattr(youtube, "download", fake_download)
    assert try_.download("t1")["state"] == "queued"
    wait_for(events, "done")
    assert [d["state"] for _, d in events] == ["finding", "downloading", "done"]
    path = tmp_path / "Artist - Song.mp3"
    assert try_.music.imported[-2:] == [("now", [path]), [path]]  # at once, then the queued import that says so
    tags = ID3(path)
    assert str(tags["TIT2"]) == "Song" and str(tags["TPE1"]) == "Artist" and str(tags["TKEY"]) == "8A"
    assert try_.download("t1")["state"] == "done"  # not fetched twice


@pytest.mark.parametrize("ffmpeg", [None, "/usr/bin/ffmpeg"])
def test_download_failures_say_what_to_do(tunebat, monkeypatch, ffmpeg):
    from musicreco import paths as binaries
    from musicreco import youtube
    from musicreco.models import MatchResult

    try_, events = tunebat
    try_.search("song")
    monkeypatch.setattr(binaries, "ffmpeg_path", lambda: ffmpeg)
    monkeypatch.setattr(youtube, "find_match", lambda t: MatchResult(track=t, match=None, closest=None, reason="no hit"))
    try_.download("t1")
    wait_for(events, "failed")
    error = events[-1][1]["error"]
    assert ("ffmpeg" in error) if ffmpeg is None else ("Try another track" in error)
    # a failed one can be tried again
    assert try_.download("t1")["state"] == "queued"
