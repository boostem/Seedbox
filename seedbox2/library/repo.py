"""Library: the repository over the library database. Routers and jobs call this; SQL stays in this package.

Rows come back as plain dicts with JSON columns decoded (`tags`, `data`, `plan`, `payload`) and `user_edited` as a
bool. Every write is one transaction on the database's writer connection; reads use the calling thread's reader.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from seedbox2.library import hashing
from seedbox2.library.db import Database

TRACK_COLUMNS = ("title", "artist", "album", "tags", "source", "source_id")
ARTIFACT_KINDS = ("stems", "waveform", "embedding")
JOB_STATES = ("queued", "running", "done", "failed", "cancelled")
_JSON = ("tags", "data", "plan", "payload", "result")
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in _JSON:
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    if "user_edited" in d:
        d["user_edited"] = bool(d["user_edited"])
    return d


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [_row(r) for r in rows]  # type: ignore[misc]


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _key(path: Path | str) -> str:
    return os.path.abspath(path)


class Library:
    def __init__(self, db: Database, hasher: Callable[[Path], hashing.ContentHash] = hashing.content_hash) -> None:
        self.db = db
        self._hasher = hasher

    @classmethod
    def open(cls, path: Path | str) -> Library:
        return cls(Database(path))

    def close(self) -> None:
        self.db.close()

    # --- tracks and files ------------------------------------------------------------------------------------------

    def hash_file(self, path: Path | str) -> hashing.ContentHash:
        """The file's content hash: from track_paths while its size and mtime are unchanged, else decoded.
        HashError if it can't be decoded, OSError if it can't be read."""
        st = os.stat(path)
        row = self.db.read().execute(
            "SELECT p.hash, t.duration FROM track_paths p JOIN tracks t USING (hash) "
            "WHERE p.path = ? AND p.size = ? AND p.mtime_ns = ?", (_key(path), st.st_size, st.st_mtime_ns)).fetchone()  # fmt: skip
        if row is not None:
            return hashing.ContentHash(row["hash"], row["duration"])
        return self._hasher(Path(path))

    def add_file(self, path: Path | str, *, content: hashing.ContentHash | None = None, **meta: Any) -> dict:
        """Hash `path` (unless `content` is given), add or update its track and record the file. `meta`: any of
        TRACK_COLUMNS; tags merge into the existing ones. Returns the track."""
        content = content or self.hash_file(path)
        st = os.stat(path)
        with self.db.write() as c:
            self._upsert_track(c, content.hash, content.duration, meta)
            c.execute(f"INSERT INTO track_paths (path, hash, size, mtime_ns) VALUES (?, ?, ?, ?) "
                      f"ON CONFLICT (path) DO UPDATE SET hash = excluded.hash, size = excluded.size, "
                      f"mtime_ns = excluded.mtime_ns, seen_at = {_NOW}",
                      (_key(path), content.hash, st.st_size, st.st_mtime_ns))  # fmt: skip
            return self.track(content.hash)  # type: ignore[return-value]

    def upsert_track(self, hash_: str, duration: float, **meta: Any) -> dict:
        with self.db.write() as c:
            self._upsert_track(c, hash_, duration, meta)
            return self.track(hash_)  # type: ignore[return-value]

    @staticmethod
    def _upsert_track(c: sqlite3.Connection, hash_: str, duration: float, meta: dict) -> None:
        bad = set(meta) - set(TRACK_COLUMNS)
        if bad:
            raise TypeError(f"unknown track fields: {sorted(bad)}")
        old = c.execute("SELECT tags FROM tracks WHERE hash = ?", (hash_,)).fetchone()
        if old is None:
            cols = {"hash": hash_, "duration": duration, **meta}
            if "tags" in cols:
                cols["tags"] = _dumps(cols["tags"] or {})
            c.execute(f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      tuple(cols.values()))  # fmt: skip
            return
        sets = {"duration": duration, **meta}
        if "tags" in sets:
            sets["tags"] = _dumps({**json.loads(old["tags"]), **(sets["tags"] or {})})
        c.execute(f"UPDATE tracks SET {', '.join(f'{k} = ?' for k in sets)}, updated_at = {_NOW} WHERE hash = ?",
                  (*sets.values(), hash_))  # fmt: skip

    def track(self, hash_: str) -> dict | None:
        t = _row(self.db.read().execute("SELECT * FROM tracks WHERE hash = ?", (hash_,)).fetchone())
        if t is not None:
            t["paths"] = self.paths(hash_)
        return t

    def tracks(self, *, search: str = "", source: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        where, args = ["1"], []
        if search:
            where.append("(title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\' OR album LIKE ? ESCAPE '\\')")
            like = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [like] * 3
        if source is not None:
            where.append("source = ?")
            args.append(source)
        rows = self.db.read().execute(
            f"SELECT * FROM tracks WHERE {' AND '.join(where)} ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, "
            f"hash LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()  # fmt: skip
        return _rows(rows)

    def track_count(self) -> int:
        return self.db.read().execute("SELECT count(*) FROM tracks").fetchone()[0]

    def track_by_source(self, source: str, source_id: str) -> dict | None:
        row = self.db.read().execute("SELECT hash FROM tracks WHERE source = ? AND source_id = ? LIMIT 1",
                                     (source, source_id)).fetchone()  # fmt: skip
        return self.track(row["hash"]) if row else None

    def track_for_path(self, path: Path | str) -> dict | None:
        row = self.db.read().execute("SELECT hash FROM track_paths WHERE path = ?", (_key(path),)).fetchone()
        return self.track(row["hash"]) if row else None

    def paths(self, hash_: str) -> list[str]:
        rows = self.db.read().execute("SELECT path FROM track_paths WHERE hash = ? ORDER BY seen_at DESC, path",
                                      (hash_,)).fetchall()  # fmt: skip
        return [r["path"] for r in rows]

    def forget_path(self, path: Path | str) -> None:
        with self.db.write() as c:
            c.execute("DELETE FROM track_paths WHERE path = ?", (_key(path),))

    # --- analyses and artifacts ------------------------------------------------------------------------------------

    def put_analysis(self, hash_: str, analyser: str, version: str, data: Any) -> None:
        with self.db.write() as c:
            c.execute(f"INSERT INTO analyses (hash, analyser, version, data) VALUES (?, ?, ?, ?) "
                      f"ON CONFLICT DO UPDATE SET data = excluded.data, created_at = {_NOW}",
                      (hash_, analyser, str(version), _dumps(data)))  # fmt: skip

    def analysis(self, hash_: str, analyser: str, version: str | None = None) -> dict | None:
        """The row for that version, or the newest one written when `version` is None."""
        if version is not None:
            row = self.db.read().execute("SELECT * FROM analyses WHERE hash = ? AND analyser = ? AND version = ?",
                                         (hash_, analyser, str(version))).fetchone()  # fmt: skip
        else:
            row = self.db.read().execute("SELECT * FROM analyses WHERE hash = ? AND analyser = ? "
                                         "ORDER BY created_at DESC, rowid DESC LIMIT 1", (hash_, analyser)).fetchone()  # fmt: skip
        return _row(row)

    def analyses(self, hash_: str) -> list[dict]:
        return _rows(self.db.read().execute("SELECT analyser, version, created_at FROM analyses WHERE hash = ? "
                                            "ORDER BY analyser, created_at", (hash_,)))  # fmt: skip

    def put_artifact(self, hash_: str, kind: str, model_version: str, path: Path | str) -> None:
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"artifact kind must be one of {ARTIFACT_KINDS}")
        with self.db.write() as c:
            c.execute(f"INSERT INTO artifacts (hash, kind, model_version, path) VALUES (?, ?, ?, ?) "
                      f"ON CONFLICT DO UPDATE SET path = excluded.path, created_at = {_NOW}",
                      (hash_, kind, model_version, str(path)))  # fmt: skip

    def artifacts(self, hash_: str, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM artifacts WHERE hash = ?", [hash_]
        if kind is not None:
            sql, args = sql + " AND kind = ?", [*args, kind]
        return _rows(self.db.read().execute(sql + " ORDER BY kind, created_at", args))

    # --- crates and sets -------------------------------------------------------------------------------------------

    def save_crate(self, name: str, hashes: Iterable[str], *, folder: str | None = None, source: str = "local",
                   seed_hash: str | None = None) -> int:
        """Create the crate (or replace the one with this source and folder) with these tracks in this order."""
        with self.db.write() as c:
            row = None
            if folder is not None:
                row = c.execute("SELECT id FROM crates WHERE source = ? AND folder = ?", (source, folder)).fetchone()
            if row is None:
                crate_id = c.execute("INSERT INTO crates (name, folder, source, seed_hash) VALUES (?, ?, ?, ?)",
                                     (name, folder, source, seed_hash)).lastrowid  # fmt: skip
            else:
                crate_id = row["id"]
                c.execute(f"UPDATE crates SET name = ?, seed_hash = ?, updated_at = {_NOW} WHERE id = ?",
                          (name, seed_hash, crate_id))  # fmt: skip
                c.execute("DELETE FROM crate_tracks WHERE crate_id = ?", (crate_id,))
            c.executemany("INSERT OR IGNORE INTO crate_tracks (crate_id, hash, position) VALUES (?, ?, ?)",
                          [(crate_id, h, n) for n, h in enumerate(dict.fromkeys(hashes))])  # fmt: skip
            return crate_id  # type: ignore[return-value]

    def crates(self) -> list[dict]:
        return _rows(self.db.read().execute(
            "SELECT c.*, (SELECT count(*) FROM crate_tracks t WHERE t.crate_id = c.id) AS track_count "
            "FROM crates c ORDER BY c.updated_at DESC, c.name COLLATE NOCASE"))  # fmt: skip

    def crate(self, crate_id: int) -> dict | None:
        crate = _row(self.db.read().execute("SELECT * FROM crates WHERE id = ?", (crate_id,)).fetchone())
        if crate is not None:
            crate["tracks"] = _rows(self.db.read().execute(
                "SELECT t.* FROM crate_tracks ct JOIN tracks t USING (hash) WHERE ct.crate_id = ? ORDER BY ct.position",
                (crate_id,)))  # fmt: skip
        return crate

    def save_set(self, name: str, hashes: Iterable[str], *, set_id: int | None = None, crate_id: int | None = None,
                 source: str = "local") -> int:
        """Create a set, or replace set `set_id` (or the one built from `crate_id` by `source`); `hashes` in running
        order (a track may come back)."""
        with self.db.write() as c:
            if set_id is None and crate_id is not None:
                row = c.execute("SELECT id FROM sets WHERE source = ? AND crate_id = ?", (source, crate_id)).fetchone()
                set_id = row["id"] if row else None
            if set_id is None:
                set_id = c.execute("INSERT INTO sets (name, crate_id, source) VALUES (?, ?, ?)",
                                   (name, crate_id, source)).lastrowid  # fmt: skip
            else:
                if c.execute(f"UPDATE sets SET name = ?, updated_at = {_NOW} WHERE id = ?", (name, set_id)).rowcount == 0:
                    raise KeyError(set_id)
                c.execute("DELETE FROM set_items WHERE set_id = ?", (set_id,))
            c.executemany("INSERT INTO set_items (set_id, position, hash) VALUES (?, ?, ?)",
                          [(set_id, n, h) for n, h in enumerate(hashes)])  # fmt: skip
            return set_id  # type: ignore[return-value]

    def sets(self) -> list[dict]:
        return _rows(self.db.read().execute(
            "SELECT s.*, (SELECT count(*) FROM set_items i WHERE i.set_id = s.id) AS track_count "
            "FROM sets s ORDER BY s.updated_at DESC, s.name COLLATE NOCASE"))  # fmt: skip

    def set(self, set_id: int) -> dict | None:
        s = _row(self.db.read().execute("SELECT * FROM sets WHERE id = ?", (set_id,)).fetchone())
        if s is not None:
            s["tracks"] = _rows(self.db.read().execute(
                "SELECT i.position, t.* FROM set_items i JOIN tracks t USING (hash) WHERE i.set_id = ? "
                "ORDER BY i.position", (set_id,)))  # fmt: skip
        return s

    def delete_set(self, set_id: int) -> bool:
        with self.db.write() as c:
            return c.execute("DELETE FROM sets WHERE id = ?", (set_id,)).rowcount > 0

    # --- mix plans -------------------------------------------------------------------------------------------------

    def put_plan(self, from_hash: str, to_hash: str, plan: Any, *, user_edited: bool, source: str = "local",
                 overwrite: bool = True) -> bool:
        """Save the plan for this pair; with overwrite=False only if the pair has none from another source.
        Returns whether it was written."""
        sql = ("INSERT INTO plans (from_hash, to_hash, plan, user_edited, source) VALUES (?, ?, ?, ?, ?) "
               f"ON CONFLICT DO UPDATE SET plan = excluded.plan, user_edited = excluded.user_edited, "
               f"source = excluded.source, updated_at = {_NOW}")  # fmt: skip
        if not overwrite:
            sql += " WHERE plans.source = excluded.source"
        with self.db.write() as c:
            return c.execute(sql, (from_hash, to_hash, _dumps(plan), int(user_edited), source)).rowcount > 0

    def plan(self, from_hash: str, to_hash: str) -> dict | None:
        return _row(self.db.read().execute("SELECT * FROM plans WHERE from_hash = ? AND to_hash = ?",
                                           (from_hash, to_hash)).fetchone())  # fmt: skip

    def plans(self, *, user_edited: bool | None = None) -> list[dict]:
        sql, args = "SELECT * FROM plans", []
        if user_edited is not None:
            sql, args = sql + " WHERE user_edited = ?", [int(user_edited)]
        return _rows(self.db.read().execute(sql + " ORDER BY updated_at DESC", args))

    def delete_plan(self, from_hash: str, to_hash: str) -> bool:
        with self.db.write() as c:
            return c.execute("DELETE FROM plans WHERE from_hash = ? AND to_hash = ?", (from_hash, to_hash)).rowcount > 0

    # --- jobs (persisted state for #39) -----------------------------------------------------------------------------

    def add_job(self, kind: str, payload: Any = None, *, priority: int = 0, job_id: str | None = None) -> dict:
        job_id = job_id or uuid.uuid4().hex
        with self.db.write() as c:
            c.execute("INSERT INTO jobs (id, kind, priority, payload) VALUES (?, ?, ?, ?)",
                      (job_id, kind, priority, _dumps(payload if payload is not None else {})))  # fmt: skip
            return self.job(job_id)  # type: ignore[return-value]

    def update_job(self, job_id: str, *, state: str | None = None, progress: float | None = None,
                   error: str | None = None, priority: int | None = None, result: Any = None) -> dict | None:
        """Change the given fields (None leaves a field as it is)."""
        if state is not None and state not in JOB_STATES:
            raise ValueError(f"job state must be one of {JOB_STATES}")
        sets = {k: v for k, v in (("state", state), ("progress", progress), ("error", error), ("priority", priority),
                                  ("result", None if result is None else _dumps(result))) if v is not None}  # fmt: skip
        with self.db.write() as c:
            c.execute(f"UPDATE jobs SET {''.join(f'{k} = ?, ' for k in sets)}updated_at = {_NOW} WHERE id = ?",
                      (*sets.values(), job_id))  # fmt: skip
            return self.job(job_id)

    def job(self, job_id: str) -> dict | None:
        return _row(self.db.read().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def jobs(self, state: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM jobs", []
        if state is not None:
            sql, args = sql + " WHERE state = ?", [state]
        return _rows(self.db.read().execute(sql + " ORDER BY priority DESC, created_at, rowid", args))

    def set_jobs_priority(self, job_ids: Iterable[str], priority: int) -> None:
        with self.db.write() as c:
            c.executemany(f"UPDATE jobs SET priority = ?, updated_at = {_NOW} WHERE id = ?",
                          [(priority, i) for i in job_ids])  # fmt: skip

    def claim_job(self, kinds: Iterable[str] | None = None) -> dict | None:
        """Mark the most urgent queued job (of these kinds) running and return it; None if there is none."""
        kinds = list(kinds) if kinds is not None else None
        sql, args = "SELECT id FROM jobs WHERE state = 'queued'", []
        if kinds is not None:
            sql += f" AND kind IN ({', '.join('?' * len(kinds))})"
            args = kinds
        with self.db.write() as c:
            row = c.execute(sql + " ORDER BY priority DESC, created_at, rowid LIMIT 1", args).fetchone()
            if row is None:
                return None
            c.execute(f"UPDATE jobs SET state = 'running', updated_at = {_NOW} WHERE id = ?", (row["id"],))
            return self.job(row["id"])

    # --- listening-test ratings ------------------------------------------------------------------------------------

    def add_rating(self, test: str, subject: str, choice: str, data: Any = None) -> int:
        with self.db.write() as c:
            return c.execute("INSERT INTO ratings (test, subject, choice, data) VALUES (?, ?, ?, ?)",
                             (test, subject, choice, _dumps(data or {}))).lastrowid  # type: ignore[return-value]

    def ratings(self, test: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM ratings", []
        if test is not None:
            sql, args = sql + " WHERE test = ?", [test]
        return _rows(self.db.read().execute(sql + " ORDER BY id", args))

    # --- summary ---------------------------------------------------------------------------------------------------

    def stats(self) -> dict:
        r = self.db.read()
        count = {t: r.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                 for t in ("tracks", "track_paths", "analyses", "artifacts", "crates", "sets", "plans", "jobs")}  # fmt: skip
        return {"path": str(self.db.path), **count,
                "user_edited_plans": r.execute("SELECT count(*) FROM plans WHERE user_edited").fetchone()[0]}  # fmt: skip
