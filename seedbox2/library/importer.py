"""Import local music: files and folders (watch folders, drag and drop) into the library, keyed by content hash.

`Importer.import_paths()` walks the given files and folders (hidden ones skipped), hashes each audio file with
ffmpeg (hashing.py; a file whose size and mtime are unchanged keeps its hash without a decode), reads its tags
(tags.py: the title, artist and album fill the track, the rest are hints) and records the file. A file inside a
Seedbox 1 crate folder (the tunebat search → crate → download flow) goes through the Seedbox 1 importer instead,
so it lands in the same library with its crate, its set and tunebat's data. `remove_paths()` forgets files that
are gone; `sync_folder()` does both for a watch folder.

Every track the library didn't have before is handed to `on_new` (the server's: `queue_analysis`).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from seedbox2.library import hashing
from seedbox2.library import import_seedbox1 as sb1_import
from seedbox2.library.repo import Library
from seedbox2.library.tags import FileTags, is_audio, read_tags

log = logging.getLogger(__name__)

HASH_WORKERS = min(4, os.cpu_count() or 1)  # ffmpeg decodes run outside the GIL

Progress = Callable[[int, int, str], None]  # (files done, files in total, the current file's name)


@dataclass
class ImportReport:
    files: int = 0  # audio files looked at
    added: int = 0  # tracks new to the library
    updated: int = 0  # files of tracks the library already had
    removed: int = 0  # files forgotten (gone from disk)
    crates: int = 0  # Seedbox 1 crates imported along the way
    errors: list[str] = field(default_factory=list)
    new_hashes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"files": self.files, "added": self.added, "updated": self.updated, "removed": self.removed,
                "crates": self.crates, "errors": self.errors[:50], "error_count": len(self.errors)}  # fmt: skip


def iter_audio(paths: Iterable[Path | str]) -> Iterator[Path]:
    """The audio files among `paths` and inside the folders among them (not following hidden files or folders)."""
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(filenames):
                    if is_audio(name):
                        yield Path(dirpath) / name
        elif p.is_file() and is_audio(p):
            yield p


def queue_analysis(jobs: Any, hashes: Iterable[str]) -> list[dict]:
    """Queue the per-track analyses (registry.per_track) for these tracks: every light one first, at the library's
    priority, then the heavy ones (stems) at background priority, so a big import gets its beats, keys and
    waveforms before the heavy worker's queue matters. Returns the job rows."""
    hashes = list(dict.fromkeys(hashes))
    types = jobs.registry.per_track()
    rows = []
    for pool, priority in (("light", "library"), ("heavy", "background")):
        for h in hashes:
            for t in types:
                if t.pool == pool:
                    rows.append(jobs.submit(t.name, {"hash": h}, priority=priority))
    return rows


