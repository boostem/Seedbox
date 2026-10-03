"""User settings (JSON in ~/Library/Application Support/Seedbox) and the app's support / cache folders.

Environment overrides: MUSICRECO_OUTPUT_ROOT replaces the crates folder (never persisted by save_settings);
SEEDBOX_SUPPORT_DIR / SEEDBOX_CACHE_DIR relocate the support and cache folders (tests, portable setups).
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_NAME = "Seedbox"
OUTPUT_ROOT_ENV = "MUSICRECO_OUTPUT_ROOT"
SUPPORT_DIR_ENV = "SEEDBOX_SUPPORT_DIR"
CACHE_DIR_ENV = "SEEDBOX_CACHE_DIR"
SETTINGS_FILE = "settings.json"
DEFAULT_OUTPUT_ROOT = Path.home() / "Music" / "MusicReco"

# field -> (type, min, max); from_json clamps (lenient) or rejects (strict) values outside these.
LIMITS: dict[str, tuple[type, float, float]] = {
    "workers": (int, 1, 8),
    "tolerance_s": (float, 1.0, 120.0),
    "bpm_tolerance": (float, 0.01, 0.5),
    "preview_concurrency": (int, 1, 8),
    "preview_cache_mb": (int, 0, 100_000),
}

_lock = threading.RLock()


@dataclass
class Settings:
    output_root: Path = field(default_factory=lambda: DEFAULT_OUTPUT_ROOT)
    workers: int = 2  # parallel downloads
    tolerance_s: float = 15.0  # YouTube duration tolerance
    bpm_tolerance: float = 0.10  # pre-tick rule
    preview_concurrency: int = 3
    preview_cache_mb: int = 500
    pretick_all: bool = True  # tick every tunebat "r" rec; False = only key/BPM rule matches


def _env_dir(var: str, default: Path) -> Path:
    raw = os.environ.get(var, "").strip()
    d = Path(raw).expanduser() if raw else default
    d.mkdir(parents=True, exist_ok=True)
    return d


def app_support_dir() -> Path:
    """~/Library/Application Support/Seedbox (or $SEEDBOX_SUPPORT_DIR), created if missing."""
    return _env_dir(SUPPORT_DIR_ENV, Path.home() / "Library" / "Application Support" / APP_NAME)


def cache_dir() -> Path:
    """~/Library/Caches/Seedbox (or $SEEDBOX_CACHE_DIR), created if missing."""
    return _env_dir(CACHE_DIR_ENV, Path.home() / "Library" / "Caches" / APP_NAME)


def settings_path() -> Path:
    return app_support_dir() / SETTINGS_FILE


def env_output_root() -> Path | None:
    """The MUSICRECO_OUTPUT_ROOT override, if set."""
    raw = os.environ.get(OUTPUT_ROOT_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def to_json(s: Settings) -> dict:
    """Wire/disk shape (types.ts Settings): output_root as a string, everything else as is."""
    return {f.name: str(getattr(s, f.name)) if f.name == "output_root" else getattr(s, f.name) for f in fields(s)}


def from_json(d: dict, *, strict: bool = False) -> Settings:
    """Settings from a dict; missing keys take defaults.

    Lenient (default, for files on disk): unknown keys are ignored, bad values fall back to the default and
    out-of-range numbers are clamped. Strict (for API input): any of those raises ValueError naming the field.
    """
    if not isinstance(d, dict):
        raise ValueError("settings must be a JSON object")
    s = Settings()
    if strict and (unknown := sorted(set(d) - {f.name for f in fields(s)})):
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    if "output_root" in d:
        root = d["output_root"]
        path = Path(root).expanduser() if isinstance(root, (str, os.PathLike)) and str(root).strip() else None
        if path is not None and path.is_absolute():
            s.output_root = path
        elif strict:
            raise ValueError("output_root must be an absolute folder path")
    for name, (kind, lo, hi) in LIMITS.items():
        if name in d:
            setattr(s, name, _number(name, d[name], kind, lo, hi, getattr(s, name), strict))
    if "pretick_all" in d:
        if isinstance(d["pretick_all"], bool):
            s.pretick_all = d["pretick_all"]
        elif strict:
            raise ValueError("pretick_all must be true or false")
    return s


def _number(name: str, value: Any, kind: type, lo: float, hi: float, default: Any, strict: bool) -> Any:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if ok and kind is int and value != int(value):
        ok = False
    if not ok:
        if strict:
            raise ValueError(f"{name} must be {'a whole number' if kind is int else 'a number'}")
        return default
    if not lo <= value <= hi:
        if strict:
            raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
        value = min(hi, max(lo, value))
    return kind(value)


def load_settings(path: Path | None = None) -> Settings:
    """Saved settings (defaults if there are none or the file is unreadable), with the env root override applied."""
    path = path or settings_path()
    with _lock:
        s = Settings()
        try:
            s = from_json(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable settings file %s: %s", path, e)
    if (root := env_output_root()) is not None:
        s.output_root = root
    return s


def save_settings(s: Settings, path: Path | None = None) -> None:
    """Write settings atomically. An output_root that only comes from MUSICRECO_OUTPUT_ROOT is not persisted."""
    path = path or settings_path()
    data = to_json(s)
    with _lock:
        env_root = env_output_root()
        if env_root is not None and Path(data["output_root"]) == env_root:
            try:
                previous = json.loads(path.read_text(encoding="utf-8")).get("output_root")
            except (OSError, ValueError, AttributeError):
                previous = None
            data["output_root"] = previous if isinstance(previous, str) and previous else str(DEFAULT_OUTPUT_ROOT)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
