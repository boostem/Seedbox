"""The supervisor: owns the job queue and the worker processes, in the server process.

- Pools: `heavy` (1 worker: stems, full renders) and `light` (cores - 2, at least 1). Workers are spawned
  processes, started when their pool first has work and kept for later jobs (their cached ONNX sessions with them).
- Queue: highest priority first, then oldest. `PRIORITY` gives the defaults by what the job is for (the open set,
  the open crate, the library); `bump` moves queued jobs to the front when the UI opens what they are about.
- State: every job is a row in the library's `jobs` table. On start, jobs left `running` by a server that stopped
  are queued again (resumable types) or marked failed; queued ones carry on.
- Cancel: a queued job is dropped; a running one is asked to stop (ctx.progress/ctx.check raise Cancelled in the
  worker) and its worker is killed and replaced if it hasn't stopped after `cancel_grace_s`.
- A worker that dies (a crash in native code, out of memory) fails its job and is replaced; the server carries on.
- RAM guard: no heavy job starts while the machine has less than `HEAVY_MIN_AVAILABLE` bytes available.
- Events (on the EventBus): `job.queued`, `job.started`, `job.progress` (coalesced per job), `job.bumped`, and
  the terminal `job.done`, `job.failed`, `job.cancelled` (never dropped).

One dispatcher thread talks to the workers; the public methods are thread-safe.
"""

from __future__ import annotations

import heapq
import itertools
import json
import logging
import multiprocessing
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from typing import Any

from seedbox2.jobs.registry import REGISTRY, Registry, UnknownJobType
from seedbox2.jobs.worker import worker_main

log = logging.getLogger(__name__)

# default priorities by what the job is for: what the user is looking at first
PRIORITY = {"set": 300, "crate": 200, "library": 100, "background": 0}
HEAVY_MIN_AVAILABLE = int(1.5 * 1024**3)  # bytes of free RAM a heavy job needs before it starts
RAM_RECHECK_S = 1.0
CANCEL_GRACE_S = 2.0
PROGRESS_PERSIST_S = 1.0  # progress is written to the database at most this often per job
TERMINAL = ("done", "failed", "cancelled")


def light_workers_default() -> int:
    return max(1, (os.cpu_count() or 1) - 2)


class RamGuard:
    """`ok()` while at least `min_available` bytes of RAM are available (psutil)."""

    def __init__(self, min_available: int = HEAVY_MIN_AVAILABLE, available: Callable[[], int] | None = None) -> None:
        self.min_available = min_available
        self._available = available

    def available(self) -> int:
        if self._available is not None:
            return self._available()
        import psutil

        return int(psutil.virtual_memory().available)

    def ok(self) -> bool:
        return self.available() >= self.min_available


@dataclass
class _Job:
    id: str
    kind: str
    pool: str
    priority: int
    seq: int
    payload: dict
    key: str  # kind + payload, to find a duplicate submission
    state: str = "queued"
    progress: float = 0.0
    persisted_at: float = 0.0
    cancel_requested: bool = False


@dataclass
class _Worker:
    pool: str
    process: Any
    conn: Connection
    cancel: Any  # multiprocessing Event: set to cancel the running job
    job: _Job | None = None
    kill_at: float | None = None  # cancel deadline of the running job
    dead: bool = False


@dataclass
class _Pool:
    name: str
    size: int
    heap: list = field(default_factory=list)  # (-priority, seq, job_id); stale entries are skipped
    workers: list[_Worker] = field(default_factory=list)