class Importer:
    def __init__(self, lib: Library, *, on_new: Callable[[list[str]], None] | None = None,
                 artwork_dir: Path | None = None, workers: int = HASH_WORKERS) -> None:  # fmt: skip
        self.lib = lib
        self.on_new = on_new
        self.artwork_dir = artwork_dir
        self.workers = workers

    def import_paths(self, paths: Iterable[Path | str], *, source: str = "local",
                     progress: Progress | None = None) -> ImportReport:  # fmt: skip
        """Add or update every audio file in `paths` (files and folders). Unreadable files go to the report's
        errors."""
        report = ImportReport()
        files = list(dict.fromkeys(iter_audio(paths)))
        crate_dirs = {f.parent for f in files if sb1_import.is_crate_folder(f.parent)}
        plain = [f for f in files if f.parent not in crate_dirs]
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="seedbox2-import") as pool:
            for seed_dir in sorted(crate_dirs):
                sb1 = sb1_import.ImportReport()
                try:
                    sb1_import.import_crate(self.lib, seed_dir, pool, sb1)
                    report.crates += 1
                except Exception as e:
                    log.exception("importing crate %s", seed_dir.name)
                    report.errors.append(f"{seed_dir.name}: {e}")
                n = sum(f.parent == seed_dir for f in files)
                report.files += n
                report.added += len(sb1.new_hashes)
                report.updated += max(0, sb1.tracks - len(sb1.new_hashes))
                report.new_hashes += sb1.new_hashes
                report.errors += sb1.errors
                done += n
                if progress is not None:
                    progress(done, len(files), seed_dir.name)
            for f, result in zip(plain, pool.map(self._read, plain)):
                done += 1
                report.files += 1
                if isinstance(result, str):
                    report.errors.append(result)
                else:
                    self._add(f, *result, source=source, report=report)
                if progress is not None:
                    progress(done, len(files), f.name)
        if report.new_hashes and self.on_new is not None:
            try:
                self.on_new(list(dict.fromkeys(report.new_hashes)))
            except Exception:
                log.exception("queueing analysis for %d new tracks", len(report.new_hashes))
        return report

    def _read(self, path: Path) -> tuple[hashing.ContentHash, FileTags] | str:
        """Hash and tags of one file (on a pool thread), or the error to report."""
        try:
            return self.lib.hash_file(path), read_tags(path)
        except (OSError, hashing.HashError) as e:
            return f"{path.name}: {e}"

    def _add(self, path: Path, content: hashing.ContentHash, tags: FileTags, *, source: str,
             report: ImportReport) -> None:  # fmt: skip
        old = self.lib.track(content.hash)
        file_tags: dict[str, Any] = {"file": tags.hints}
        if self.artwork_dir is not None:
            try:
                art = tags.save_artwork(self.artwork_dir)
            except OSError as e:
                log.warning("artwork of %s: %s", path.name, e)
                art = None
            if art is not None:
                file_tags["artwork"] = str(art)
        try:
            if old is None:
                self.lib.add_file(path, content=content, title=tags.title or path.stem, artist=tags.artist,
                                  album=tags.album, tags=file_tags, source=source)  # fmt: skip
                report.added += 1
                report.new_hashes.append(content.hash)
            elif old["source"] in ("local", source):  # the file's tags are the best we know: follow retagging
                self.lib.add_file(path, content=content, title=tags.title or old["title"] or path.stem,
                                  artist=tags.artist or old["artist"], album=tags.album or old["album"],
                                  tags=file_tags)  # fmt: skip
                report.updated += 1
            else:  # from Seedbox 1, tunebat...: that source's data stays, the file's tags are kept beside it
                self.lib.add_file(path, content=content, tags=file_tags)
                report.updated += 1
        except OSError as e:  # gone between the hash and now
            report.errors.append(f"{path.name}: {e}")

    def remove_paths(self, paths: Iterable[Path | str]) -> ImportReport:
        """Forget these files and the files inside these folders; tracks left with no file go (see
        Library.remove_paths)."""
        report = ImportReport()
        keys: list[str] = []
        for p in paths:
            keys += self.lib.paths_under(p)
            keys.append(os.path.abspath(p))
        before = {k for k in keys if self.lib.track_for_path(k) is not None}
        self.lib.remove_paths(before)
        report.removed = len(before)
        return report

    def sync_folder(self, folder: Path | str, *, progress: Progress | None = None) -> ImportReport:
        """Import everything in `folder` and forget the files recorded inside it that are no longer there."""
        report = self.import_paths([folder], progress=progress)
        present = {os.path.abspath(f) for f in iter_audio([folder])}
        gone = [p for p in self.lib.paths_under(folder) if p not in present]
        if gone:
            report.removed = self.remove_paths(gone).removed
        return report


class ImportStatus:
    """The latest import's state for GET and the `library.import` events: {state, kind, done, total, ...}."""

    def __init__(self, publish: Callable[[dict], None] | None = None) -> None:
        self._publish = publish
        self._lock = threading.Lock()
        self.current: dict = {"state": "idle"}

    def update(self, status: dict) -> None:
        with self._lock:
            self.current = status
        if self._publish is not None:
            self._publish(status)
