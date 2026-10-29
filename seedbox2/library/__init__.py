"""The Seedbox 2 library (#38): one SQLite database keyed by a content hash of the decoded audio.

- `db`: schema, migrations and connections (one writer, a reader per thread)
- `hashing`: the content hash that survives retagging and renames
- `repo`: `Library`, the repository the server routers and jobs use (no SQL outside this package)
- `import_seedbox1`: read-only import of Seedbox 1's crates, sets and user-edited mix plans
- local music (#40): `tags` (mutagen, tags as hints), `importer` (files and folders, analysis queueing), `watch`
  (watch folders with watchdog), `playlists_xml` (Rekordbox and iTunes/Music XML as crates), `service` (what the
  server runs: settings, the import queue, the watcher)
"""

from __future__ import annotations

import os
from pathlib import Path

from seedbox2.library.db import FILENAME, Database
from seedbox2.library.repo import Library

__all__ = ["Database", "Library", "default_path"]


def default_path() -> Path:
    """library.sqlite3 in Seedbox 2's support folder ($SEEDBOX_SUPPORT_DIR, set by the launcher)."""
    support = os.environ.get("SEEDBOX_SUPPORT_DIR", "").strip()
    if not support:
        from seedbox2.app import data_dirs

        support = str(data_dirs()["SEEDBOX_SUPPORT_DIR"])
    return Path(support) / FILENAME
