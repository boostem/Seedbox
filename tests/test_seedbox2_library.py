"""seedbox2.library: schema migrations, the content hash, the Seedbox 1 import, the repository and concurrency."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from mutagen.id3 import APIC, ID3, TIT2, TPE1, TRCK, TXXX, ID3NoHeaderError

from seedbox2.library import Database, Library, db, hashing
from seedbox2.library.import_seedbox1 import import_seedbox1
from seedbox2.server import start_server

ROOT = Path(__file__).resolve().parent.parent
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

TABLES = {"tracks", "track_paths", "analyses", "artifacts", "crates", "crate_tracks", "sets", "set_items", "plans",
          "jobs", "ratings"}  # fmt: skip


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


# --- schema and migrations ----------------------------------------------------------------------------------------


def test_fresh_database_has_the_latest_schema_in_wal_mode(tmp_path):
    with Database(tmp_path / "lib.sqlite3") as d:
        c = d.read()
        assert db.schema_version(c) == db.SCHEMA_VERSION
        assert TABLES <= _tables(c)
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrations_upgrade_an_older_file_and_keep_its_data(tmp_path):
    path = tmp_path / "lib.sqlite3"
    with Database(path, db.MIGRATIONS) as d, d.write() as c:
        c.execute("INSERT INTO tracks (hash, duration, title) VALUES ('h1', 60, 'Kept')")

    def backfill(conn: sqlite3.Connection) -> None:  # a Python migration step, next to an SQL one
        conn.execute("UPDATE tracks SET rating = 3")

    newer = [*db.MIGRATIONS, "ALTER TABLE tracks ADD COLUMN rating INTEGER;", backfill]
    with Database(path, newer) as d:
        c = d.read()
        assert db.schema_version(c) == len(newer)
        assert "rating" in _columns(c, "tracks")
        assert dict(c.execute("SELECT title, rating FROM tracks").fetchone()) == {"title": "Kept", "rating": 3}
    with Database(path, newer) as d:  # reopening is a no-op
        assert db.schema_version(d.read()) == len(newer)


def test_a_failing_migration_leaves_the_file_as_it_was(tmp_path):
    path = tmp_path / "lib.sqlite3"
    Database(path).close()
    broken = [*db.MIGRATIONS, "CREATE TABLE half (x INTEGER);\nINSERT INTO nowhere VALUES (1);"]
    with pytest.raises(sqlite3.OperationalError):
        Database(path, broken)
    with Database(path) as d:
        assert db.schema_version(d.read()) == db.SCHEMA_VERSION
        assert "half" not in _tables(d.read())


def test_a_file_from_a_newer_seedbox2_is_refused(tmp_path):
    path = tmp_path / "lib.sqlite3"
    Database(path, [*db.MIGRATIONS, "CREATE TABLE later (x);"]).close()
    with pytest.raises(db.SchemaTooNew):
        Database(path)


def test_writes_roll_back_on_error(tmp_path):
    with Database(tmp_path / "lib.sqlite3") as d:
        with pytest.raises(RuntimeError), d.write() as c:
            c.execute("INSERT INTO tracks (hash, duration) VALUES ('h', 1)")
            raise RuntimeError
        assert d.read().execute("SELECT count(*) FROM tracks").fetchone()[0] == 0


# --- content hash -------------------------------------------------------------------------------------------------


def _mp3(path: Path, freq: int = 440, seconds: float = 4, *, tags: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}", "-ac", "2", "-b:a", "128k", str(path)], check=True)  # fmt: skip
    if tags:
        _tag(path, **tags)
    return path


def _tag(path: Path, **tags: str) -> None:
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    frames = {"title": TIT2, "artist": TPE1, "trck": TRCK}
    for k, v in tags.items():
        if k == "spotify_id":
            id3.add(TXXX(desc="SPOTIFY_TRACK_ID", text=[v]))
        elif k == "cover":
            id3.add(APIC(mime="image/jpeg", type=3, desc="cover", data=v.encode() * 4096))
        else:
            id3.add(frames[k](text=[v]))
    id3.save(path)


@needs_ffmpeg
def test_content_hash_ignores_tags_and_survives_renames(tmp_path):
    original = _mp3(tmp_path / "a.mp3", seconds=70)  # longer than both 30 s edges
    first = hashing.content_hash(original)
    assert re.fullmatch(r"[0-9a-f]{40}", first.hash) and first.duration == pytest.approx(70, abs=0.1)

    copy = tmp_path / "moved" / "Renamed Copy.mp3"
    copy.parent.mkdir()
    shutil.copy(original, copy)
    _tag(copy, title="New Title " * 50, artist="Someone", trck="3/12", spotify_id="abc", cover="x")
    assert copy.read_bytes() != original.read_bytes()
    assert hashing.content_hash(copy) == first

    _tag(copy, title="Retagged again", trck="7/12")
    assert hashing.content_hash(copy) == first

    other = _mp3(tmp_path / "b.mp3", freq=441, seconds=70)
    assert hashing.content_hash(other).hash != first.hash
    longer = _mp3(tmp_path / "c.mp3", seconds=71)  # same edges, different length
    assert hashing.content_hash(longer).hash != first.hash


@needs_ffmpeg
def test_content_hash_rejects_non_audio(tmp_path):
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio" * 100)
    with pytest.raises(hashing.HashError):
        hashing.content_hash(junk)


@needs_ffmpeg
def test_library_reuses_hashes_until_the_file_changes(tmp_path):
    calls = []

    def counting(path: Path) -> hashing.ContentHash:
        calls.append(path)
        return hashing.content_hash(path)

    lib = Library(Database(tmp_path / "lib.sqlite3"), hasher=counting)
    song = _mp3(tmp_path / "music" / "song.mp3")
    track = lib.add_file(song, title="Song")
    assert lib.add_file(song)["hash"] == track["hash"] and len(calls) == 1  # unchanged: no decode

    _tag(song, title="Retitled")
    os.utime(song, ns=(time.time_ns(), time.time_ns() + 10**9))
    renamed = song.with_name("renamed.mp3")
    song.rename(renamed)
    again = lib.add_file(renamed)
    assert again["hash"] == track["hash"] and len(calls) == 2
    assert again["title"] == "Song" and str(renamed) in again["paths"]
    lib.forget_path(song)
    assert lib.track(track["hash"])["paths"] == [str(renamed)]
    lib.close()


# --- Seedbox 1 import ---------------------------------------------------------------------------------------------


def _seedbox1_crates(root: Path) -> Path:
    """Two Seedbox 1 crates: tracks numbered out of name order, crate.json, an edited plan and a stray plan."""
    crate = root / "Artist - Seed"
    _mp3(crate / "Artist - Seed.mp3", 440, tags={"title": "Seed", "artist": "Artist", "trck": "2/3", "spotify_id": "S1"})
    _mp3(crate / "B - Two.mp3", 550, tags={"title": "Two", "artist": "B", "trck": "1/3", "spotify_id": "S2"})
    _mp3(crate / "C - Three.mp3", 660, tags={"title": "Three", "artist": "C", "trck": "3/3", "spotify_id": "S3"})
    meta = crate / ".seedbox"
    (meta / "mixplans").mkdir(parents=True)
    raw = {"id": "S1", "n": "Seed", "as": ["Artist"], "an": "LP", "rd": "2020", "d": 4000, "p": 50, "k": "A minor",
           "c": "8A", "b": 124.0, "e": 0.8, "da": 0.7}  # fmt: skip
    (meta / "crate.json").write_text(json.dumps({"version": 1, "seed": raw, "recs": [], "earlier": []}))
    (meta / "mixplans" / "S2__S1.json").write_text(json.dumps({"from_id": "S2", "to_id": "S1", "edited": True}))
    (meta / "mixplans" / "S2__GONE.json").write_text(json.dumps({"from_id": "S2", "to_id": "GONE"}))
    other = root / "Other - Crate"
    shutil.copy(crate / "B - Two.mp3", _mp3(other / "x.mp3", 770, tags={"spotify_id": "S4", "trck": "1/2"}).parent
                / "Two again.mp3")  # the same audio in two crates: one track  # fmt: skip
    (root / ".hidden").mkdir()
    return root


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


@needs_ffmpeg
def test_import_seedbox1_reads_crates_sets_and_edited_plans_without_touching_them(tmp_path):
    root = _seedbox1_crates(tmp_path / "MusicReco")
    before = _snapshot(root)
    os.chmod(root / "Artist - Seed", 0o555)  # read-only: any write would fail loudly
    try:
        lib = Library.open(tmp_path / "data" / "lib.sqlite3")
        progress = []
        report = import_seedbox1(lib, root, lambda *a: progress.append(a))
    finally:
        os.chmod(root / "Artist - Seed", 0o755)
    assert _snapshot(root) == before
    assert report.crates == 2 and report.sets == 2 and report.tracks == 5 and report.plans == 1
    assert len(report.errors) == 1 and "S2__GONE.json" in report.errors[0]
    assert [p[:2] for p in progress] == [(1, 2), (2, 2)]
    assert lib.track_count() == 4  # "Two again.mp3" is "B - Two.mp3"'s audio

    two = lib.track_by_source("seedbox1", "S2")
    assert len(two["paths"]) == 2
    seed = lib.track_by_source("seedbox1", "S1")
    assert seed["tags"]["tunebat"]["camelot"] == "8A" and seed["artist"] == "Artist"

    crate = next(c for c in lib.crates() if c["name"] == "Artist - Seed")
    assert crate["seed_hash"] == seed["hash"] and crate["track_count"] == 3
    s = next(s for s in lib.sets() if s["crate_id"] == crate["id"])
    assert [t["title"] for t in lib.set(s["id"])["tracks"]] == ["Two", "Seed", "Three"]  # TRCK order

    plan = lib.plan(two["hash"], seed["hash"])
    assert plan["user_edited"] is True and plan["plan"]["from_id"] == "S2" and plan["source"] == "seedbox1"

    # importing again updates in place, and keeps a plan edited in Seedbox 2 since
    lib.put_plan(two["hash"], seed["hash"], {"mine": True}, user_edited=True)
    again = import_seedbox1(lib, root)
    assert again.plans == 0 and again.plans_kept == 1
    assert lib.plan(two["hash"], seed["hash"])["plan"] == {"mine": True}
    assert len(lib.crates()) == 2 and len(lib.sets()) == 2 and lib.track_count() == 4
    lib.close()


# --- repository through the routers -------------------------------------------------------------------------------


def test_routers_have_no_sql():
    for path in (ROOT / "seedbox2" / "server").rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\s|import sqlite3|\.execute\(", text), path


class _Client:
    def __init__(self, url: str) -> None:
        self.url = url

    def request(self, method: str, path: str, body: object = None, **params: object) -> tuple[int, object]:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path + query, data=data, method=method,
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path: str, **params: object) -> object:
        status, body = self.request("GET", path, **params)
        assert status == 200, (path, status, body)
        return body


@needs_ffmpeg
def test_library_sets_and_plans_over_http(tmp_path):
    root = _seedbox1_crates(tmp_path / "MusicReco")
    handle = start_server(library_path=tmp_path / "data" / "lib.sqlite3")
    client = _Client(handle.url)
    try:
        assert client.get("/api/library")["tracks"] == 0
        status, body = client.request("POST", "/api/library/import/seedbox1", {"root": str(root)})
        assert status == 202 and body["state"] == "running"
        for _ in range(200):
            body = client.get("/api/library/import/seedbox1")
            if body["state"] != "running":
                break
            time.sleep(0.05)
        assert body["state"] == "done" and body["crates"] == 2, body

        page = client.get("/api/library/tracks", search="thr")
        assert page["total"] == 4 and [t["title"] for t in page["items"]] == ["Three"]
        h = page["items"][0]["hash"]
        assert client.get(f"/api/library/tracks/{h}")["paths"][0].endswith("C - Three.mp3")
        assert client.request("GET", "/api/library/tracks/nope")[0] == 404
        assert client.request("POST", "/api/library/import/seedbox1", {"root": str(tmp_path / "nope")})[0] == 422

        crates = client.get("/api/library/crates")
        assert {c["name"] for c in crates} == {"Artist - Seed", "Other - Crate"}
        assert len(client.get(f"/api/library/crates/{crates[0]['id']}")["tracks"]) == crates[0]["track_count"]
        sets = client.get("/api/sets")
        one = client.get(f"/api/sets/{sets[0]['id']}")
        assert [t["position"] for t in one["tracks"]] == list(range(len(one["tracks"])))
        assert client.request("GET", "/api/sets/999")[0] == 404

        plans = client.get("/api/mixes/plans", edited="true")
        assert len(plans) == 1 and "plan" not in plans[0]
        full = client.get(f"/api/mixes/plans/{plans[0]['from_hash']}/{plans[0]['to_hash']}")
        assert full["plan"]["to_id"] == "S1"
    finally:
        handle.stop()
    assert handle.app.state.library is None  # closed on shutdown


def test_repository_round_trips(tmp_path):
    with Database(tmp_path / "lib.sqlite3") as d:
        lib = Library(d)
        lib.upsert_track("a", 100, title="A", tags={"bpm": 120})
        lib.upsert_track("a", 100, tags={"key": "8A"})
        lib.upsert_track("b", 90, title="B_%")
        assert lib.track("a")["tags"] == {"bpm": 120, "key": "8A"}
        assert [t["hash"] for t in lib.tracks(search="_%")] == ["b"]  # LIKE wildcards are literal

        lib.put_analysis("a", "beats", "1", {"bpm": 120})
        lib.put_analysis("a", "beats", "2", {"bpm": 121})
        assert lib.analysis("a", "beats")["data"] == {"bpm": 121}
        assert lib.analysis("a", "beats", "1")["data"] == {"bpm": 120}
        lib.put_artifact("a", "stems", "htdemucs-1", "/cache/stems/a")
        assert lib.artifacts("a", "stems")[0]["path"] == "/cache/stems/a"
        with pytest.raises(ValueError):
            lib.put_artifact("a", "video", "1", "/x")

        set_id = lib.save_set("Friday", ["a", "b", "a"])
        assert [t["hash"] for t in lib.set(set_id)["tracks"]] == ["a", "b", "a"]
        lib.save_set("Friday v2", ["b"], set_id=set_id)
        assert lib.set(set_id)["name"] == "Friday v2" and len(lib.set(set_id)["tracks"]) == 1

        low = lib.add_job("waveform", {"hash": "a"})
        high = lib.add_job("stems", {"hash": "a"}, priority=5)
        assert lib.claim_job()["id"] == high["id"]
        assert lib.claim_job(["stems"]) is None
        assert lib.update_job(low["id"], state="done", progress=1)["state"] == "done"
        lib.add_rating("ab-1", "pair-x", "A", {"ms": 900})
        assert lib.ratings("ab-1")[0]["data"] == {"ms": 900}

        with pytest.raises(sqlite3.IntegrityError):  # foreign keys are on
            lib.put_plan("a", "missing", {}, user_edited=False)
        assert lib.stats()["tracks"] == 2


# --- concurrency --------------------------------------------------------------------------------------------------


def test_parallel_jobs_share_one_writer_and_read_on_their_own_connections(tmp_path):
    lib = Library.open(tmp_path / "lib.sqlite3")
    writers, per_writer = 8, 40
    errors: list[BaseException] = []
    readers: set[int] = set()
    seen: list[list[int]] = [[] for _ in range(3)]  # what each reader thread saw, in order
    stop = threading.Event()
    lock = threading.Lock()

    def job_worker(n: int) -> None:
        try:
            with lock:
                readers.add(id(lib.db.read()))
            for i in range(per_writer):
                h = f"{n}-{i}"
                lib.upsert_track(h, i, title=f"t{h}")
                job = lib.add_job("analyse", {"hash": h})
                lib.put_analysis(h, "beats", "1", {"i": i})
                lib.update_job(job["id"], state="done", progress=1.0)
                assert lib.analysis(h, "beats")["data"] == {"i": i}  # its own write is visible right away
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def reader(n: int) -> None:
        try:
            while not stop.is_set():
                seen[n].append(lib.track_count())
                lib.tracks(limit=20)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=job_worker, args=(n,)) for n in range(writers)]
    watchers = [threading.Thread(target=reader, args=(n,)) for n in range(3)]
    for t in watchers + threads:
        t.start()
    for t in threads:
        t.join(60)
    stop.set()
    for t in watchers:
        t.join(10)
    assert not errors, errors[:3]
    assert len(readers) == writers  # one reader connection per thread
    assert lib.track_count() == writers * per_writer
    assert len(lib.jobs("done")) == writers * per_writer
    for counts in seen:  # readers never block or fail mid-write, and never see the library go back in time
        assert counts and counts == sorted(counts) and counts[-1] <= writers * per_writer
    lib.close()


def test_worker_processes_write_to_the_same_file(tmp_path):
    path = tmp_path / "lib.sqlite3"
    lib = Library.open(path)
    code = ("import sys; from seedbox2.library import Library; lib = Library.open(sys.argv[1]); "
            "[lib.add_job('stems', {'n': i}) for i in range(100)]; lib.close()")  # fmt: skip
    procs = [subprocess.Popen([sys.executable, "-c", code, str(path)], cwd=ROOT) for _ in range(3)]
    for i in range(100):
        lib.add_job("waveform", {"n": i})
    assert [p.wait(60) for p in procs] == [0, 0, 0]
    assert len(lib.jobs()) == 400
    lib.close()
