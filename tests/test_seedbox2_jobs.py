"""Seedbox 2's job system (#39): worker processes, priorities, cancel, restart recovery, the RAM guard, events."""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from pathlib import Path

import pytest

from seedbox2.jobs import PRIORITY, Cancelled, RamGuard, Registry, Supervisor, UnknownJobType
from seedbox2.jobs.registry import REGISTRY
from seedbox2.library import Library
from seedbox2.server.events import EventBus, _Item, _Subscriber

# --- job functions (module level: the spawned workers import them by name) -------------------------------------------


def record(payload, ctx):
    """Append payload["name"] to payload["log"] once payload["gate"] (if any) exists."""
    gate = payload.get("gate")
    while gate and not Path(gate).exists():
        ctx.check()
        time.sleep(0.01)
    with open(payload["log"], "a") as f:
        f.write(payload["name"] + "\n")
    return {"name": payload["name"], "pid": os.getpid()}


def slow(payload, ctx):
    """Report progress for `seconds`; stops when cancelled."""
    steps = int(payload.get("seconds", 30) / 0.01)
    for i in range(steps):
        ctx.progress(i / steps, f"step {i}")
        time.sleep(0.01)
    return "finished"


def stubborn(payload, ctx):
    """Ignores cancel (a native call that doesn't return)."""
    time.sleep(60)


def crash(payload, ctx):
    os._exit(3)


def boom(payload, ctx):
    raise ValueError("bad input")


def cancels_itself(payload, ctx):
    raise Cancelled


def session_reuse(payload, ctx):
    """Builds its 'ONNX session' once per worker process."""
    calls = ctx.cached("calls", lambda: [])
    calls.append(1)
    return {"pid": os.getpid(), "calls": len(calls)}


def registry() -> Registry:
    reg = Registry()
    for fn in (record, slow, stubborn, crash, boom, cancels_itself, session_reuse):
        reg.add(fn.__name__, fn)
    reg.add("heavy_record", record, pool="heavy")
    reg.add("once", record, resumable=False)
    return reg


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, object, str | None, bool]] = []

    def publish(self, type_, data=None, *, key=None, terminal=False):
        self.events.append((type_, data, key, terminal))

    def types(self, job_id: str) -> list[str]:
        out = []
        for t, d, _, _ in self.events:
            if isinstance(d, dict) and d.get("id") == job_id:
                out.append(t)
        return out


@pytest.fixture
def lib(tmp_path):
    library = Library.open(tmp_path / "library.sqlite3")
    yield library
    library.close()


@pytest.fixture
def make(lib):
    started: list[Supervisor] = []

    def make(**kw) -> Supervisor:
        kw.setdefault("registry", registry())
        kw.setdefault("light_workers", 1)
        sup = Supervisor(kw.pop("library", lib), kw.pop("events", None), **kw).start()
        started.append(sup)
        return sup

    yield make
    for sup in started:
        sup.stop()


def lines(path: Path) -> list[str]:
    return path.read_text().split() if path.exists() else []


