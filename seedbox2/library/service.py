"""LocalMusic: what the server runs to bring local music in (#40). Watch folders (kept in settings.json, default:
the music folder Seedbox 1 downloads into), imports of dropped files and folders, and Rekordbox/iTunes XML.

Imports asked for by the user run one at a time on one background thread, in the order they were asked; the
watch folders scan and follow their changes on their own threads. Every step is published as a `library.import`
event ({state: queued|running|done|failed, kind: files|xml|watch, ...}) and the latest is kept for GET. New tracks
get their analyses queued (importer.queue_analysis) through `on_new`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from seedbox2.library import playlists_xml
from seedbox2.library.importer import Importer, ImportReport
from seedbox2.library.repo import Library
from seedbox2.library.watch import FolderWatcher

log = logging.getLogger(__name__)

SETTINGS_FILE = "settings.json"
UPLOADS_SUBDIR = "Imported"  # in the music folder: files dropped onto the app in a browser, which sends no path


class Settings:
    """A small JSON file of Seedbox 2's settings, written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("unreadable settings %s: %s", self.path, e)
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = {**self.load(), key: value}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
            tmp.replace(self.path)


class LocalMusic:
    def __init__(self, lib: Library, *, settings: Settings, music_dir: Path, artwork_dir: Path | None = None,
                 on_new: Callable[[list[str]], None] | None = None,
                 publish: Callable[[dict], None] | None = None) -> None:  # fmt: skip
        self.lib = lib
        self.settings = settings
        self.music_dir = Path(music_dir)
        self.importer = Importer(lib, on_new=on_new, artwork_dir=artwork_dir)
        self._publish = publish
        self._lock = threading.Lock()
        self.status: dict = {"state": "idle"}
        self._queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix="seedbox2-import")
        self.watcher = FolderWatcher(self.importer, self.watch_folders(), on_report=self._watch_report,
                                     on_scan=self._watch_scan)  # fmt: skip

    # --- watch folders ---------------------------------------------------------------------------------------------

    def watch_folders(self) -> list[str]:
        folders = self.settings.get("watch_folders")
        if not isinstance(folders, list):
            return [str(self.music_dir)]
        return [str(f) for f in folders if isinstance(f, str)]

    def start_watching(self) -> LocalMusic:
        self.watcher.start()
        return self

    def add_watch_folder(self, folder: Path | str) -> list[str]:
        """NotADirectoryError if it isn't a folder."""
        folder = os.path.abspath(Path(folder).expanduser())
        if not os.path.isdir(folder):
            raise NotADirectoryError(folder)
        folders = self.watch_folders()
        if folder not in folders:
            self.settings.set("watch_folders", [*folders, folder])
        self.watcher.add(folder)
        return self.watch_folders()

    def remove_watch_folder(self, folder: Path | str) -> list[str]:
        """Stop watching it (KeyError if it isn't watched). Its tracks stay in the library."""
        folder = os.path.abspath(Path(folder).expanduser())
        folders = self.watch_folders()
        if folder not in folders:
            raise KeyError(folder)
        self.settings.set("watch_folders", [f for f in folders if f != folder])
        self.watcher.remove(folder)
        return self.watch_folders()

    def _watch_scan(self, folder: str, done: int, total: int, name: str) -> None:
        if done == total or done % 10 == 0:
            self._update({"state": "running", "kind": "watch", "folder": folder, "done": done, "total": total,
                          "file": name})  # fmt: skip

    def _watch_report(self, folder: str | None, report: ImportReport) -> None:
        if folder is None and not (report.added or report.removed or report.errors):
            return  # a file re-saved with the same audio: nothing to say
        self._update({"state": "done", "kind": "watch", "folder": folder, **report.to_json()})

    # --- imports ---------------------------------------------------------------------------------------------------

    def import_files(self, paths: list[Path | str]) -> dict:
        """Queue an import of these files and folders (dropped onto the window). Returns the queued status."""
        paths = [Path(p).expanduser() for p in paths]
        label = paths[0].name if len(paths) == 1 else f"{len(paths)} items"

        def run() -> dict:
            report = self.importer.import_paths(paths, progress=self._progress("files", label))
            return report.to_json()

        return self._submit("files", label, run)

    def import_xml(self, path: Path | str) -> dict:
        """Queue an import of a Rekordbox or iTunes/Music XML's playlists. NotAPlaylistFile/OSError at once if
        the file isn't one."""
        path = Path(path).expanduser()
        playlists_xml.parse(path)  # fail now, not in the background

        def run() -> dict:
            return playlists_xml.import_playlists(self.lib, self.importer, path,
                                                  progress=self._progress("xml", path.name)).to_json()  # fmt: skip

        return self._submit("xml", path.name, run)

    def upload_path(self, name: str) -> Path:
        """Where a file dropped in a browser (which sends no path) goes: the music folder's Imported folder, under
        its own name (with a number when that is taken)."""
        safe = "".join("_" if c in '<>:"/\\|?*\0' else c for c in Path(name).name).strip(" .") or "track"
        folder = self.music_dir / UPLOADS_SUBDIR
        folder.mkdir(parents=True, exist_ok=True)
        stem, suffix = Path(safe).stem, Path(safe).suffix
        dest, n = folder / safe, 1
        while dest.exists():
            n += 1
            dest = folder / f"{stem} ({n}){suffix}"
        return dest

    def _progress(self, kind: str, label: str) -> Callable[[int, int, str], None]:
        def progress(done: int, total: int, name: str) -> None:
            if done == total or done % 5 == 0:
                self._update({"state": "running", "kind": kind, "label": label, "done": done, "total": total,
                              "file": name})  # fmt: skip

        return progress

    def _submit(self, kind: str, label: str, run: Callable[[], dict]) -> dict:
        def task() -> None:
            self._update({"state": "running", "kind": kind, "label": label, "done": 0, "total": None})
            try:
                self._update({"state": "done", "kind": kind, "label": label, **run()})
            except Exception as e:  # noqa: BLE001
                log.exception("import of %s failed", label)
                self._update({"state": "failed", "kind": kind, "label": label, "error": f"{type(e).__name__}: {e}"})

        status = {"state": "queued", "kind": kind, "label": label}
        self._update(status)
        self._queue.submit(task)
        return status

    def _update(self, status: dict) -> None:
        with self._lock:
            self.status = status
        if self._publish is not None:
            self._publish(status)

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """True once the queued imports have run and the watch folders are quiet."""
        done = threading.Event()
        self._queue.submit(done.set)
        return done.wait(timeout) and self.watcher.wait_idle(timeout)

    def close(self) -> None:
        self.watcher.stop()
        self._queue.shutdown(wait=False, cancel_futures=True)
