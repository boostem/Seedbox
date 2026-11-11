""""Report a problem" (#89): one zip with what a bug report needs, and nothing of the music.

`build_bundle()` writes `Seedbox-2-report-<time>.zip` into a `reports/` folder in the logs folder with:

| file            | what                                                                                        |
|-----------------|---------------------------------------------------------------------------------------------|
| README.txt      | what this is, and what it leaves out                                                        |
| system.json     | Seedbox 2's and Python's versions, the OS, CPU, memory, disk space, folders, ffmpeg, deno,   |
|                 | the update state, yt-dlp, and the $SEEDBOX* variables                                       |
| self-test.json  | the output of `--self-test`, run in a separate process (or why it didn't run)               |
| versions.json   | each analyser's and model's version, the library's schema and how many analyses and          |
|                 | artifacts of each version it holds, and the installed packages' versions                    |
| logs/           | the log files (seedbox2.log and its rotations, the update installer's log), at most the last |
|                 | LOG_LIMIT bytes of each                                                                     |

No audio, no stems, no database and no settings file go in. File and folder names in the logs can still name tracks;
README.txt says so. The home folder is written as `~` everywhere, so the user name doesn't travel with it.
`issue_url()` is a new GitHub issue with the versions filled in, for the zip to be attached to.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import zipfile
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

from seedbox2 import __version__

log = logging.getLogger(__name__)

REPORTS_SUBDIR = "reports"
LOG_LIMIT = 4_000_000  # bytes of each log file, from its end
SELF_TEST_TIMEOUT_S = 180.0
ANALYSIS_MODULES = ("beats", "beatthis", "clap", "energy", "grid", "key", "loudness", "sections", "stems", "vibe",
                    "vocals", "waveform")  # fmt: skip
PACKAGES = ("numpy", "scipy", "soundfile", "onnxruntime", "fastapi", "uvicorn", "pydantic", "pywebview", "yt-dlp",
            "yt-dlp-ejs", "pedalboard", "mutagen", "watchdog", "platformdirs", "psutil", "certifi")  # fmt: skip
ISSUES_URL = "https://github.com/{repo}/issues/new"

README = """Seedbox 2 problem report
========================

Made by Settings > Report a problem in Seedbox 2 {version} on {when}.
Attach this zip to an issue: {issues}

What is inside
- system.json: the versions of Seedbox 2, Python and the OS; the CPU, memory and free disk space; Seedbox 2's
  folders, ffmpeg and deno; the update state and yt-dlp.
- self-test.json: the result of `Seedbox 2 --self-test`.
- versions.json: the version of each analyser and model, and how many analyses of each version the library holds.
- logs/: Seedbox 2's log files.

What is not: audio, stems, the library database and your settings. The logs can name the files and folders that
Seedbox 2 worked on; look through them first if that matters. Your home folder is written as ~.
"""


def _redact(text: str, home: str) -> str:
    if not home or len(home) < 3:
        return text
    out = text.replace(home, "~")
    if os.sep == "\\":  # paths in JSON have their backslashes doubled
        out = out.replace(home.replace("\\", "\\\\"), "~")
    return out


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a report says what it couldn't find out
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- parts

def os_name() -> str:
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if sys.platform == "win32":
        release, version, _csd, _ptype = platform.win32_ver()
        return f"Windows {release} ({version})"
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME") or platform.platform()
    except OSError:
        return platform.platform()


def memory() -> dict:
    try:
        import psutil

        vm = psutil.virtual_memory()
        return {"total_gb": round(vm.total / 1e9, 1), "available_gb": round(vm.available / 1e9, 1)}
    except Exception:  # noqa: BLE001
        return {}


def system_info(extra: dict | None = None) -> dict:
    from musicreco import paths as binaries
    from seedbox2 import paths

    folders = paths.folders()
    disk = {}
    for name in ("settings", "cache", "music"):
        try:
            usage = shutil.disk_usage(folders[name] if folders[name].exists() else Path.home())
            disk[name] = {"free_gb": round(usage.free / 1e9, 1), "total_gb": round(usage.total / 1e9, 1)}
        except OSError:
            pass
    info = {
        "app": "Seedbox 2",
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "python": sys.version,
        "os": os_name(),
        "platform": platform.platform(),
        "sys_platform": sys.platform,
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpus": os.cpu_count(),
        "memory": memory(),
        "disk": disk,
        "locale": _safe(lambda: ".".join(filter(None, __import__("locale").getlocale()))),
        "folders": {name: str(path) for name, path in folders.items()},
        "ffmpeg": _safe(binaries.ffmpeg_path),
        "deno": _safe(binaries.deno_path),
        "environment": {k: v for k, v in os.environ.items() if k.startswith(("SEEDBOX", "MUSICRECO"))},
        "ytdlp": _safe(lambda: __import__("seedbox2.ytdlp", fromlist=["describe"]).describe()),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return {**info, **(extra or {})}


def self_test_command(report: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--self-test", "--report", str(report)]
    return [sys.executable, "-m", "seedbox2", "--self-test", "--report", str(report)]


def run_self_test(work: Path, timeout: float = SELF_TEST_TIMEOUT_S) -> dict:
    """`--self-test` in a separate process: its report, with how long it took and its exit code."""
    report = work / "self-test-raw.json"
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    cwd = Path(__file__).resolve().parent.parent if not getattr(sys, "frozen", False) else None
    t0 = time.monotonic()
    try:
        proc = subprocess.run(self_test_command(report), capture_output=True, text=True, timeout=timeout,  # noqa: S603
                              stdin=subprocess.DEVNULL, cwd=cwd, check=False, **extra)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ran": False, "error": f"{type(e).__name__}: {e}"}
    out: dict[str, Any] = {"ran": True, "exit_code": proc.returncode, "seconds": round(time.monotonic() - t0, 1)}
    try:
        out["report"] = json.loads(report.read_text("utf-8"))
    except (OSError, ValueError):
        out["stdout"] = (proc.stdout or "")[-20_000:]
    if proc.returncode != 0:
        out["stderr"] = (proc.stderr or "")[-20_000:]
    return out


def analyser_versions() -> dict:
    import importlib

    out = {}
    for name in ANALYSIS_MODULES:
        try:
            mod = importlib.import_module(f"seedbox2.analysis.{name}")
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": f"{type(e).__name__}: {e}"}
            continue
        found = {k: getattr(mod, k) for k in dir(mod) if k.endswith("VERSION") and isinstance(getattr(mod, k), str | int)}
        if found:
            out[name] = found
    return out


def model_versions() -> dict:
    from seedbox2.models import MODELS
    from seedbox2.models.store import installed, models_dir

    root = models_dir()
    return {name: {"version": spec.version, "installed": _safe(lambda spec=spec: installed(spec, root))}
            for name, spec in MODELS.items()}  # fmt: skip


def library_versions(path: Path | None = None) -> dict:
    """The library's schema version and its analyses and artifacts counted by version, read-only."""
    from seedbox2.library import default_path

    path = Path(path) if path is not None else default_path()
    if not path.is_file():
        return {"exists": False}
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        out: dict[str, Any] = {"exists": True, "schema": conn.execute("PRAGMA user_version").fetchone()[0]}
        queries = {
            "tracks": "SELECT COUNT(*) FROM tracks",
            "analyses": "SELECT analyser, version, COUNT(*) FROM analyses GROUP BY 1, 2 ORDER BY 1, 2",
            "artifacts": "SELECT kind, model_version, COUNT(*) FROM artifacts GROUP BY 1, 2 ORDER BY 1, 2",
            "jobs": "SELECT kind, state, COUNT(*) FROM jobs GROUP BY 1, 2 ORDER BY 1, 2",
        }
        for key, sql in queries.items():
            try:
                rows = conn.execute(sql).fetchall()
            except sqlite3.Error as e:
                out[key] = {"error": str(e)}
                continue
            out[key] = rows[0][0] if key == "tracks" else [list(r) for r in rows]
        return out
    finally:
        conn.close()


