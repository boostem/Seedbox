"""Being polite on a laptop (#85): is it on battery or running hot, and what Seedbox 2 holds back because of that.

- `read_power()`: battery or mains and thermal pressure. Linux reads /sys/class/power_supply and /sys/class/thermal,
  macOS asks `NSProcessInfo.thermalState` and psutil (IOKit), Windows asks psutil (`GetSystemPowerStatus`; Windows has
  no thermal pressure to ask for).
- `decide(mode, reading)`: is low-power mode on, and why. The `mode` setting (`power.mode` in settings.json) is `auto`
  (on battery or when hot, the default), `on` or `off`.
- `limits_for(...)`: what low-power mode means for the job system and the UI: background stems wait (what the user
  opened still runs, with the INT8 model), half the light workers, the small beat model and 30 fps waveforms. Thermal
  pressure at `critical` also stops the library's analysis and leaves one light worker.
- `PowerMonitor`: polls, applies the limits to the Supervisor and publishes a `power` event when anything changes.
- `lower_process_priority()`: every worker process calls it first, so the UI and audio always come before analysis.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SETTINGS_KEY = "power"
MODES = ("auto", "on", "off")
DEFAULT_MODE = "auto"
POLL_S = 15.0
THERMAL_HOLD_S = 60.0  # stays low-power this long after the heat is gone, so a machine at the limit doesn't flap
LOW_POWER_FPS = 30
LOW_POWER_NICE = 10
BELOW_NORMAL_PRIORITY_CLASS = 0x4000

THERMAL_LEVELS = ("nominal", "fair", "serious", "critical")  # NSProcessInfoThermalState's order
HOT = ("serious", "critical")
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Reading:
    on_battery: bool | None = None  # None: this machine has no battery
    battery_percent: int | None = None
    thermal: str = UNKNOWN  # one of THERMAL_LEVELS, or UNKNOWN where the OS doesn't say


# --- reading the machine -----------------------------------------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def read_linux_battery(root: Path = Path("/sys/class/power_supply")) -> tuple[bool | None, int | None]:
    """(on battery, percent) from sysfs. A mouse's battery (`scope` Device) doesn't count; neither does a desktop's
    lack of one (None, None)."""
    try:
        supplies = sorted(root.iterdir())
    except OSError:
        return None, None
    statuses: list[str] = []
    percents: list[int] = []
    mains: list[bool] = []
    for supply in supplies:
        if (_read(supply / "scope") or "").lower() == "device":
            continue
        if _read(supply / "type") == "Battery":
            statuses.append((_read(supply / "status") or "").lower())
            capacity = _read(supply / "capacity")
            if capacity and capacity.isdigit():
                percents.append(int(capacity))
        else:
            online = _read(supply / "online")
            if online is not None:
                mains.append(online == "1")
    if not statuses:
        return None, None
    percent = round(sum(percents) / len(percents)) if percents else None
    if any(mains):
        return False, percent
    if "discharging" in statuses:
        return True, percent
    if "charging" in statuses or "full" in statuses:  # some drivers don't say `online` truthfully
        return False, percent
    return bool(mains), percent  # a charger that says it is unplugged; or nothing to go on


def read_linux_thermal(root: Path = Path("/sys/class/thermal")) -> str:
    """How close the hottest thermal zone is to its kernel trip points: `passive` is where the kernel starts to slow
    the CPU down (serious), `hot` and `critical` where the machine is about to act (critical)."""
    try:
        zones = sorted(root.glob("thermal_zone*"))
    except OSError:
        return UNKNOWN
    level = UNKNOWN
    for zone in zones:
        temp = _read(zone / "temp")
        if temp is None or not temp.lstrip("-").isdigit():
            continue
        level = max(level, "nominal", key=_thermal_rank)
        for trip in zone.glob("trip_point_*_type"):
            kind = _read(trip)
            limit = _read(trip.with_name(trip.name.replace("_type", "_temp")))
            if limit is None or not limit.isdigit() or int(limit) <= 0 or int(temp) < int(limit):
                continue  # drivers report nonsense limits (0, negative) for trips they don't use
            if kind == "passive":
                level = max(level, "serious", key=_thermal_rank)
            elif kind in ("hot", "critical"):
                level = "critical"
    return level


def _thermal_rank(level: str) -> int:
    return THERMAL_LEVELS.index(level) if level in THERMAL_LEVELS else -1


def read_mac_thermal() -> str:
    try:
        # pyobjc, which the window (pywebview) already brings on macOS
        from Foundation import NSProcessInfo

        return THERMAL_LEVELS[int(NSProcessInfo.processInfo().thermalState())]
    except Exception as e:  # noqa: BLE001 - no pyobjc, or a state newer than this list: just not known
        log.debug("no thermal state: %s", e)
        return UNKNOWN


def _read_psutil_battery() -> tuple[bool | None, int | None]:
    try:
        import psutil

        battery = psutil.sensors_battery()
    except Exception as e:  # noqa: BLE001 - not every machine or psutil build can tell
        log.debug("no battery state: %s", e)
        return None, None
    if battery is None:
        return None, None
    return battery.power_plugged is False, round(battery.percent)


def read_power() -> Reading:
    """What the machine says now. Never raises: what it can't tell is None or UNKNOWN, which changes nothing."""
    if sys.platform.startswith("linux"):
        on_battery, percent = read_linux_battery()
        return Reading(on_battery, percent, read_linux_thermal())
    on_battery, percent = _read_psutil_battery()
    return Reading(on_battery, percent, read_mac_thermal() if sys.platform == "darwin" else UNKNOWN)


