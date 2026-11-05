"""The library database: one SQLite file (WAL), its schema as numbered migrations, and its connections.

The schema version is SQLite's `PRAGMA user_version`. `MIGRATIONS[n]` takes a database from version n to n + 1 and
runs in one transaction with the version bump, so a failed migration leaves the file as it was. Never edit a
released migration: append a new one.

Connections (`Database`): one writer connection shared by every thread and serialised by a lock (`with
db.write() as c:` is one IMMEDIATE transaction), and one read-only connection per thread (`db.read()`). WAL lets
readers run while the writer writes, and each reader statement sees the last committed state. Worker processes
(#39) open their own `Database` on the same file; `busy_timeout` makes their writers wait their turn.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

FILENAME = "library.sqlite3"
BUSY_TIMEOUT_MS = 30_000

Migration = Callable[[sqlite3.Connection], None] | str

_V1 = """
CREATE TABLE tracks (
    hash        TEXT PRIMARY KEY,               -- content hash of the decoded audio (hashing.py)
    duration    REAL NOT NULL,                  -- seconds of decoded audio
    title       TEXT NOT NULL DEFAULT '',
    artist      TEXT NOT NULL DEFAULT '',
    album       TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '{}',     -- JSON: everything else the file's tags or the source say
    source      TEXT NOT NULL DEFAULT 'local',  -- seedbox1, local, rekordbox, itunes, tunebat, youtube...
    source_id   TEXT,                           -- the track's ID at that source (e.g. Seedbox 1's Spotify ID)
    added_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX tracks_source ON tracks (source, source_id);

-- every file that holds a track; (size, mtime_ns) tell whether the hash is still valid without decoding again
CREATE TABLE track_paths (
    path        TEXT PRIMARY KEY,
    hash        TEXT NOT NULL REFERENCES tracks (hash) ON DELETE CASCADE,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    seen_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX track_paths_hash ON track_paths (hash);

CREATE TABLE analyses (
    hash        TEXT NOT NULL REFERENCES tracks (hash) ON DELETE CASCADE,
    analyser    TEXT NOT NULL,                  -- e.g. beats, key, sections, seedbox1
    version     TEXT NOT NULL,                  -- the analyser's version: a new one is a new row
    data        TEXT NOT NULL,                  -- JSON
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (hash, analyser, version)
);

CREATE TABLE artifacts (
    hash          TEXT NOT NULL REFERENCES tracks (hash) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('stems', 'waveform', 'embedding')),
    model_version TEXT NOT NULL,
    path          TEXT NOT NULL,                -- file or folder in the cache
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (hash, kind, model_version)
);

CREATE TABLE crates (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    folder      TEXT,                           -- the folder it mirrors, if any (Seedbox 1 crates)
    source      TEXT NOT NULL DEFAULT 'local',
    seed_hash   TEXT REFERENCES tracks (hash) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source, folder)
);

CREATE TABLE crate_tracks (
    crate_id    INTEGER NOT NULL REFERENCES crates (id) ON DELETE CASCADE,
    hash        TEXT NOT NULL REFERENCES tracks (hash) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    PRIMARY KEY (crate_id, hash)
);
CREATE INDEX crate_tracks_hash ON crate_tracks (hash);

