"""Download jobs: pipeline.run_batch in a background thread with live SSE progress, then complete_batch.

One thread per job. Jobs for different crates run side by side; a second job for the same crate folder waits
(items stay "queued") until the first one is done, because a batch sweeps its folder's temp dirs on start.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from musicreco import pipeline
from musicreco.models import DownloadResult, Score, Track

from . import engine, serializers
from .events import EventBus

log = logging.getLogger(__name__)

FINAL_STATUSES = ("downloaded", "copied", "exists", "skipped", "failed")
MAX_FINISHED_JOBS = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobItem:
    track: Track
    text: str = "queued"
    status: str | None = None
    reason: str | None = None
    genre: str | None = None
    path: Path | None = None
    youtube_url: str | None = None

    def to_json(self) -> dict:
        return {
            "track": serializers.track(self.track),
            "text": self.text,
            "status": self.status,
            "reason": self.reason,
            "genre": self.genre,
            "path": str(self.path) if self.path else None,
            "youtube_url": self.youtube_url,
        }

    def apply(self, r: DownloadResult) -> None:
        cancelled = r.reason == pipeline.CANCELLED
        self.status = r.status
        self.text = "cancelled" if cancelled else r.status
        self.reason = "cancelled" if cancelled else r.reason
        self.genre = r.genre
        self.path = r.path
        self.youtube_url = r.youtube.url if r.youtube else None


@dataclass
class Job:
    job_id: str
    folder: str
    seed: Track
    recs: list[Track]
    scores: dict[str, Score]
    seed_dir: Path
    items: dict[str, JobItem]  # in download order (seed first, then recs best-first)
    state: str = "running"  # running | finalizing | done | cancelled
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def to_json(self) -> dict:
        with self.lock:
            return {
                "job_id": self.job_id,
                "folder": self.folder,
                "state": self.state,
                "items": [i.to_json() for i in self.items.values()],
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    @property
    def finished(self) -> bool:
        return self.finished_at is not None


class JobManager:
    """Starts, tracks and cancels download jobs. `on_finished(job)` runs (in the job thread) after each job."""

    def __init__(
        self,
        bus: EventBus,
        get_settings: Callable[[], Any],
        on_finished: Callable[[Job], None] | None = None,
    ) -> None:
        self.bus = bus
        self.get_settings = get_settings
        self.on_finished = on_finished
        self._jobs: dict[str, Job] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._folder_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._closed = False

    def start(
        self, seed: Track, recs: list[Track], scores: dict[str, Score], selected: list[Track], seed_dir: Path
    ) -> Job:
        """Queue a download of `selected` (already in download order) into `seed_dir`."""
        job = Job(
            job_id=uuid.uuid4().hex[:12],
            folder=seed_dir.name,
            seed=seed,
            recs=recs,
            scores=scores,
            seed_dir=seed_dir,
            items={t.id: JobItem(t) for t in selected},
        )
        thread = threading.Thread(target=self._run, args=(job,), name=f"seedbox-job-{job.job_id}", daemon=True)
        with self._lock:
            if self._closed:
                raise RuntimeError("server is shutting down")
            self._jobs[job.job_id] = job
            self._threads[job.job_id] = thread
            self._prune()
        self.bus.publish("job_state", {"job_id": job.job_id, "state": job.state})
        thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def busy(self, folder: str) -> bool:
        """True while a job for this crate folder is queued or running."""
        with self._lock:
            return any(j.folder == folder and not j.finished for j in self._jobs.values())

    def cancel(self, job_id: str) -> Job | None:
        """Stop queued tracks now and in-flight ones before their next network step (downloads finish)."""
        job = self.get(job_id)
        if job is not None and not job.finished:
            job.cancel.set()
        return job

    def shutdown(self, timeout: float = 3.0) -> None:
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
            threads = list(self._threads.values())
        for job in jobs:
            job.cancel.set()
        for t in threads:
            t.join(timeout / max(1, len(threads)))

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if j.finished]
        for j in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
            self._jobs.pop(j.job_id, None)
            self._threads.pop(j.job_id, None)

    def _folder_lock(self, folder: str) -> threading.Lock:
        with self._lock:
            return self._folder_locks.setdefault(folder, threading.Lock())

    def _set_state(self, job: Job, state: str) -> None:
        with job.lock:
            job.state = state
            if state in ("done", "cancelled"):
                job.finished_at = _now()
        self.bus.publish("job_state", {"job_id": job.job_id, "state": state})

    def _update(self, job: Job, track: Track, text: str) -> None:
        with job.lock:
            item = job.items.get(track.id)
            if item is None:
                return
            item.text = text
            if text in FINAL_STATUSES:
                item.status = text
            elif text == "cancelled":
                item.status, item.reason = "failed", "cancelled"
        self.bus.publish("job", {"job_id": job.job_id, "track_id": track.id, "text": text})

    def _run(self, job: Job) -> None:
        folder_lock = self._folder_lock(job.folder)
        acquired = False
        try:
            while not (acquired := folder_lock.acquire(timeout=0.25)):
                if job.cancel.is_set():
                    break
            if acquired and not job.cancel.is_set():
                self._download(job)
            else:
                for item in job.items.values():
                    self._update(job, item.track, "cancelled")
        except Exception as e:  # noqa: BLE001 - report, never kill the server
            log.exception("job %s failed", job.job_id)
            with job.lock:
                for item in job.items.values():
                    if item.status is None:
                        item.status, item.text, item.reason = "failed", "failed", f"job error: {e}"
            self.bus.toast("error", f"Download job for {job.folder} failed: {e}")
        finally:
            if acquired:
                folder_lock.release()
            self._set_state(job, "cancelled" if job.cancel.is_set() else "done")
            self.bus.publish("job_done", job.to_json())
            if self.on_finished is not None:
                try:
                    self.on_finished(job)
                except Exception:  # noqa: BLE001
                    log.exception("post-job hook failed for %s", job.job_id)

    def _download(self, job: Job) -> None:
        settings = self.get_settings()
        tracks = [i.track for i in job.items.values()]
        results = engine.run_batch(
            tracks,
            job.seed,
            job.seed_dir,
            workers=int(settings.workers),
            tolerance_s=float(settings.tolerance_s),
            on_update=lambda t, text: self._update(job, t, text),
            cancel=job.cancel,
        )
        with job.lock:
            for r in results:
                if item := job.items.get(r.track.id):
                    item.apply(r)
        self._set_state(job, "finalizing")
        # Runs even after a cancel: whatever did download gets rated, numbered, listed and written to the CSV.
        done = engine.complete_batch(job.seed, job.recs, job.scores, set(job.items), job.seed_dir, results,
                                     tol=float(settings.bpm_tolerance))  # fmt: skip
        if errors := getattr(done, "errors", None):
            self.bus.toast("warning", f"{job.folder}: couldn't write {'; '.join(errors)}")
