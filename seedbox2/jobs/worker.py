"""The worker process: receives one job at a time over a pipe, runs it, reports progress and the outcome.

Messages, supervisor -> worker: `("run", job_id, target, payload)`, or None to exit.
Worker -> supervisor: `("progress", job_id, fraction, message)`, `("done", job_id, result_json)`,
`("failed", job_id, error)`, `("cancelled", job_id)`.
"""

from __future__ import annotations

import json
import logging
import signal
import time
import traceback
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from seedbox2.jobs.registry import resolve

log = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 0.05  # at most this often over the pipe (the event bus coalesces further)


class Cancelled(Exception):  # noqa: N818 - raised inside a job to stop it
    """The job was cancelled. Raised by ctx.progress() and ctx.check(); let it propagate."""


class JobContext:
    """What a job function gets besides its payload."""

    def __init__(self, job_id: str, conn: Connection, cancel: Any, cache: dict, library_path: Path | None) -> None:
        self.job_id = job_id
        self._conn = conn
        self._cancel = cancel
        self._cache = cache
        self._library_path = library_path
        self._last_sent = 0.0

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        """Raise Cancelled if the job was cancelled. Call it now and then in long loops."""
        if self._cancel.is_set():
            raise Cancelled

    def progress(self, fraction: float, message: str | None = None) -> None:
        """Report progress (0..1); raises Cancelled if the job was cancelled."""
        self.check()
        now = time.monotonic()
        if now - self._last_sent >= PROGRESS_INTERVAL_S or fraction >= 1:
            self._last_sent = now
            self._conn.send(("progress", self.job_id, max(0.0, min(1.0, float(fraction))), message))

    def cached(self, key: str, factory: Callable[[], Any]) -> Any:
        """A per-process object built once and reused by every later job in this worker (ONNX sessions, models)."""
        if key not in self._cache:
            self._cache[key] = factory()
        return self._cache[key]

    @property
    def library(self) -> Any:
        """This process's own connection to the library (opened on first use), or None without one."""
        if self._library_path is None:
            return None

        def open_library() -> Any:
            from seedbox2.library import Library

            return Library.open(self._library_path)

        return self.cached("seedbox2.library", open_library)


def worker_main(conn: Connection, cancel: Any, pool: str, library_path: str | None = None) -> None:
    """A worker process's loop (the spawn target)."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C in a terminal: the supervisor decides when workers stop
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {pool} worker %(levelname)s %(name)s: %(message)s")
    functions: dict[str, Callable[..., Any]] = {}
    cache: dict[str, Any] = {}
    path = Path(library_path) if library_path else None
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):  # the supervisor is gone
            return
        if msg is None:
            return
        _, job_id, target, payload = msg
        ctx = JobContext(job_id, conn, cancel, cache, path)
        try:
            fn = functions.get(target)
            if fn is None:
                fn = functions[target] = resolve(target)
            result = fn(payload, ctx)
            reply: tuple = ("done", job_id, None if result is None else json.dumps(result))
        except Cancelled:
            reply = ("cancelled", job_id)
        except Exception as e:  # noqa: BLE001 - reported as the job's error
            log.warning("job %s (%s) failed:\n%s", job_id, target, traceback.format_exc())
            reply = ("failed", job_id, f"{type(e).__name__}: {e}")
        try:
            conn.send(reply)
        except (EOFError, OSError):
            return
