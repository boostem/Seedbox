"""Low-power mode (#85): reading battery and heat, deciding, the job system's limits, OS priority and /api/power."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from seedbox2 import power
from seedbox2.jobs import PRIORITY, Registry, Supervisor
from seedbox2.jobs.worker import JobContext
from seedbox2.library import Library
from seedbox2.library.service import Settings
from seedbox2.power import PowerMonitor, Reading, decide, limits_for

# --- job functions (module level: the spawned workers import them by name) -------------------------------------------


def report(payload, ctx):
    return {"pid": os.getpid(), "low_power": ctx.low_power, "pool_size": ctx.pool_size}


def priority_of_this_process(payload, ctx):
    if sys.platform == "win32":
        import psutil

        return {"class": psutil.Process().nice()}
    return {"nice": os.nice(0)}


def hold(payload, ctx):
    while not Path(payload["gate"]).exists():
        ctx.check()
        time.sleep(0.01)
    return os.getpid()


def session_in_cache(payload, ctx):
    """The sessions this worker has from earlier jobs, then one for this job's mode."""
    before = sorted(k for k in ctx._cache if k.startswith("seedbox2.models."))
    ctx.cached(f"seedbox2.models.{'small' if ctx.low_power else 'big'}", object)
    return before


def registry() -> Registry:
    r = Registry()
    r.add("report", report, pool="light")
    r.add("heavy_report", report, pool="heavy")
    r.add("priority", priority_of_this_process, pool="light")
    r.add("hold", hold, pool="light")
    r.add("sessions", session_in_cache, pool="light")
    return r


@pytest.fixture
def lib(tmp_path):
    library = Library.open(tmp_path / "library.sqlite3")
    yield library
    library.close()


@pytest.fixture
def make(lib):
    started: list[Supervisor] = []

    def make(**kw) -> Supervisor:
        sup = Supervisor(lib, None, registry=registry(), **kw).start()
        started.append(sup)
        return sup

    yield make
    for sup in started:
        sup.stop()


def until(cond, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


# --- reading the machine ---------------------------------------------------------------------------------------------


def supply(root: Path, name: str, **files: object) -> None:
    (root / name).mkdir(parents=True)
    for key, value in files.items():
        (root / name / key).write_text(f"{value}\n")


def test_linux_on_battery_and_on_mains(tmp_path):
    supply(tmp_path, "AC", type="Mains", online=0)
    supply(tmp_path, "BAT0", type="Battery", status="Discharging", capacity=64)
    assert power.read_linux_battery(tmp_path) == (True, 64)
    (tmp_path / "AC" / "online").write_text("1\n")
    (tmp_path / "BAT0" / "status").write_text("Charging\n")
    assert power.read_linux_battery(tmp_path) == (False, 64)


def test_linux_a_desktop_has_no_battery_even_with_a_wireless_mouse(tmp_path):
    assert power.read_linux_battery(tmp_path / "nothing") == (None, None)
    supply(tmp_path, "hidpp_battery_0", type="Battery", scope="Device", status="Discharging", capacity=20)
    assert power.read_linux_battery(tmp_path) == (None, None)


def test_linux_battery_without_a_charger_entry(tmp_path):
    supply(tmp_path, "BAT0", type="Battery", status="Full", capacity=100)
    assert power.read_linux_battery(tmp_path) == (False, 100)
    (tmp_path / "BAT0" / "status").write_text("Discharging\n")
    assert power.read_linux_battery(tmp_path) == (True, 100)


def zone(root: Path, n: int, temp: int, **trips: int) -> None:
    files = {"temp": temp}
    for i, (kind, limit) in enumerate(trips.items()):
        files[f"trip_point_{i}_type"], files[f"trip_point_{i}_temp"] = kind, limit
    supply(root, f"thermal_zone{n}", **files)


@pytest.mark.parametrize(("temp", "level"), [(60_000, "nominal"), (95_000, "serious"), (105_000, "critical")])
def test_linux_thermal_pressure_from_trip_points(tmp_path, temp, level):
    zone(tmp_path, 0, temp, passive=90_000, critical=105_000)
    zone(tmp_path, 1, 30_000, passive=-32_768, hot=0)  # a driver's unused trips say nothing
    assert power.read_linux_thermal(tmp_path) == level


def test_linux_thermal_unknown_without_zones(tmp_path):
    assert power.read_linux_thermal(tmp_path) == "unknown"


def test_macos_thermal_state_from_nsprocessinfo(monkeypatch):
    info = SimpleNamespace(thermalState=lambda: 2)
    fake = SimpleNamespace(NSProcessInfo=SimpleNamespace(processInfo=lambda: info))
    monkeypatch.setitem(sys.modules, "Foundation", fake)
    assert power.read_mac_thermal() == "serious"
    monkeypatch.setitem(sys.modules, "Foundation", None)  # no pyobjc
    assert power.read_mac_thermal() == "unknown"


def test_psutil_battery_for_macos_and_windows(monkeypatch):
    import psutil

    monkeypatch.setattr(psutil, "sensors_battery", lambda: SimpleNamespace(power_plugged=False, percent=41.6), raising=False)
    assert power._read_psutil_battery() == (True, 42)
    monkeypatch.setattr(psutil, "sensors_battery", lambda: SimpleNamespace(power_plugged=True, percent=99), raising=False)
    assert power._read_psutil_battery() == (False, 99)
    monkeypatch.setattr(psutil, "sensors_battery", lambda: None, raising=False)  # a desktop
    assert power._read_psutil_battery() == (None, None)


# --- deciding --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "reading", "reason"), [
    ("auto", Reading(False, 100, "nominal"), None),
    ("auto", Reading(None, None, "unknown"), None),
    ("auto", Reading(True, 50, "nominal"), "battery"),
    ("auto", Reading(False, 100, "serious"), "thermal"),
    ("auto", Reading(False, 100, "fair"), None),
    ("on", Reading(False, 100, "nominal"), "manual"),
    ("on", Reading(False, 100, "critical"), "thermal"),
    ("off", Reading(True, 5, "critical"), None),
])  # fmt: skip
def test_decide(mode, reading, reason):
    assert decide(mode, reading) == reason