# --- what follows from it ----------------------------------------------------------------------------------------------


def decide(mode: str, reading: Reading) -> str | None:
    """Why low-power mode is on (`thermal`, `manual`, `battery`), or None when it isn't. Heat comes first even when
    it was switched on by hand: at `critical` it holds back more."""
    if mode == "off":
        return None
    if reading.thermal in HOT:
        return "thermal"
    if mode == "on":
        return "manual"
    return "battery" if reading.on_battery else None


@dataclass(frozen=True)
class Limits:
    low_power: bool = False
    light_workers: int | None = None  # None: the pool's full size
    min_priority: dict[str, int] | None = None  # pool -> jobs below this priority wait
    fps: int | None = None  # the UI's cap on waveform drawing; None: no cap


def limits_for(reason: str | None, thermal: str, full_light_workers: int) -> Limits:
    from seedbox2.jobs.supervisor import PRIORITY

    if reason is None:
        return Limits()
    if reason == "thermal" and thermal == "critical":
        return Limits(True, 1, {"heavy": PRIORITY["crate"], "light": PRIORITY["crate"]}, LOW_POWER_FPS)
    return Limits(True, max(1, full_light_workers // 2), {"heavy": PRIORITY["library"]}, LOW_POWER_FPS)


class PowerMonitor:
    """The low-power state: `status()` for /api/power, `check()` on a poll thread, `attach()` to the Supervisor."""

    def __init__(self, settings: Any = None, *, read: Callable[[], Reading] | None = None,
                 publish: Callable[..., None] | None = None, forced_mode: str | None = None,
                 clock: Callable[[], float] = time.monotonic, poll_s: float = POLL_S) -> None:  # fmt: skip
        self.settings = settings
        self._read_power = read
        self._publish = publish
        self._forced_mode = forced_mode
        self._clock = clock
        self._poll_s = poll_s
        self._lock = threading.RLock()
        self._jobs: Any = None
        self._reading: Reading | None = None
        self._hot_until = float("-inf")
        self._last_thermal = UNKNOWN
        self._last: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def mode(self) -> str:
        if self._forced_mode is not None:
            return self._forced_mode
        raw = self.settings.get(SETTINGS_KEY, {}) if self.settings is not None else {}
        mode = raw.get("mode") if isinstance(raw, dict) else None
        return mode if mode in MODES else DEFAULT_MODE

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise ValueError(f"no power mode {mode!r}")
        if self.settings is not None:
            self.settings.set(SETTINGS_KEY, {"mode": mode})
        return self.check()

    def attach(self, jobs: Any) -> None:
        """From now on the limits apply to this Supervisor, starting with the current ones."""
        with self._lock:
            self._jobs = jobs
        self.check(force=True)

    def _full_light_workers(self) -> int:
        from seedbox2.jobs.supervisor import light_workers_default

        return self._jobs.full_size("light") if self._jobs is not None else light_workers_default()

    def _sample(self) -> Reading:
        read = self._read_power or read_power  # looked up late: tests replace read_power
        try:
            return read()
        except Exception:
            log.exception("couldn't read the power state")
            return Reading()

    def _state(self, reading: Reading) -> tuple[str | None, str, Limits]:
        """The reason, the thermal level it acts on, and the limits (holding on to the heat for THERMAL_HOLD_S)."""
        now = self._clock()
        if reading.thermal in HOT:
            self._hot_until = now + THERMAL_HOLD_S
            self._last_thermal = reading.thermal
        thermal = reading.thermal
        if thermal not in HOT and now < self._hot_until:
            thermal = self._last_thermal
        effective = Reading(reading.on_battery, reading.battery_percent, thermal)
        reason = decide(self.mode, effective)
        return reason, thermal, limits_for(reason, thermal, self._full_light_workers())

    def _render(self, reading: Reading, reason: str | None, limits: Limits) -> dict:
        full = self._full_light_workers()
        min_priority = limits.min_priority or {}
        return {
            "mode": self.mode,
            "low_power": limits.low_power,
            "reason": reason,
            "on_battery": reading.on_battery,
            "battery_percent": reading.battery_percent,
            "thermal": reading.thermal,
            "limits": {
                "stems_paused": "heavy" in min_priority,
                "analysis_paused": "light" in min_priority,
                "light_workers": limits.light_workers or full,
                "light_workers_full": full,
                "fps": limits.fps,
            },
        }

    def check(self, *, force: bool = False) -> dict:
        """Read the machine; when the outcome changed, apply it to the jobs and publish it. Returns the status."""
        with self._lock:
            reading = self._sample()
            reason, _, limits = self._state(reading)
            status = self._render(reading, reason, limits)
            changed = status != self._last
            self._reading, self._last = reading, status
            if self._jobs is not None and (changed or force):
                self._jobs.set_limits(low_power=limits.low_power, light_workers=limits.light_workers,
                                      min_priority=limits.min_priority)  # fmt: skip
        if changed:
            if status["low_power"]:
                log.info("low-power mode on (%s)", reason)
            if self._publish is not None:
                self._publish("power", status)
        return status

    def status(self) -> dict:
        return self.check()

    def start(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="seedbox2-power", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._poll_s):
            try:
                self.check()
            except Exception:
                log.exception("power check failed")

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(2)


# --- the OS's priority ------------------------------------------------------------------------------------------------


def lower_process_priority() -> bool:
    """Make this process give way to everything else: `nice` on macOS and Linux, BELOW_NORMAL_PRIORITY_CLASS on
    Windows. Threads and child processes (onnxruntime, ffmpeg) started afterwards inherit it. A job still gets the
    whole machine when nothing else wants it. Returns whether it worked."""
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p  # a pseudo-handle, -1: wider than the default int
            return bool(kernel32.SetPriorityClass(ctypes.c_void_p(kernel32.GetCurrentProcess()),
                                                  BELOW_NORMAL_PRIORITY_CLASS))  # fmt: skip
        os.nice(LOW_POWER_NICE)
        return True
    except (OSError, AttributeError) as e:
        log.warning("couldn't lower this worker's priority: %s", e)
        return False