class Supervisor:
    def __init__(self, library: Any, events: Any = None, *, registry: Registry = REGISTRY,
                 light_workers: int | None = None, heavy_workers: int = 1, ram_guard: RamGuard | None = None,
                 cancel_grace_s: float = CANCEL_GRACE_S) -> None:  # fmt: skip
        self.library = library
        self.events = events
        self.registry = registry
        self.ram_guard = ram_guard or RamGuard()
        self.cancel_grace_s = cancel_grace_s
        self._mp = multiprocessing.get_context("spawn")
        self._pools = {"heavy": _Pool("heavy", max(1, heavy_workers)),
                       "light": _Pool("light", light_workers or light_workers_default())}  # fmt: skip
        self._queued: dict[str, _Job] = {}
        self._running: dict[str, _Job] = {}
        self._by_key: dict[str, str] = {}  # key -> id of the queued or running job
        self._seq = itertools.count()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._wake_r, self._wake_w = multiprocessing.Pipe(duplex=False)
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.waiting_for_ram = False
        self._listeners: list[Callable[[dict], None]] = []

    # --- lifecycle -------------------------------------------------------------------------------------------------

    def start(self) -> Supervisor:
        """Recover the jobs a previous run left behind, then start dispatching."""
        with self._lock:
            if self._thread is not None:
                return self
            self.recover()
            self._thread = threading.Thread(target=self._run, name="seedbox2-jobs", daemon=True)
            self._thread.start()
        return self

    def recover(self) -> dict:
        """Queue again the jobs left running (resumable types; the others fail) and load the queued ones."""
        resumed = failed = 0
        for row in self.library.jobs("running"):
            job_type = self.registry.get(row["kind"]) if row["kind"] in self.registry else None
            if job_type is not None and job_type.resumable:
                self.library.update_job(row["id"], state="queued", progress=0.0)
                resumed += 1
            else:
                self.library.update_job(row["id"], state="failed", error="interrupted: Seedbox 2 stopped while it ran")
                failed += 1
        for row in self.library.jobs("queued"):  # in queue order, so `seq` keeps the order of equal priorities
            if row["kind"] not in self.registry:
                self.library.update_job(row["id"], state="failed", error=f"unknown job type {row['kind']!r}")
                failed += 1
                continue
            self._enqueue(self._job_from_row(row))
        if resumed or failed:
            log.info("jobs after restart: %d resumed, %d failed, %d queued", resumed, failed, len(self._queued))
        return {"resumed": resumed, "failed": failed, "queued": len(self._queued)}

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the workers (running jobs stay `running` in the database and are recovered on the next start)."""
        with self._lock:
            self._stopping = True
            thread = self._thread
        self._wake()
        if thread is not None:
            thread.join(timeout)

    # --- the public API --------------------------------------------------------------------------------------------

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        """Call `fn(row)` on the dispatcher thread after each job ends (done, failed or cancelled): keep it short.
        What a job leads to (stems -> their waveform rows) goes here, since a worker can't reach the queue."""
        self._listeners.append(fn)

    def submit(self, kind: str, payload: dict | None = None, *, priority: int | str = "library",
               front: bool = False) -> dict:  # fmt: skip
        """Queue a job (UnknownJobType if no such type) and return its row. The same kind and payload as a job
        already queued or running returns that job (raised to this priority if that is higher)."""
        job_type = self.registry.get(kind)
        payload = dict(payload or {})
        prio = PRIORITY[priority] if isinstance(priority, str) else int(priority)
        key = kind + "\0" + json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                if front:
                    self.bump([existing])
                elif existing in self._queued and prio > self._queued[existing].priority:
                    self._reprioritise([self._queued[existing]], prio)
                return self.library.job(existing)
            if front:
                prio = max(prio, self._top_priority() + 1)
            row = self.library.add_job(kind, payload, priority=prio)
            job = _Job(row["id"], kind, job_type.pool, prio, next(self._seq), payload, key)
            self._enqueue(job)
        self._publish("job.queued", row)
        self._wake()
        return row

    def cancel(self, job_id: str) -> dict | None:
        """Cancel a queued or running job. Returns its row (None if there is no such job); finished jobs stay as
        they are. A running job reaches `cancelled` once its worker has stopped it (see `wait`)."""
        with self._lock:
            job = self._queued.pop(job_id, None)
            if job is not None:
                self._forget(job)
                row = self.library.update_job(job_id, state="cancelled")
                self._changed.notify_all()
            else:
                job = self._running.get(job_id)
                if job is not None and not job.cancel_requested:
                    job.cancel_requested = True
                    for worker in self._pools[job.pool].workers:
                        if worker.job is job:
                            worker.cancel.set()
                            worker.kill_at = time.monotonic() + self.cancel_grace_s
                    self._wake()
                return self.library.job(job_id)
        self._publish("job.cancelled", row, terminal=True)
        return row

    def bump(self, ids: Iterable[str] = (), hashes: Iterable[str] = ()) -> list[str]:
        """Move these queued jobs, and the queued jobs about these tracks (payload `hash` or `hashes`), to the
        front of their queues, keeping their order. Returns the ids moved."""
        ids, hashes = set(ids), set(hashes)
        with self._lock:
            jobs = [j for j in self._queued.values() if j.id in ids or _about(j.payload, hashes)]
            if not jobs:
                return []
            top = self._top_priority() + 1
            self._reprioritise(jobs, top)
            moved = [j.id for j in sorted(jobs, key=lambda j: j.seq)]
        self._publish("job.bumped", {"ids": moved, "priority": top})
        return moved

    def job(self, job_id: str) -> dict | None:
        return self.library.job(job_id)

    def jobs(self, state: str | None = None) -> list[dict]:
        return self.library.jobs(state)

    def wait(self, job_id: str, timeout: float | None = None) -> dict | None:
        """Block until the job has finished (done, failed or cancelled) or `timeout`; returns its row."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while (job_id in self._queued or job_id in self._running) and not self._stopping:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    break
                self._changed.wait(left)
        return self.library.job(job_id)

    def status(self) -> dict:
        with self._lock:
            pools = {name: {"workers": len(p.workers), "size": p.size,
                            "busy": sum(w.job is not None for w in p.workers),
                            "queued": sum(j.pool == name for j in self._queued.values())}
                     for name, p in self._pools.items()}  # fmt: skip
            return {"pools": pools, "running": len(self._running), "queued": len(self._queued),
                    "waiting_for_ram": self.waiting_for_ram, "min_available_ram": self.ram_guard.min_available}

    # --- queue -----------------------------------------------------------------------------------------------------

    def _job_from_row(self, row: dict) -> _Job:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        key = row["kind"] + "\0" + json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return _Job(row["id"], row["kind"], self.registry.get(row["kind"]).pool, row["priority"], next(self._seq),
                    payload, key)  # fmt: skip

    def _enqueue(self, job: _Job) -> None:
        self._queued[job.id] = job
        self._by_key[job.key] = job.id
        heapq.heappush(self._pools[job.pool].heap, (-job.priority, job.seq, job.id))

    def _forget(self, job: _Job) -> None:
        if self._by_key.get(job.key) == job.id:
            del self._by_key[job.key]

    def _top_priority(self) -> int:
        return max((j.priority for j in self._queued.values()), default=0)

    def _reprioritise(self, jobs: list[_Job], priority: int) -> None:
        self.library.set_jobs_priority([j.id for j in jobs], priority)
        for j in jobs:
            j.priority = priority
            heapq.heappush(self._pools[j.pool].heap, (-priority, j.seq, j.id))

    def _pop(self, pool: _Pool) -> _Job | None:
        while pool.heap:
            neg_prio, _, job_id = heapq.heappop(pool.heap)
            job = self._queued.get(job_id)
            if job is not None and job.priority == -neg_prio:  # else cancelled or bumped (a newer entry exists)
                del self._queued[job_id]
                return job
        return None

    def _peek(self, pool: _Pool) -> bool:
        while pool.heap:
            neg_prio, _, job_id = pool.heap[0]
            job = self._queued.get(job_id)
            if job is not None and job.priority == -neg_prio:
                return True
            heapq.heappop(pool.heap)
        return False

    # --- the dispatcher thread -------------------------------------------------------------------------------------

    def _wake(self) -> None:
        try:
            self._wake_w.send_bytes(b"")
        except OSError:
            pass

    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._stopping:
                        break
                self._dispatch()
                self._loop_once()
        except Exception:
            log.exception("job dispatcher crashed")
        finally:
            self._shutdown_workers()

    def _loop_once(self) -> None:
        workers = [w for p in self._pools.values() for w in p.workers]
        handles: list[Any] = [self._wake_r]
        for w in workers:
            handles += [w.conn, w.process.sentinel]
        timeout = None
        deadlines = [w.kill_at for w in workers if w.kill_at is not None]
        if deadlines:
            timeout = max(0.0, min(deadlines) - time.monotonic())
        if self.waiting_for_ram:
            timeout = RAM_RECHECK_S if timeout is None else min(timeout, RAM_RECHECK_S)
        ready = wait(handles, timeout)
        if self._wake_r in ready:
            while self._wake_r.poll():
                self._wake_r.recv_bytes()
        for w in workers:
            if w.conn in ready:
                self._read(w)
            if not w.dead and (w.process.sentinel in ready or not w.process.is_alive()):
                self._read(w)  # whatever it sent before it died
                if not w.dead:
                    w.process.join(1)  # the sentinel can fire before the process is reaped (exitcode still None)
                    self._worker_died(w, f"worker process crashed (exit code {w.process.exitcode})")
            if not w.dead and w.kill_at is not None and time.monotonic() >= w.kill_at:
                log.info("job %s didn't stop within %.1fs of cancel: killing its worker", w.job and w.job.id,
                         self.cancel_grace_s)  # fmt: skip
                w.process.kill()
                w.process.join(5)
                self._worker_died(w, "cancelled")

    def _dispatch(self) -> None:
        for pool in self._pools.values():
            while True:
                with self._lock:
                    if self._stopping or not self._peek(pool):
                        break
                    idle = next((w for w in pool.workers if w.job is None), None)
                    if idle is None and len(pool.workers) >= pool.size:
                        break
                    if pool.name == "heavy":
                        ok = self.ram_guard.ok()
                        if ok != (not self.waiting_for_ram):
                            log.info("heavy jobs %s", "resume: enough free RAM" if ok else
                                     f"wait: less than {self.ram_guard.min_available / 1024**3:.1f} GB RAM available")
                        self.waiting_for_ram = not ok
                        if not ok:
                            break
                    job = self._pop(pool)
                    assert job is not None
                    job.state = "running"
                    self._running[job.id] = job
                    row = self.library.update_job(job.id, state="running", progress=0.0)
                if idle is None:
                    idle = self._spawn(pool)
                with self._lock:
                    idle.job, idle.kill_at = job, None
                    idle.cancel.clear()
                    if job.cancel_requested:  # cancelled between leaving the queue and reaching a worker
                        idle.cancel.set()
                        idle.kill_at = time.monotonic() + self.cancel_grace_s
                try:
                    idle.conn.send(("run", job.id, self.registry.get(job.kind).target, job.payload))
                except (OSError, ValueError) as e:
                    self._worker_died(idle, f"could not reach the worker: {e}")
                    continue
                self._publish("job.started", row)
            if pool.name == "heavy" and not self._peek_locked(pool):
                self.waiting_for_ram = False

    def _peek_locked(self, pool: _Pool) -> bool:
        with self._lock:
            return self._peek(pool)

    def _spawn(self, pool: _Pool) -> _Worker:
        parent, child = self._mp.Pipe()
        cancel = self._mp.Event()
        library_path = getattr(getattr(self.library, "db", None), "path", None)
        args = (child, cancel, pool.name, str(library_path or ""), pool.size)
        process = self._mp.Process(target=worker_main, args=args,
                                   name=f"seedbox2-{pool.name}-worker", daemon=True)  # fmt: skip
        process.start()
        child.close()
        worker = _Worker(pool.name, process, parent, cancel)
        with self._lock:
            pool.workers.append(worker)
        log.info("started %s worker (pid %s)", pool.name, process.pid)
        return worker

    def _read(self, w: _Worker) -> None:
        try:
            while not w.dead and w.conn.poll():
                self._handle(w, w.conn.recv())
        except (EOFError, OSError):
            pass  # the sentinel tells us it died

    def _handle(self, w: _Worker, msg: tuple) -> None:
        kind, job_id = msg[0], msg[1]
        job = w.job
        if job is None or job.id != job_id:
            return
        if kind == "progress":
            job.progress = msg[2]
            now = time.monotonic()
            if now - job.persisted_at >= PROGRESS_PERSIST_S:
                job.persisted_at = now
                self.library.update_job(job.id, progress=job.progress)
            self._publish("job.progress", {"id": job.id, "kind": job.kind, "hash": job.payload.get("hash"),
                                           "progress": job.progress, "message": msg[3]},
                          key=f"job.progress:{job.id}")  # fmt: skip
            return
        if kind == "done":
            result = json.loads(msg[2]) if msg[2] is not None else None
            self._finish(w, "done", progress=1.0, result=result)
        elif kind == "failed":
            self._finish(w, "failed", error=msg[2])
        elif kind == "cancelled":
            self._finish(w, "cancelled")

    def _finish(self, w: _Worker, state: str, **fields: Any) -> None:
        job = w.job
        assert job is not None
        w.job, w.kill_at = None, None
        with self._lock:
            self._running.pop(job.id, None)
            self._forget(job)
            job.state = state
            row = self.library.update_job(job.id, state=state, **fields)
            self._changed.notify_all()
        self._publish(f"job.{state}", row, terminal=True)
        for fn in self._listeners:
            try:
                fn(row)
            except Exception:
                log.exception("job listener failed on %s", job.id)

    def _worker_died(self, w: _Worker, reason: str) -> None:
        w.dead = True
        with self._lock:
            pool = self._pools[w.pool]
            if w in pool.workers:
                pool.workers.remove(w)
        if w.job is not None:
            if w.job.cancel_requested:
                self._finish(w, "cancelled")
            else:
                log.warning("job %s (%s): %s", w.job.id, w.job.kind, reason)
                self._finish(w, "failed", error=reason)
        try:
            w.conn.close()
        except OSError:
            pass

    def _shutdown_workers(self) -> None:
        workers = [w for p in self._pools.values() for w in p.workers]
        for w in workers:
            try:
                w.conn.send(None)
            except (OSError, ValueError):
                pass
        deadline = time.monotonic() + 2.0
        for w in workers:
            if w.job is not None:  # a running job: it would only finish after we stop listening
                w.process.kill()
            w.process.join(max(0.0, deadline - time.monotonic()))
            if w.process.is_alive():
                w.process.kill()
                w.process.join(1)
            try:
                w.conn.close()
            except OSError:
                pass
        with self._lock:
            for p in self._pools.values():
                p.workers.clear()
            self._changed.notify_all()

    def _publish(self, type_: str, data: Any, *, key: str | None = None, terminal: bool = False) -> None:
        if self.events is not None:
            self.events.publish(type_, data, key=key, terminal=terminal)


def _about(payload: dict, hashes: set[str]) -> bool:
    if not hashes:
        return False
    if payload.get("hash") in hashes:
        return True
    many = payload.get("hashes")
    return isinstance(many, list) and any(h in hashes for h in many)