def test_limits():
    assert limits_for(None, "nominal", 6) == power.Limits()
    normal = limits_for("battery", "nominal", 6)
    assert normal.low_power and normal.light_workers == 3 and normal.fps == 30
    assert normal.min_priority == {"heavy": PRIORITY["library"]}  # background stems wait; what was opened runs
    assert limits_for("battery", "nominal", 1).light_workers == 1
    hot = limits_for("thermal", "critical", 6)
    assert hot.light_workers == 1 and hot.min_priority == {"heavy": PRIORITY["crate"], "light": PRIORITY["crate"]}


def test_monitor_follows_the_mode_setting_and_publishes_changes(tmp_path):
    published: list = []
    settings = Settings(tmp_path / "settings.json")
    reading = {"now": Reading(False, 100, "nominal")}
    monitor = PowerMonitor(settings, read=lambda: reading["now"], publish=lambda t, d: published.append((t, d)))
    assert monitor.mode == "auto" and monitor.status()["low_power"] is False
    published.clear()
    assert monitor.status()["low_power"] is False and published == []  # nothing changed, nothing published
    reading["now"] = Reading(True, 80, "nominal")
    s = monitor.check()
    assert s["low_power"] and s["reason"] == "battery" and s["on_battery"] and s["battery_percent"] == 80
    assert s["limits"]["stems_paused"] and not s["limits"]["analysis_paused"] and s["limits"]["fps"] == 30
    assert published[-1] == ("power", s)
    assert monitor.set_mode("off")["low_power"] is False and settings.get("power") == {"mode": "off"}
    assert PowerMonitor(settings, read=lambda: reading["now"]).mode == "off"  # kept for the next run
    assert monitor.set_mode("on")["reason"] == "manual"
    with pytest.raises(ValueError):
        monitor.set_mode("turbo")
    settings.set("power", {"mode": "nonsense"})
    assert monitor.mode == "auto"


def test_monitor_stays_low_power_for_a_while_after_the_heat_is_gone():
    now = {"t": 0.0}
    reading = {"now": Reading(False, 100, "serious")}
    monitor = PowerMonitor(read=lambda: reading["now"], clock=lambda: now["t"])
    assert monitor.check()["reason"] == "thermal"
    reading["now"] = Reading(False, 100, "nominal")
    now["t"] = power.THERMAL_HOLD_S - 1
    assert monitor.check()["reason"] == "thermal"
    now["t"] = power.THERMAL_HOLD_S + 1
    assert monitor.check()["low_power"] is False


def test_monitor_survives_a_reader_that_breaks():
    def broken() -> Reading:
        raise OSError("nope")

    assert PowerMonitor(read=broken).status()["low_power"] is False


