"""Background structure analysis (musicreco.structure.load_or_analyze) for crate files, with SSE progress.

1-2 worker threads drain a de-duplicated queue; an opened set's tracks jump the queue (front=True). Results are
kept in a small in-memory LRU (the structure module keeps the on-disk cache in <crate>/.seedbox/analysis/).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from . import engine
from .events import EventBus

log = logging.getLogger(__name__)

MEMORY_CACHE = 64  # full analyses (waveforms included) kept in memory


@dataclass
class AnalysisTask:
    folder: str
    seed_dir: Path
    track_id: str
    path: Path
    bpm_hint: float | None

    @property
    def key(self) -> tuple[str, str]:
        return self.folder, self.track_id


def default_workers() -> int:
    return 2 if (os.cpu_count() or 1) >= 8 else 1


def disk_cache_path(seed_dir: Path, track_id: str) -> Path:
    """Where structure.load_or_analyze keeps a track's analysis (APP_SPEC)."""
    return seed_dir / ".seedbox" / "analysis" / f"{track_id}.json"


class AnalysisQueue:
    def __init__(self, bus: EventBus, workers: int | None = None, memory_cache: int = MEMORY_CACHE) -> None:
        self.bus = bus
        self._cond = threading.Condition()
        self._queue: list[AnalysisTask] = []
        self._states: dict[tuple[str, str], str] = {}  # queued | running | ready | error
        self._errors: dict[tuple[str, str], str] = {}
        self._results: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._memory_cache = memory_cache
        self._closed = False
        self._threads = [
            threading.Thread(target=self._worker, name=f"seedbox-analysis-{n}", daemon=True)
            for n in range(workers or default_workers())
        ]
        for t in self._threads:
            t.start()

    @staticmethod
    def available() -> bool:
        return engine.optional("structure") is not None

    def state(self, folder: str, track_id: str) -> str:
        """missing | queued | running | ready | error."""
        with self._cond:
            return self._states.get((folder, track_id), "missing")

    def error(self, folder: str, track_id: str) -> str | None:
        with self._cond:
            return self._errors.get((folder, track_id))

    def cached(self, folder: str, track_id: str) -> dict | None:
        with self._cond:
            hit = self._results.get((folder, track_id))
            if hit is not None:
                self._results.move_to_end((folder, track_id))
            return hit

    def enqueue(self, tasks: list[AnalysisTask], front: bool = False) -> None:
        """Queue tasks not already ready/running/failed; `front` moves them (in order) ahead of everything else."""
        if not tasks or not self.available():
            return
        queued: list[AnalysisTask] = []
        with self._cond:
            if self._closed:
                return
            for task in tasks:
                st = self._states.get(task.key)
                if st in ("running", "ready", "error"):
                    continue
                if st == "queued":
                    if not front:
                        continue
                    self._queue = [t for t in self._queue if t.key != task.key]
                queued.append(task)
                self._states[task.key] = "queued"
            if front:
                self._queue[:0] = queued
            else:
                self._queue.extend(queued)
            self._cond.notify_all()
        for task in queued:
            self._publish(task, "queued")

    def load_now(self, task: AnalysisTask) -> dict:
        """Analyse (or load from the disk cache) on the calling thread; records the result like a worker would."""
        structure = engine.optional("structure")
        if structure is None:
            raise RuntimeError("analysis module not available")
        return self._analyse(structure, task)

    def forget(self, folder: str | None = None) -> None:
        """Drop remembered states/results (all, or one folder's); queued work for them is dropped too."""
        with self._cond:
            keep = (lambda k: k[0] != folder) if folder is not None else (lambda k: False)
            self._queue = [t for t in self._queue if keep(t.key)]
            self._states = {k: v for k, v in self._states.items() if keep(k) or v == "running"}
            self._errors = {k: v for k, v in self._errors.items() if keep(k)}
            self._results = OrderedDict((k, v) for k, v in self._results.items() if keep(k))

    def pending(self) -> int:
        with self._cond:
            return len(self._queue) + sum(1 for v in self._states.values() if v == "running")

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._closed = True
            self._queue.clear()
            self._cond.notify_all()
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))

    def _publish(self, task: AnalysisTask, state: str) -> None:
        self.bus.publish("analysis", {"folder": task.folder, "track_id": task.track_id, "state": state})

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                task = self._queue.pop(0)
            structure = engine.optional("structure")
            if structure is None:
                with self._cond:
                    self._states.pop(task.key, None)
                continue
            try:
                self._analyse(structure, task)
            except Exception:  # noqa: BLE001 - recorded as an error state by _analyse
                pass

    def _analyse(self, structure, task: AnalysisTask) -> dict:  # noqa: ANN001
        with self._cond:
            self._states[task.key] = "running"
        self._publish(task, "running")
        try:
            result = structure.load_or_analyze(task.seed_dir, task.track_id, task.path, task.bpm_hint)
        except Exception as e:  # noqa: BLE001 - bad audio etc.: surface as an error state
            msg = str(e) or type(e).__name__
            log.warning("analysis of %s failed: %s", task.path.name, msg)
            with self._cond:
                self._states[task.key] = "error"
                self._errors[task.key] = msg
            self._publish(task, "error")
            raise
        with self._cond:
            self._states[task.key] = "ready"
            self._errors.pop(task.key, None)
            self._results[task.key] = result
            self._results.move_to_end(task.key)
            while len(self._results) > self._memory_cache:
                old, _ = self._results.popitem(last=False)
                if self._states.get(old) == "ready":
                    self._states.pop(old, None)  # unknown again: next request reloads it from the disk cache
        self._publish(task, "ready")
        return result
