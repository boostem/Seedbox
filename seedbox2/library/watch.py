"""Watch folders: scan each folder, then follow its changes with watchdog and keep the library in step.

Events are batched: a path is handled once nothing has happened to it for `debounce_s` and its size has stopped
changing (a download or a copy still being written waits). Within a batch, new and changed files are imported
before gone ones are forgotten, so a file moved or renamed inside the watched folders keeps its track (and its
analyses: the hash is the same). Hidden files and folders (`.seedbox`, `.DS_Store`, partial downloads named
`.something`) are ignored, as are files that aren't audio (tags.AUDIO_EXTENSIONS).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from seedbox2.library.import_seedbox1 import is_crate_folder
from seedbox2.library.importer import Importer, ImportReport
from seedbox2.library.tags import is_audio

log = logging.getLogger(__name__)

DEBOUNCE_S = 2.0


def _hidden(path: str, roots: Iterable[str]) -> bool:
    for root in roots:
        if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
            rel = path[len(root):].strip(os.sep)
            return any(part.startswith(".") for part in rel.split(os.sep) if part)
    return True  # outside every watched folder


class FolderWatcher:
    """Keeps the files in `folders` (recursively) in the library. `on_report(folder_or_none, report)` is called after
    each scan and each batch of changes."""

    def __init__(self, importer: Importer, folders: Iterable[Path | str] = (), *, debounce_s: float = DEBOUNCE_S,
                 on_report: Callable[[str | None, ImportReport], None] | None = None,
                 on_scan: Callable[[str, int, int, str], None] | None = None) -> None:  # fmt: skip
        self.importer = importer
        self.debounce_s = debounce_s
        self.on_report = on_report
        self.on_scan = on_scan  # (folder, done, total, file) while a folder is scanned
        self._folders: dict[str, Any] = {os.path.abspath(f): None for f in folders}  # folder -> watchdog watch
        self._pending: dict[str, tuple[str, float, int]] = {}  # path -> (changed|deleted, last event, size)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._observer: Any = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._busy = 0  # scans started and batches being applied
        self.scanning: str | None = None

    @property
    def folders(self) -> list[str]:
        with self._lock:
            return list(self._folders)

    # --- lifecycle -------------------------------------------------------------------------------------------------

    def start(self) -> FolderWatcher:
        """Start watching, then scan every folder (in the background: this returns at once)."""
        from watchdog.observers import Observer

        with self._lock:
            if self._thread is not None:
                return self
            self._observer = Observer()
            self._observer.daemon = True
            self._observer.start()
            folders = list(self._folders)
            self._busy += 1
            self._thread = threading.Thread(target=self._run, name="seedbox2-watch", daemon=True)
            self._thread.start()
        for folder in folders:
            self._schedule(folder)
        threading.Thread(target=self._scan_all, args=(folders,), name="seedbox2-watch-scan", daemon=True).start()
        return self

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join(5)
        if self._thread is not None:
            self._thread.join(5)

    def add(self, folder: Path | str) -> bool:
        """Watch one more folder and scan it (in the background). False if it was watched already."""
        folder = os.path.abspath(folder)
        with self._lock:
            if folder in self._folders:
                return False
            self._folders[folder] = None
            running = self._observer is not None
            if running:
                self._busy += 1
        if running:
            self._schedule(folder)
            threading.Thread(target=self._scan_all, args=([folder],), name="seedbox2-watch-scan", daemon=True).start()
        return True

    def remove(self, folder: Path | str) -> bool:
        """Stop watching a folder. Its tracks stay in the library."""
        folder = os.path.abspath(folder)
        with self._lock:
            if folder not in self._folders:
                return False
            watch = self._folders.pop(folder)
            if watch is not None and self._observer is not None:
                self._observer.unschedule(watch)
            for p in [p for p in self._pending if p == folder or p.startswith(folder + os.sep)]:
                del self._pending[p]
        return True

    def _schedule(self, folder: str) -> None:
        if not os.path.isdir(folder):
            log.warning("watch folder %s doesn't exist (yet): not watched", folder)
            return
        from watchdog.events import FileSystemEventHandler

        watcher = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:
                watcher._event(event)

        try:
            with self._lock:
                if self._observer is None or folder not in self._folders:
                    return
                self._folders[folder] = self._observer.schedule(Handler(), folder, recursive=True)
        except OSError as e:  # too many inotify watches, permissions...
            log.warning("can't watch %s: %s", folder, e)

    def _scan_all(self, folders: list[str]) -> None:
        for folder in folders:
            if self._stopping or folder not in self._folders or not os.path.isdir(folder):
                continue
            self.scanning = folder
            try:
                progress = (lambda d, t, f, folder=folder: self.on_scan(folder, d, t, f)) if self.on_scan else None
                report = self.importer.sync_folder(folder, progress=progress)
                log.info("watch folder %s scanned: %s", folder, report.to_json())
                if self.on_report is not None:
                    self.on_report(folder, report)
            except Exception:
                log.exception("scanning %s", folder)
            finally:
                self.scanning = None
        with self._lock:
            self._busy -= 1

    # --- events ----------------------------------------------------------------------------------------------------

    def _event(self, event: Any) -> None:
        kind = event.event_type
        if kind in ("opened", "closed_no_write"):
            return
        items: list[tuple[str, str]] = []
        if kind == "moved":
            items = [(os.fsdecode(event.src_path), "deleted"), (os.fsdecode(event.dest_path), "changed")]
        elif kind == "deleted":
            items = [(os.fsdecode(event.src_path), "deleted")]
        elif not (event.is_directory and kind == "modified"):  # created, modified, closed
            items = [(os.fsdecode(event.src_path), "changed")]
        now = time.monotonic()
        with self._cond:
            roots = list(self._folders)
            for path, what in items:
                if _hidden(path, roots) or not (event.is_directory or is_audio(path)):
                    continue
                self._pending[path] = (what, now, -1)
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
                if not self._pending:
                    self._cond.wait()
                    continue
                now = time.monotonic()
                ready: dict[str, str] = {}
                # changes first: a file still being written holds back the deletions too, so the two halves of a
                # move land in one batch and the track keeps its analyses
                for path, (what, at, size) in sorted(self._pending.items(), key=lambda kv: kv[1][0] != "changed"):
                    if now - at < self.debounce_s:
                        continue
                    if what == "deleted" and any(w == "changed" for w, _, _ in self._pending.values()):
                        continue
                    if what == "changed":
                        try:
                            current = os.stat(path).st_size if os.path.isfile(path) else 0
                        except OSError:
                            current = 0
                        if current != size:  # still being written: look again after another debounce
                            self._pending[path] = (what, now, current)
                            continue
                    ready[path] = what
                    del self._pending[path]
                if not ready:
                    soonest = min(at for _, at, _ in self._pending.values())
                    self._cond.wait(max(0.05, soonest + self.debounce_s - now))
                    continue
                self._busy += 1
            try:
                self._apply(ready)
            finally:
                with self._lock:
                    self._busy -= 1

    def _apply(self, ready: dict[str, str]) -> None:
        changed = [p for p, w in ready.items() if w == "changed" and os.path.exists(p)]
        gone = [p for p, w in ready.items() if w == "deleted" or not os.path.exists(p)]
        # a file gone from a Seedbox 1 crate: import the crate again, so its track list follows
        crates = {os.path.dirname(p) for p in gone if is_audio(p) and is_crate_folder(Path(os.path.dirname(p)))}
        try:
            report = self.importer.import_paths([*changed, *sorted(crates)]) if changed or crates else ImportReport()
            if gone:
                report.removed = self.importer.remove_paths(gone).removed
        except Exception:
            log.exception("applying %d watch folder changes", len(ready))
            return
        log.info("watch folders changed: %s", report.to_json())
        if self.on_report is not None:
            self.on_report(None, report)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """True once no change is pending and no scan or batch runs."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                idle = not self._pending and not self._busy
            if idle:
                return True
            time.sleep(0.05)
        return False