def test_forced_mode_beats_the_setting(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    settings.set("power", {"mode": "on"})
    monitor = PowerMonitor(settings, read=lambda: Reading(True, 10, "critical"), forced_mode="off")
    assert monitor.status()["low_power"] is False


# --- the job system's limits (real worker processes) ------------------------------------------------------------------


def test_workers_run_at_low_os_priority(make):
    sup = make(light_workers=1)
    row = sup.wait(sup.submit("priority")["id"], 30)
    assert row["state"] == "done"
    if sys.platform == "win32":  # and the server process keeps its own: it's what the comparison below is against
        assert row["result"]["class"] == power.BELOW_NORMAL_PRIORITY_CLASS
    else:
        assert row["result"]["nice"] - os.nice(0) == power.LOW_POWER_NICE  # relative to whatever started the app


def test_background_heavy_jobs_wait_and_what_the_user_opened_runs(make):
    sup = make(light_workers=1)
    sup.set_limits(low_power=True, min_priority={"heavy": PRIORITY["library"]})
    background = sup.submit("heavy_report", {"n": 1}, priority="background")
    opened = sup.submit("heavy_report", {"n": 2}, priority="set")
    light = sup.submit("report", {"n": 3}, priority="background")  # only the heavy pool is held
    assert sup.wait(opened["id"], 30)["state"] == "done"
    assert sup.wait(light["id"], 30)["state"] == "done"
    time.sleep(0.3)
    assert sup.job(background["id"])["state"] == "queued"
    pools = sup.status()["pools"]
    assert pools["heavy"]["paused"] == 1 and pools["light"]["paused"] == 0
    assert not sup.status()["waiting_for_ram"]  # that isn't why
    sup.set_limits()
    assert sup.wait(background["id"], 30)["state"] == "done"
    assert sup.status()["pools"]["heavy"]["paused"] == 0


def test_bumping_a_waiting_job_runs_it(make):
    sup = make(light_workers=1)
    sup.set_limits(low_power=True, min_priority={"heavy": PRIORITY["library"]})
    job = sup.submit("heavy_report", {"n": 1}, priority="background")
    sup.bump([job["id"]])
    assert sup.wait(job["id"], 30)["state"] == "done"
    sup.set_limits(low_power=True, min_priority={"heavy": PRIORITY["crate"]})
    again = sup.submit("heavy_report", {"n": 2}, priority="background")
    assert sup.submit("heavy_report", {"n": 2}, priority="set")["id"] == again["id"]  # asked for again, and it matters
    assert sup.wait(again["id"], 30)["state"] == "done"


def test_the_light_pool_shrinks_and_grows_back(make, tmp_path):
    sup = make(light_workers=2)
    gate = tmp_path / "gate"
    first = [sup.submit("hold", {"gate": str(gate), "n": n}) for n in (1, 2)]
    until(lambda: sup.status()["pools"]["light"]["busy"] == 2, 30)
    sup.set_limits(low_power=True, light_workers=1)
    time.sleep(0.2)
    assert sup.status()["pools"]["light"]["workers"] == 2  # running jobs aren't interrupted
    gate.write_text("")
    for job in first:
        assert sup.wait(job["id"], 30)["state"] == "done"
    until(lambda: sup.status()["pools"]["light"]["workers"] == 1)
    assert sup.status()["pools"]["light"] | {"workers": 0, "busy": 0} == {
        "workers": 0, "size": 1, "full_size": 2, "busy": 0, "queued": 0, "paused": 0}  # fmt: skip
    pids = {sup.wait(sup.submit("report", {"n": n})["id"], 30)["result"]["pid"] for n in range(3)}
    assert len(pids) == 1  # one worker does them all

    sup.set_limits()
    gate.unlink()
    again = [sup.submit("hold", {"gate": str(gate), "n": n}) for n in (3, 4)]
    until(lambda: sup.status()["pools"]["light"]["busy"] == 2, 30)
    gate.write_text("")
    for job in again:
        assert sup.wait(job["id"], 30)["state"] == "done"


def test_jobs_hear_about_low_power_and_fewer_workers_mean_fewer_threads(make):
    sup = make(light_workers=4)
    assert sup.wait(sup.submit("report", {"n": 1})["id"], 30)["result"]["low_power"] is False
    sup.set_limits(low_power=True, light_workers=2)
    r = sup.wait(sup.submit("report", {"n": 2})["id"], 30)["result"]
    assert r["low_power"] is True
    assert r["pool_size"] == 4  # each worker keeps its share of the cores, not double


def test_a_worker_drops_its_sessions_when_the_mode_changes(make):
    sup = make(light_workers=1)
    assert sup.wait(sup.submit("sessions", {"n": 1})["id"], 30)["result"] == []
    assert sup.wait(sup.submit("sessions", {"n": 2})["id"], 30)["result"] == ["seedbox2.models.big"]
    sup.set_limits(low_power=True)
    assert sup.wait(sup.submit("sessions", {"n": 3})["id"], 30)["result"] == []


def test_the_monitor_limits_an_attached_supervisor(make):
    reading = {"now": Reading(True, 50, "nominal")}
    sup = make(light_workers=4)
    monitor = PowerMonitor(read=lambda: reading["now"], publish=None)
    monitor.attach(sup)
    assert sup.low_power and sup.status()["pools"]["light"]["size"] == 2
    assert monitor.status()["limits"]["light_workers"] == 2 and monitor.status()["limits"]["light_workers_full"] == 4
    reading["now"] = Reading(False, 50, "nominal")
    monitor.check()
    assert not sup.low_power and sup.status()["pools"]["light"]["size"] == 4


# --- jobs choose the small models ------------------------------------------------------------------------------------


def test_the_context_tells_jobs(tmp_path):
    assert JobContext("j", None, None, {}, None).low_power is False
    assert JobContext("j", None, None, {}, None, low_power=True).low_power is True


def test_stems_job_uses_int8_in_low_power_mode(tmp_path, monkeypatch):
    from seedbox2.analysis import stems

    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    library = Library.open(tmp_path / "library.sqlite3")
    hash_ = library.upsert_track("a" * 40, 10.0)["hash"]
    asked: list[str] = []

    class Ctx:
        def __init__(self, low_power: bool) -> None:
            self.low_power = low_power
            self.library = library
            self.settings = Settings(tmp_path / "settings.json")

        def session_or_none(self, name: str) -> None:
            asked.append(name)

    assert stems.stems_job({"hash": hash_}, Ctx(True))["skipped"] == "model_missing"
    assert asked == ["htdemucs_int8", "htdemucs"]
    asked.clear()
    stems.stems_job({"hash": hash_}, Ctx(False))
    assert asked == ["htdemucs", "htdemucs_int8"]
    library.close()


# --- the OS call ------------------------------------------------------------------------------------------------------


def test_lower_process_priority_says_when_it_cant(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("os.nice isn't what Windows uses")

    def refused(_inc: int) -> int:
        raise PermissionError("no")

    monkeypatch.setattr(power.os, "nice", refused)
    assert power.lower_process_priority() is False


# --- the API ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    from seedbox2.server import start_server

    monkeypatch.setattr(power, "read_power", lambda: Reading(True, 37, "nominal"))
    handle = start_server(library_path=tmp_path / "library.sqlite3")
    yield handle
    handle.stop()


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_api_status_and_mode(server, tmp_path):
    s = call(f"{server.url}/api/power")
    assert s["mode"] == "auto" and s["low_power"] and s["reason"] == "battery" and s["battery_percent"] == 37
    s = call(f"{server.url}/api/power/settings", {"mode": "off"})
    assert s["mode"] == "off" and not s["low_power"] and s["limits"]["fps"] is None
    assert json.loads((tmp_path / "settings.json").read_text())["power"] == {"mode": "off"}
    with pytest.raises(urllib.error.HTTPError) as e:
        call(f"{server.url}/api/power/settings", {"mode": "turbo"})
    assert e.value.code == 422


def test_the_jobs_of_the_app_obey_the_monitor(server):
    call(f"{server.url}/api/jobs")  # starts the supervisor
    status = call(f"{server.url}/api/jobs")["status"]
    assert status["low_power"] is True
    assert status["pools"]["light"]["size"] == max(1, status["pools"]["light"]["full_size"] // 2)
    call(f"{server.url}/api/power/settings", {"mode": "off"})
    assert call(f"{server.url}/api/jobs")["status"]["low_power"] is False


def test_the_benchmark_measures_the_normal_mode(tmp_path, monkeypatch):
    from seedbox2.server import start_server

    monkeypatch.setattr(power, "read_power", lambda: Reading(True, 37, "serious"))
    handle = start_server(library_path=tmp_path / "library.sqlite3", power_mode="off")
    try:
        assert call(f"{handle.url}/api/power")["low_power"] is False
    finally:
        handle.stop()
