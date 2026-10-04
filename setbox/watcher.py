"""Crates change under Setbox: Seedbox (its own process) downloads into them and starts new ones. A pass every few
seconds costs one stat per crate folder (a folder's mtime moves when a file in it is added, removed or replaced, and
the crate's MP3s, tracks.csv and playlists are all written that way); only a folder that moved gets its crate
fingerprint (crates._signature) re-read, and a crate whose fingerprint changed is announced as `set_changed`, the
event the set builder and the crate list already refetch on. Folders modified in the last few seconds are re-read
on every pass too: on file systems with coarse timestamps (HFS+, FAT) a second change can keep the same mtime.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from seedbox.events import EventBus

from .crates import _signature

log = logging.getLogger(__name__)

INTERVAL_S = 3.0
RECENT_NS = 5_000_000_000  # re-read folders modified this recently on every pass


class CrateWatcher:
    def __init__(self, root: Callable[[], Path], bus: EventBus, interval_s: float = INTERVAL_S,
                 start: bool = True) -> None:  # fmt: skip
        self._root = root
        self.bus = bus
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[int, tuple]] | None = None  # folder -> (folder mtime_ns, crate fingerprint)
        self._seen_root: Path | None = None
        self._thread = threading.Thread(target=self._loop, name="setbox-crate-watcher", daemon=True)
        if start:
            self._thread.start()

    def forget(self) -> None:
        """The crates folder changed: the next pass takes a fresh baseline (announcing nothing)."""
        with self._lock:
            self._seen = None

    def shutdown(self) -> None:
        self._stop.set()

    def check(self) -> list[str]:
        """One pass: the folders whose crate changed since the last pass, each announced as set_changed. The first pass
        (and the first after the crates folder changes) only takes the baseline."""
        with self._lock:
            root = self._root()
            old = self._seen if self._seen_root == root else None
            now: dict[str, tuple[int, tuple]] = {}
            try:
                with os.scandir(root) as it:
                    entries = [e for e in it if not e.name.startswith(".") and e.is_dir()]
            except OSError:
                entries = []
            wall = time.time_ns()
            for e in entries:
                try:
                    mtime = e.stat().st_mtime_ns
                except OSError:
                    continue
                prev = old.get(e.name) if old else None
                same = prev is not None and prev[0] == mtime and wall - mtime > RECENT_NS
                now[e.name] = prev if same else (mtime, _signature(Path(e.path)))
            changed = [] if old is None else sorted(
                f for f in now.keys() | old.keys() if (f in now) != (f in old) or now[f][1] != old[f][1]
            )
            self._seen, self._seen_root = now, root
        for folder in changed:
            self.bus.publish("set_changed", {"folder": folder})
        return changed

    def _loop(self) -> None:
        while True:
            try:
                self.check()  # the first pass takes the baseline right away
            except Exception:  # noqa: BLE001 - keep watching
                log.exception("watching the crates folder failed")
            if self._stop.wait(self.interval_s):
                return
