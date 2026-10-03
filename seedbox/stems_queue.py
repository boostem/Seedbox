"""Background vocal stem splitting (musicreco.stems) for crate files, with SSE progress.

One worker drains a de-duplicated queue. Songs in use (an open transition, a setlist, a set mix) are queued at the
front, in order; the rest of a crate waits behind them and only starts while nothing is being rendered (`idle`).
The stem sidecars (loudness envelopes) are kept in a small memory LRU; the vocal FLACs stay on disk. enrich() hands
the planner analyses whose vocal_per_bar comes from the stems once a track is split.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import engine
from .events import EventBus

log = logging.getLogger(__name__)

MEMORY_CACHE = 128  # stem sidecars kept in memory
PROGRESS_STEP = 0.05
IDLE_POLL_S = 1.0
UNLOAD_AFTER_S = 300.0  # drop the vocal model from memory after this long without work


@dataclass
class StemTask:
    folder: str
    seed_dir: Path
    track_id: str
    path: Path

    @property
    def key(self) -> tuple[str, str]:
        return self.folder, self.track_id


class StemQueue:
    def __init__(self, bus: EventBus, idle: Callable[[], bool] = lambda: True, start: bool = True) -> None:
        self.bus = bus
        self.idle = idle
        self._cond = threading.Condition()
        self._front: list[StemTask] = []
        self._back: list[StemTask] = []
        self._states: dict[tuple[str, str], str] = {}  # queued | running | ready | error
        self._progress: dict[tuple[str, str], float] = {}
        self._errors: dict[tuple[str, str], str] = {}
        self._metas: OrderedDict[tuple[str, str], tuple[int, dict]] = OrderedDict()  # key -> (json mtime, meta)
        self._bars: dict[tuple, list[float] | None] = {}
        self._cancel = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._worker, name="seedbox-stems", daemon=True)
        if start:
            self._thread.start()

    # --- availability / state -------------------------------------------------------------------------------------

    @staticmethod
    def module():  # noqa: ANN205
        return engine.optional("stems")

    def available(self) -> tuple[bool, str]:
        stems = self.module()
        if stems is None:
            return False, "vocal stems aren't part of this build"
        try:
            return stems.available()
        except Exception as e:  # noqa: BLE001 - a broken optional dependency must not break the app
            return False, f"vocal stems can't load: {e}"

    def state(self, folder: str, track_id: str) -> str:
        """missing | queued | running | ready | error."""
        with self._cond:
            return self._states.get((folder, track_id), "missing")

    def status(self, seed_dir: Path, files: list) -> dict:
        """StemsStatus (types.ts) for a crate's files: availability and each track's state / progress."""
        ok, why = self.available()
        tracks = {}
        for f in files:
            tid = f.track.id
            st = self.state(seed_dir.name, tid)
            if st in ("missing", "queued") and self.meta(seed_dir, tid, f.path) is not None:
                st = "ready"
            with self._cond:
                tracks[tid] = {"state": st, "progress": round(self._progress.get((seed_dir.name, tid), 0.0), 3),
                               "error": self._errors.get((seed_dir.name, tid))}
        return {"available": ok, "reason": None if ok else why, "tracks": tracks,
                "ready": sum(1 for t in tracks.values() if t["state"] == "ready"), "total": len(tracks)}

    def pending(self) -> int:
        with self._cond:
            return len(self._front) + len(self._back) + sum(1 for v in self._states.values() if v == "running")

    # --- stems on disk --------------------------------------------------------------------------------------------

    def meta(self, seed_dir: Path, track_id: str, path: Path) -> dict | None:
        """The track's stem sidecar (memory, or disk when it still matches the file), no splitting."""
        stems = self.module()
        if stems is None:
            return None
        k = (seed_dir.name, track_id)
        meta_path = stems.paths(seed_dir, track_id)[1]
        try:
            mt = meta_path.stat().st_mtime_ns
        except OSError:
            return None
        with self._cond:
            hit = self._metas.get(k)
            if hit is not None and hit[0] == mt:
                self._metas.move_to_end(k)
                return hit[1]
        try:
            data = stems.load(seed_dir, track_id, path)
        except Exception:  # noqa: BLE001
            log.exception("reading the stems of %s failed", track_id)
            data = None
        if data is None:
            return None
        with self._cond:
            self._metas[k] = (mt, data)
            self._metas.move_to_end(k)
            while len(self._metas) > MEMORY_CACHE:
                self._metas.popitem(last=False)
        return data

    def vocal_path(self, seed_dir: Path, track_id: str, path: Path) -> Path | None:
        """The track's vocal stem FLAC when it's split and current."""
        if self.meta(seed_dir, track_id, path) is None:
            return None
        return self.module().paths(seed_dir, track_id)[0]

    def enrich(self, seed_dir: Path, track_id: str, path: Path, an: dict | None) -> dict | None:
        """The analysis with its vocal_per_bar from the stems once the track is split (the analysis itself before)."""
        if not isinstance(an, dict):
            return an
        meta = self.meta(seed_dir, track_id, path)
        if meta is None:
            return an
        grid = (seed_dir.name, track_id, an.get("first_downbeat_s"), an.get("bar_s"), an.get("bar_count"),
                len(meta.get("vocal_db") or ()), (meta.get("key") or {}).get("mtime"))
        with self._cond:
            per_bar = self._bars.get(grid, ...)
        if per_bar is ...:
            per_bar = self.module().vocal_per_bar(meta, an)
            with self._cond:
                if len(self._bars) > 4 * MEMORY_CACHE:
                    self._bars.clear()
                self._bars[grid] = per_bar
        if per_bar is None:
            return an
        return {**an, "vocal_per_bar": per_bar, "vocal_per_bar_heuristic": an.get("vocal_per_bar"),
                "vocal_source": "stems"}

    # --- queue ----------------------------------------------------------------------------------------------------

    def enqueue(self, tasks: list[StemTask], front: bool = False) -> None:
        """Queue tasks not already split / running / failed this session. `front` moves them (in order) ahead of
        everything else; background tasks only start while the app is idle."""
        if not tasks or not self.available()[0]:
            return
        queued: list[StemTask] = []
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
                    self._front = [t for t in self._front if t.key != task.key]
                    self._back = [t for t in self._back if t.key != task.key]
                queued.append(task)
                self._states[task.key] = "queued"
            if front:
                self._front[:0] = queued
            else:
                self._back.extend(queued)
            self._cond.notify_all()
        for task in queued:
            self._publish(task, "queued", 0.0)

    def forget(self, folder: str | None = None) -> None:
        """Drop remembered states (all, or one folder's) and their queued work."""
        with self._cond:
            keep = (lambda k: k[0] != folder) if folder is not None else (lambda k: False)
            self._front = [t for t in self._front if keep(t.key)]
            self._back = [t for t in self._back if keep(t.key)]
            self._states = {k: v for k, v in self._states.items() if keep(k) or v == "running"}
            self._errors = {k: v for k, v in self._errors.items() if keep(k)}
            self._metas = OrderedDict((k, v) for k, v in self._metas.items() if keep(k))

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._closed = True
            self._front.clear()
            self._back.clear()
            self._cond.notify_all()
        self._cancel.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _publish(self, task: StemTask, state: str, progress: float, error: str | None = None) -> None:
        body = {"folder": task.folder, "track_id": task.track_id, "state": state, "progress": round(progress, 3)}
        if error:
            body["error"] = error
        self.bus.publish("stems", body)

    def _next(self) -> StemTask | None:
        """The next task: the front queue first; a background one only while the app is idle."""
        while True:
            with self._cond:
                if self._closed:
                    return None
                if self._front:
                    return self._front.pop(0)
                if self._back:
                    break
                timed_out = not self._cond.wait(UNLOAD_AFTER_S)
            if timed_out:  # nothing to do for a while: free the model's memory (outside the lock)
                self._unload()
        while True:  # background work waits for the app to be idle (renders take the CPU first)
            try:
                idle = bool(self.idle())
            except Exception:  # noqa: BLE001
                idle = True
            with self._cond:
                if self._closed:
                    return None
                if self._front:
                    return self._front.pop(0)
                if idle and self._back:
                    return self._back.pop(0)
                if not self._back:
                    return None
                self._cond.wait(IDLE_POLL_S)

    def _unload(self) -> None:
        stems = self.module()
        if stems is not None:
            try:
                stems.unload_model(UNLOAD_AFTER_S)
            except Exception:  # noqa: BLE001
                log.exception("unloading the vocal model failed")

    def _worker(self) -> None:
        while True:
            task = self._next()
            if task is None:
                with self._cond:
                    if self._closed:
                        return
                continue
            self._run(task)

    def _run(self, task: StemTask) -> None:
        stems = self.module()
        if stems is None:
            with self._cond:
                self._states.pop(task.key, None)
            return
        if self.meta(task.seed_dir, task.track_id, task.path) is not None:
            with self._cond:
                self._states[task.key] = "ready"
            self._publish(task, "ready", 1.0)
            return
        with self._cond:
            self._states[task.key] = "running"
            self._progress[task.key] = 0.0
        self._publish(task, "running", 0.0)
        last = [0.0]

        def progress(f: float) -> None:
            with self._cond:
                self._progress[task.key] = f
            if f - last[0] >= PROGRESS_STEP:
                last[0] = f
                self._publish(task, "running", f)

        t0 = time.monotonic()
        try:
            stems.split(task.seed_dir, task.track_id, task.path, progress=progress, cancel=self._cancel)
        except Exception as e:  # noqa: BLE001 - bad audio, model download failed, cancelled...
            if isinstance(e, stems.Cancelled):
                with self._cond:
                    self._states.pop(task.key, None)
                return
            msg = str(e) or type(e).__name__
            log.warning("splitting the vocals of %s failed: %s", task.path.name, msg)
            with self._cond:
                self._states[task.key] = "error"
                self._errors[task.key] = msg
            self._publish(task, "error", last[0], msg)
            return
        log.info("split the vocals of %s in %.0f s", task.path.name, time.monotonic() - t0)
        with self._cond:
            self._states[task.key] = "ready"
            self._progress[task.key] = 1.0
            self._errors.pop(task.key, None)
        self._publish(task, "ready", 1.0)