CREATE TABLE sets (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    crate_id    INTEGER REFERENCES crates (id) ON DELETE SET NULL,  -- the crate it was built from, if one
    source      TEXT NOT NULL DEFAULT 'local',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE UNIQUE INDEX sets_crate_source ON sets (source, crate_id) WHERE crate_id IS NOT NULL;

CREATE TABLE set_items (
    set_id      INTEGER NOT NULL REFERENCES sets (id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,               -- 0-based running order
    hash        TEXT NOT NULL REFERENCES tracks (hash) ON DELETE CASCADE,
    PRIMARY KEY (set_id, position)
);

-- one plan per ordered pair of tracks, wherever they meet
CREATE TABLE plans (
    from_hash   TEXT NOT NULL REFERENCES tracks (hash) ON DELETE CASCADE,
    to_hash     TEXT NOT NULL REFERENCES tracks (hash) ON DELETE CASCADE,
    plan        TEXT NOT NULL,                  -- JSON
    user_edited INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'local',
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (from_hash, to_hash)
);

CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'queued', -- queued, running, done, failed, cancelled
    priority    INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL DEFAULT '{}',     -- JSON
    progress    REAL NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX jobs_queue ON jobs (state, priority DESC, created_at);

-- listening tests (#86): one row per answer
CREATE TABLE ratings (
    id          INTEGER PRIMARY KEY,
    test        TEXT NOT NULL,
    subject     TEXT NOT NULL,
    choice      TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',     -- JSON
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX ratings_test ON ratings (test, subject);
"""

# v2 (#39): what a finished job returned (JSON), for the jobs that don't write it to the library themselves
_V2 = """
ALTER TABLE jobs ADD COLUMN result TEXT;
"""

# v3 (#60): the energy arc, tempo journey and pins a set's order was last made with (JSON)
_V3 = """
ALTER TABLE sets ADD COLUMN order_settings TEXT;
"""

MIGRATIONS: list[Migration] = [_V1, _V2, _V3]
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaTooNew(RuntimeError):
    """The file was written by a newer Seedbox 2."""


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _statements(script: str) -> Iterator[str]:
    """`script` split into complete statements (executescript would commit the migration's transaction)."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                yield buf
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement in migration: {buf.strip()[:80]}")


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Bring the database up to len(migrations), one transaction per step; returns the version it started at.

    `conn` must be in autocommit mode (isolation_level=None). SchemaTooNew if the file is ahead of this code.
    """
    start = schema_version(conn)
    if start > len(migrations):
        raise SchemaTooNew(f"library schema v{start} is newer than this Seedbox 2 (v{len(migrations)})")
    for version in range(start, len(migrations)):
        step = migrations[version]
        conn.execute("BEGIN IMMEDIATE")
        try:
            if schema_version(conn) != version:  # another process migrated it meanwhile
                conn.execute("ROLLBACK")
                continue
            if callable(step):
                step(conn)
            else:
                for stmt in _statements(step):
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version + 1}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        log.info("library schema migrated to v%d", version + 1)
    return start


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL: a crash loses at most the last commits
    return conn


class Database:
    """The library file: migrated on open, one writer connection, one reader connection per thread."""

    def __init__(self, path: Path | str, migrations: Sequence[Migration] = MIGRATIONS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = _connect(self.path)
        try:
            if self._writer.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() != "wal":
                raise sqlite3.OperationalError(f"could not put {self.path} in WAL mode")
            migrate(self._writer, migrations)
        except BaseException:
            self._writer.close()
            raise
        self._write_lock = threading.RLock()
        self._depth = 0
        self._owner: int | None = None  # thread inside write()
        self._local = threading.local()
        self._readers: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._readers_lock = threading.Lock()
        self._closed = False

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """The writer connection inside one transaction, committed on exit (rolled back on an exception).

        Threads queue on a lock; nested write() blocks in one thread join the outer transaction.
        """
        with self._write_lock:
            if self._closed:
                raise sqlite3.ProgrammingError("library database is closed")
            if self._depth:
                self._depth += 1
                try:
                    yield self._writer
                finally:
                    self._depth -= 1
                return
            self._writer.execute("BEGIN IMMEDIATE")
            self._depth, self._owner = 1, threading.get_ident()
            try:
                yield self._writer
            except BaseException:
                self._writer.execute("ROLLBACK")
                raise
            else:
                self._writer.execute("COMMIT")
            finally:
                self._depth, self._owner = 0, None

    def read(self) -> sqlite3.Connection:
        """This thread's read-only connection (opened on first use). Inside write() on this thread, the writer,
        so a transaction reads its own changes."""
        if self._owner == threading.get_ident():
            return self._writer
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._closed:
                raise sqlite3.ProgrammingError("library database is closed")
            conn = _connect(self.path, readonly=True)
            self._local.conn = conn
            with self._readers_lock:
                for thread, old in self._readers:  # threads that have finished (e.g. a pool's) leave theirs
                    if not thread.is_alive():
                        old.close()
                self._readers = [(t, c) for t, c in self._readers if t.is_alive()]
                self._readers.append((threading.current_thread(), conn))
        return conn

    def close(self) -> None:
        with self._write_lock:
            self._closed = True
            with self._readers_lock:
                for _thread, conn in self._readers:
                    conn.close()
                self._readers.clear()
            self._writer.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