def until(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


# --- running jobs ---------------------------------------------------------------------------------------------------


def test_a_job_runs_in_a_worker_process_and_its_result_is_persisted(make, lib, tmp_path):
    events = Recorder()
    sup = make(events=events)
    job = sup.submit("record", {"name": "a", "log": str(tmp_path / "log")})
    assert job["state"] == "queued" and job["priority"] == PRIORITY["library"]
    done = sup.wait(job["id"], 20)
    assert done["state"] == "done" and done["progress"] == 1.0
    assert done["result"]["name"] == "a" and done["result"]["pid"] != os.getpid()
    assert lib.job(job["id"])["result"] == done["result"]
    assert events.types(job["id"]) == ["job.queued", "job.started", "job.done"]
    assert [e[3] for e in events.events if e[0] == "job.done"] == [True]  # terminal: never dropped


def test_a_failing_job_fails_with_its_error_and_the_worker_carries_on(make, tmp_path):
    sup = make()
    bad = sup.submit("boom")
    assert sup.wait(bad["id"], 20)["error"] == "ValueError: bad input"
    ok = sup.submit("record", {"name": "a", "log": str(tmp_path / "log")})
    assert sup.wait(ok["id"], 20)["state"] == "done"
    assert sup.status()["pools"]["light"]["workers"] == 1  # the same worker


def test_workers_keep_their_cached_sessions_across_jobs(make):
    sup = make()
    first = sup.wait(sup.submit("session_reuse", {"n": 1})["id"], 20)["result"]
    second = sup.wait(sup.submit("session_reuse", {"n": 2})["id"], 20)["result"]
    assert first["pid"] == second["pid"] and (first["calls"], second["calls"]) == (1, 2)


def test_progress_is_published_coalesced_per_job_and_persisted(make, lib):
    events = Recorder()
    sup = make(events=events)
    job = sup.submit("slow", {"seconds": 1.5})
    done = sup.wait(job["id"], 20)
    assert done["state"] == "done" and done["result"] == "finished"
    progress = [e for e in events.events if e[0] == "job.progress"]
    assert progress and all(e[2] == f"job.progress:{job['id']}" and not e[3] for e in progress)
    assert len(progress) < 150 / 2  # the worker sends at most every 50 ms, not every step


def test_the_same_job_submitted_twice_is_one_job(make, tmp_path):
    sup = make()
    gate = tmp_path / "gate"
    payload = {"name": "x", "log": str(tmp_path / "log"), "gate": str(gate)}
    a = sup.submit("record", payload)
    b = sup.submit("record", payload, priority="set")
    assert a["id"] == b["id"]
    gate.touch()
    sup.wait(a["id"], 20)
    assert sup.submit("record", payload)["id"] != a["id"]  # finished: a new run


def test_unknown_job_types_and_non_module_functions_are_refused(make):
    sup = make()
    with pytest.raises(UnknownJobType):
        sup.submit("nope")
    with pytest.raises(ValueError):
        Registry().add("lambda", lambda payload, ctx: None)


# --- priorities -----------------------------------------------------------------------------------------------------


def test_jobs_run_by_priority_and_bumped_jobs_go_to_the_front(make, tmp_path):
    sup = make()
    log, gate = tmp_path / "log", tmp_path / "gate"
    blocker = sup.submit("record", {"name": "blocker", "log": str(log), "gate": str(gate)})
    until(lambda: sup.job(blocker["id"])["state"] == "running")

    def add(name, priority, front=False, **extra):
        return sup.submit("record", {"name": name, "log": str(log), **extra}, priority=priority, front=front)

    add("library1", "library")
    add("set", "set")
    add("crate", "crate")
    opened = add("library2", "library", hash="h2")  # a track the user just opened
    also = add("library3", "library", hashes=["h9", "h3"])
    add("background", "background")
    first = add("urgent", "library", front=True)
    assert first["priority"] == PRIORITY["set"] + 1
    assert sup.bump(hashes=["h3", "h2"]) == [opened["id"], also["id"]]  # in submission order
    gate.touch()
    until(lambda: len(lines(log)) == 8)
    assert lines(log) == ["blocker", "library2", "library3", "urgent", "set", "crate", "library1", "background"]


def test_bump_ignores_running_and_unknown_jobs(make, tmp_path):
    sup = make()
    gate = tmp_path / "gate"
    running = sup.submit("record", {"name": "r", "log": str(tmp_path / "log"), "gate": str(gate)})
    until(lambda: sup.job(running["id"])["state"] == "running")
    assert sup.bump([running["id"], "missing"]) == []
    gate.touch()


# --- cancel ---------------------------------------------------------------------------------------------------------


def test_cancel_a_queued_job(make, tmp_path):
    events = Recorder()
    sup = make(events=events)
    log, gate = tmp_path / "log", tmp_path / "gate"
    blocker = sup.submit("record", {"name": "blocker", "log": str(log), "gate": str(gate)})
    queued = sup.submit("record", {"name": "queued", "log": str(log)})
    assert sup.cancel(queued["id"])["state"] == "cancelled"
    assert events.types(queued["id"]) == ["job.queued", "job.cancelled"]
    gate.touch()
    sup.wait(blocker["id"], 20)
    time.sleep(0.2)
    assert lines(log) == ["blocker"]
    assert sup.cancel(blocker["id"])["state"] == "done"  # finished jobs stay as they are
    assert sup.cancel("missing") is None


def test_cancel_a_running_job_mid_job(make, tmp_path):
    sup = make()
    job = sup.submit("slow", {"seconds": 30})
    until(lambda: sup.job(job["id"])["state"] == "running")
    t0 = time.monotonic()
    sup.cancel(job["id"])
    assert sup.wait(job["id"], 10)["state"] == "cancelled"
    assert time.monotonic() - t0 < 1.5  # stopped at its next progress report, not killed after the grace period
    after = sup.submit("record", {"name": "after", "log": str(tmp_path / "log")})
    assert sup.wait(after["id"], 20)["state"] == "done"


def test_a_job_that_ignores_cancel_is_killed_and_its_worker_replaced(make, tmp_path):
    sup = make(cancel_grace_s=0.3)
    job = sup.submit("stubborn")
    until(lambda: sup.job(job["id"])["state"] == "running")
    sup.cancel(job["id"])
    assert sup.wait(job["id"], 10)["state"] == "cancelled"
    after = sup.submit("record", {"name": "after", "log": str(tmp_path / "log")})
    assert sup.wait(after["id"], 20)["state"] == "done"


def test_a_job_can_cancel_itself(make):
    sup = make()
    assert sup.wait(sup.submit("cancels_itself")["id"], 20)["state"] == "cancelled"


# --- crashes and restarts -------------------------------------------------------------------------------------------


def test_a_crashing_worker_fails_its_job_and_is_replaced(make, tmp_path):
    sup = make()
    job = sup.submit("crash")
    failed = sup.wait(job["id"], 20)
    assert failed["state"] == "failed" and "exit code 3" in failed["error"]
    after = sup.submit("record", {"name": "after", "log": str(tmp_path / "log")})
    assert sup.wait(after["id"], 20)["state"] == "done"


def test_restart_resumes_or_fails_interrupted_jobs_and_keeps_the_queue(make, lib, tmp_path):
    log = tmp_path / "log"
    resumable = lib.add_job("record", {"name": "resumed", "log": str(log)}, priority=1)
    once = lib.add_job("once", {"name": "once", "log": str(log)}, priority=1)
    queued = lib.add_job("record", {"name": "queued", "log": str(log)}, priority=0)
    unknown = lib.add_job("gone", {}, priority=0)
    for row in (resumable, once):
        lib.update_job(row["id"], state="running", progress=0.4)  # as a server that stopped mid-job left them
    sup = make()
    assert sup.wait(resumable["id"], 20)["state"] == "done"
    assert sup.wait(queued["id"], 20)["state"] == "done"
    interrupted = lib.job(once["id"])
    assert interrupted["state"] == "failed" and "stopped while it ran" in interrupted["error"]
    assert lib.job(unknown["id"])["state"] == "failed"
    assert lines(log) == ["resumed", "queued"]


def test_stopping_mid_job_and_starting_again_runs_it_again(make, lib, tmp_path):
    log, gate = tmp_path / "log", tmp_path / "gate"
    sup = make()
    job = sup.submit("record", {"name": "a", "log": str(log), "gate": str(gate)})
    later = sup.submit("record", {"name": "b", "log": str(log)})
    until(lambda: sup.job(job["id"])["state"] == "running")
    sup.stop()
    assert lib.job(job["id"])["state"] == "running" and lib.job(later["id"])["state"] == "queued"
    gate.touch()
    again = make()
    assert again.wait(job["id"], 20)["state"] == "done"
    assert again.wait(later["id"], 20)["state"] == "done"
    assert lines(log) == ["a", "b"]


# --- the RAM guard --------------------------------------------------------------------------------------------------


def test_no_heavy_job_starts_while_ram_is_low(make, tmp_path):
    free = {"bytes": 1 * 1024**3}
    sup = make(ram_guard=RamGuard(available=lambda: free["bytes"]))
    log = tmp_path / "log"
    heavy = sup.submit("heavy_record", {"name": "heavy", "log": str(log)})
    light = sup.submit("record", {"name": "light", "log": str(log)})
    assert sup.wait(light["id"], 20)["state"] == "done"  # light jobs carry on
    time.sleep(0.3)
    assert sup.job(heavy["id"])["state"] == "queued" and sup.status()["waiting_for_ram"]
    free["bytes"] = 2 * 1024**3
    assert sup.wait(heavy["id"], 20)["state"] == "done"
    assert not sup.status()["waiting_for_ram"]


def test_ram_guard_reads_psutil():
    guard = RamGuard()
    assert guard.min_available == int(1.5 * 1024**3) and guard.available() > 0


# --- the event bus --------------------------------------------------------------------------------------------------


def test_event_bus_coalesces_keyed_events_and_never_drops_terminal_ones():
    async def scenario():
        sub = _Subscriber(asyncio.get_running_loop(), maxsize=3)
        sub.push(_Item("p1", key="job.progress:a"))
        sub.push(_Item("other"))
        sub.push(_Item("p2", key="job.progress:a"))  # replaces p1 where it waits
        sub.push(_Item("done-a", terminal=True))
        assert [i.line for i in sub.items] == ["p2", "other", "done-a"]
        assert (await sub.get()).line == "p2"
        sub.push(_Item("p3", key="job.progress:a"))  # p2 was read: queued again
        for n in range(5):
            sub.push(_Item(f"done-{n}", terminal=True))
        lines_ = [i.line for i in sub.items]
        assert lines_ == ["done-a", "done-0", "done-1", "done-2", "done-3", "done-4"]  # only droppable ones went
        assert sub.dropped == 2 and not sub.waiting

    asyncio.run(scenario())


def test_event_bus_streams_coalesced_progress_and_terminal_events():
    bus = EventBus(queue_size=2, heartbeat_s=5)

    async def scenario():
        stream = bus.stream()
        assert (await stream.__anext__()).startswith("retry:")
        for i in range(50):
            bus.publish("job.progress", {"id": "a", "progress": i / 50}, key="job.progress:a")
        for i in range(5):
            bus.publish("job.done", {"id": str(i)}, terminal=True)
        bus.publish("noise", 1)  # the queue is full of terminal events: this one goes
        await asyncio.sleep(0.05)  # let the loop run the pushes
        got = []
        for _ in range(5):
            got.append(json.loads((await stream.__anext__())[len("data: "):]))
        await stream.aclose()
        return got

    got = asyncio.run(scenario())
    assert [e["data"]["id"] for e in got] == ["0", "1", "2", "3", "4"]


# --- through the server ---------------------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    from seedbox2.server import start_server

    names = []
    for fn in (record, crash):
        names.append(f"test_{fn.__name__}")
        REGISTRY.add(names[-1], fn)
    handle = start_server(library_path=tmp_path / "library.sqlite3")
    yield handle
    handle.stop()
    for name in names:
        REGISTRY._types.pop(name, None)


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - localhost
        return json.loads(r.read())


def test_a_crashing_worker_does_not_take_the_server_down(server, tmp_path):
    crashed = call(f"{server.url}/api/jobs", {"kind": "test_crash"})
    until(lambda: call(f"{server.url}/api/jobs/{crashed['id']}")["state"] == "failed", 20)
    assert call(f"{server.url}/api/health")
    ok = call(f"{server.url}/api/jobs", {"kind": "test_record", "payload": {"name": "a", "log": str(tmp_path / "l")},
                                         "priority": "set"})  # fmt: skip
    until(lambda: call(f"{server.url}/api/jobs/{ok['id']}")["state"] == "done", 20)
    listing = call(f"{server.url}/api/jobs")
    assert {"test_crash", "test_record"} <= set(listing["types"]) and len(listing["items"]) == 2
    assert call(f"{server.url}/api/jobs/bump", {"hashes": ["x"]}) == {"ids": []}
    assert call(f"{server.url}/api/jobs/{ok['id']}/cancel", {})["state"] == "done"


def test_jobs_api_errors(server):
    for body, status in (({"kind": "nope"}, 404), ({"kind": "test_record", "priority": "soon"}, 422)):
        with pytest.raises(urllib.error.HTTPError) as e:
            call(f"{server.url}/api/jobs", body)
        assert e.value.code == status
    with pytest.raises(urllib.error.HTTPError) as e:
        call(f"{server.url}/api/jobs/missing")
    assert e.value.code == 404