def package_versions() -> dict:
    out = {}
    for name in PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def versions() -> dict:
    return {"app": __version__, "analysers": _safe(analyser_versions), "models": _safe(model_versions),
            "library": _safe(library_versions), "packages": _safe(package_versions)}


def log_files(log_dir: Path, cache_dir: Path | None = None) -> list[Path]:
    """The log files: `*.log*` directly in the logs folder (so not the reports, nor anything else) and the update
    installer's log."""
    files = sorted(p for p in log_dir.glob("*.log*") if p.is_file()) if log_dir.is_dir() else []
    if cache_dir is not None and (install_log := cache_dir / "updates" / "install.log").is_file():
        files.append(install_log)
    return files


def _tail(path: Path, limit: int = LOG_LIMIT) -> bytes:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - limit))
        data = f.read()
    return (b"[... the first %d bytes were left out ...]\n" % (size - limit) + data) if size > limit else data


# ---------------------------------------------------------------- bundle

def issue_url(repo: str, info: dict | None = None) -> str:
    info = info or {}
    body = (f"**What happened**\n\n\n**What I expected**\n\n\n**Steps**\n1. \n\n---\nSeedbox 2 {__version__} on "
            f"{info.get('os') or os_name()} ({info.get('machine') or platform.machine()}). The problem report zip "
            "is attached.\n")  # fmt: skip
    return f"{ISSUES_URL.format(repo=repo)}?{urllib.parse.urlencode({'body': body})}"


def build_bundle(dest_dir: Path | None = None, *, self_test: bool = True, extra_system: dict | None = None,
                 repo: str | None = None) -> dict:  # fmt: skip
    """Write the zip; returns {path, size, files, issue_url, self_test_ok}."""
    from seedbox2 import paths
    from seedbox2.update import DEFAULT_REPO, REPO_ENV

    repo = repo or os.environ.get(REPO_ENV, "").strip() or DEFAULT_REPO
    folders = paths.folders()
    dest_dir = Path(dest_dir) if dest_dir is not None else folders["logs"] / REPORTS_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = dest_dir / f"Seedbox-2-report-{stamp}.zip"
    home = str(Path.home())
    work = dest_dir / f".work-{stamp}-{os.getpid()}"
    work.mkdir()
    try:
        info = system_info(extra_system)
        parts = {"system.json": _dump(info), "versions.json": _dump(versions())}
        st = run_self_test(work) if self_test else {"ran": False, "error": "skipped"}
        parts["self-test.json"] = _dump(st)
        parts["README.txt"] = README.format(version=__version__, when=info["time"],
                                            issues=ISSUES_URL.format(repo=repo))  # fmt: skip
        tmp = work / path.name
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, text in parts.items():
                zf.writestr(name, _redact(text, home))
            for f in log_files(folders["logs"], folders["cache"]):
                try:
                    data = _tail(f)
                except OSError as e:
                    log.warning("leaving %s out of the report: %s", f, e)
                    continue
                zf.writestr(f"logs/{f.name}", _redact(data.decode("utf-8", "replace"), home))
            files = zf.namelist()
        os.replace(tmp, path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log.info("problem report written: %s", path)
    report = st.get("report") if isinstance(st.get("report"), dict) else {}
    return {"path": str(path), "size": path.stat().st_size, "files": files, "issue_url": issue_url(repo, info),
            "self_test_ok": report.get("ok") if st.get("ran") else None}
