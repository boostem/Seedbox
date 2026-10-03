"""Runtime yt-dlp updates: newer yt-dlp (+ yt-dlp-ejs) wheels in a user-writable folder that shadows the bundled copy.

A frozen yt-dlp stops working within weeks (YouTube changes), so the app can fetch a newer one without pip:

    root/                       ~/Library/Application Support/Seedbox/pydeps (or $SEEDBOX_PYDEPS_DIR,
                                or $SEEDBOX_SUPPORT_DIR/pydeps)
      versions/<version>/       unpacked wheels: yt_dlp/, yt_dlp_ejs/, *.dist-info, MARKER (written after checks)
      current  -> versions/<v>  symlink swapped atomically (rename over it); this path goes on sys.path
      previous -> versions/<v>  what `current` pointed to before the last update (for `rollback()`)

`activate()` must run before anything imports yt_dlp: it puts root/current at the front of sys.path when the
copy there passed verification and is newer than the yt-dlp bundled with the app (a newer Seedbox build wins).
PyInstaller's importer honours sys.path order, so this works frozen and from source alike. An update is
verified in a separate process (import, extractor list, YoutubeDL instance, bytecode compile) before the swap,
and takes effect on the next launch. Only the standard library (+ certifi when present) is used here.
"""

from __future__ import annotations

import compileall
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

log = logging.getLogger(__name__)

PYDEPS_ENV = "SEEDBOX_PYDEPS_DIR"
SUPPORT_DIR_ENV = "SEEDBOX_SUPPORT_DIR"
DISABLE_ENV = "SEEDBOX_PYDEPS_DISABLE"  # "1": never activate the user copy (verification child, debugging)
VERIFY_FLAG = "--seedbox-verify-pydeps"
MARKER = "seedbox-pydeps.json"
PYPI_JSON = "https://pypi.org/pypi/{name}/json"
PYPI_RELEASE_JSON = "https://pypi.org/pypi/{name}/{version}/json"
HTTP_TIMEOUT_S = 20.0
VERIFY_TIMEOUT_S = 300.0
CHECK_INTERVAL_S = 6 * 3600.0
RETRY_AFTER_ERROR_S = 300.0  # offline at startup: try again soon rather than in 6 h
_RESULT_PREFIX = "SEEDBOX-VERIFY:"
_EJS_PIN_RE = re.compile(r"^yt-dlp-ejs\s*==\s*([0-9][\w.]*)")

_lock = threading.Lock()  # one update/rollback at a time
_state: dict[str, Any] = {
    "activated": None,  # Path on sys.path, or None
    "activate_called": False,
    "bundled": None,  # bundled yt-dlp version, computed once before activation
    "latest": None,  # last PyPI answer (display form)
    "latest_error": None,
    "checked_at": 0.0,
    "checking": False,
}


class UpdateError(RuntimeError):
    """An update step failed; the message is shown to the user."""


# ---------------------------------------------------------------- versions + folders

def pydeps_root() -> Path:
    """Folder holding updated packages (not created here)."""
    if raw := os.environ.get(PYDEPS_ENV, "").strip():
        return Path(raw).expanduser()
    if raw := os.environ.get(SUPPORT_DIR_ENV, "").strip():
        return Path(raw).expanduser() / "pydeps"
    return Path.home() / "Library" / "Application Support" / "Seedbox" / "pydeps"


def _vkey(version: str | None) -> tuple[int, ...]:
    """Sortable key for yt-dlp's date versions ("2026.08.19", "2026.8.19", "2026.08.19.123456")."""
    if not version:
        return ()
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", version.strip()))


def display_version(version: str) -> str:
    """yt-dlp's own spelling ("2026.08.19") of a PyPI-normalised version ("2026.8.19")."""
    parts = version.split(".")
    if len(parts) >= 3 and len(parts[0]) == 4 and all(p.isdigit() for p in parts[:3]):
        parts[1], parts[2] = parts[1].zfill(2), parts[2].zfill(2)
    return ".".join(parts)


def _newer(a: str | None, b: str | None) -> bool:
    return bool(a) and _vkey(a) > _vkey(b)


def _read_marker(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("ok") and data.get("yt_dlp") else None


def installed_version() -> str | None:
    """Version in root/current if it passed verification (active or waiting for a restart), else None."""
    marker = _read_marker(pydeps_root() / "current")
    return marker["yt_dlp"] if marker else None


def bundled_version() -> str | None:
    """Version of the yt-dlp shipped with the app (or installed in the venv), ignoring the pydeps folder."""
    if _state["bundled"] is None:
        root = str(pydeps_root())
        search = [p for p in sys.path if not str(p).startswith(root)]
        try:
            dist = next(iter(metadata.distributions(name="yt-dlp", path=search)), None)
        except Exception:  # noqa: BLE001 - broken metadata must never stop the app
            dist = None
        _state["bundled"] = display_version(dist.version) if dist is not None else ""
    return _state["bundled"] or None


def activate() -> Path | None:
    """Put the verified user copy at the front of sys.path if it is newer than the bundled one. Idempotent."""
    if _state["activate_called"]:
        return _state["activated"]
    _state["activate_called"] = True
    if os.environ.get(DISABLE_ENV) == "1":
        return None
    if "yt_dlp" in sys.modules:
        log.warning("yt_dlp was imported before updater.activate(); the bundled copy stays in use")
        return None
    current = pydeps_root() / "current"
    marker = _read_marker(current)
    if not marker:
        return None
    bundled = bundled_version()
    if bundled and not _newer(marker["yt_dlp"], bundled):
        log.info("bundled yt-dlp %s is as new as the downloaded %s; using the bundled one", bundled, marker["yt_dlp"])
        return None
    # the resolved folder, not the symlink: a later update swaps `current` without touching this process
    folder = current.resolve()
    sys.path.insert(0, str(folder))
    _state["activated"] = folder
    log.info("using downloaded yt-dlp %s from %s", marker["yt_dlp"], folder)
    return folder


def current_version() -> str:
    """Version of the yt-dlp this process uses (imports yt_dlp.version if nothing has yet)."""
    activate()
    mod = sys.modules.get("yt_dlp.version")
    if mod is None:
        try:
            from yt_dlp import version as mod  # type: ignore[no-redef]
        except Exception:  # noqa: BLE001
            return "unknown"
    return str(getattr(mod, "__version__", "unknown"))


# ---------------------------------------------------------------- PyPI

def _ssl_context() -> ssl.SSLContext:
    # A frozen app's OpenSSL points at the build machine's CA paths; certifi travels with the app.
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _get(url: str, timeout: float = HTTP_TIMEOUT_S) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Seedbox-updater/1 (+yt-dlp self-update)",
                                               "Accept": "application/json, */*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"could not reach {urllib.parse.urlparse(url).netloc}: {e}") from e


def _get_json(url: str) -> dict:
    try:
        return json.loads(_get(url))
    except ValueError as e:
        raise UpdateError(f"bad JSON from {url}: {e}") from e


def latest_version(timeout: float = HTTP_TIMEOUT_S) -> str:
    """Newest yt-dlp release on PyPI, in yt-dlp's spelling. Raises UpdateError."""
    try:
        data = json.loads(_get(PYPI_JSON.format(name="yt-dlp"), timeout))
        return display_version(str(data["info"]["version"]))
    except (KeyError, TypeError, ValueError) as e:
        raise UpdateError(f"unexpected PyPI answer: {e}") from e


def _check() -> None:
    try:
        _state.update(latest=latest_version(), latest_error=None, checked_at=time.time())
    except UpdateError as e:
        log.info("yt-dlp update check failed: %s", e)
        _state.update(latest_error=str(e), checked_at=time.time() - CHECK_INTERVAL_S + RETRY_AFTER_ERROR_S)
    finally:
        _state["checking"] = False


def check_in_background(force: bool = False) -> None:
    """Ask PyPI for the newest yt-dlp on a daemon thread (no-op while one runs or a recent answer exists)."""
    if _state["checking"] or (not force and time.time() - _state["checked_at"] < CHECK_INTERVAL_S):
        return
    _state["checking"] = True
    threading.Thread(target=_check, name="ytdlp-update-check", daemon=True).start()


def cached_latest() -> str | None:
    """Last known newest version (None until the background check answers; starts one if needed)."""
    check_in_background()
    return _state["latest"]


def cached_latest_version() -> str | None:
    """PyPI's newest yt-dlp when it is worth offering (newer than the running and any pending copy), else None."""
    return cached_latest() if update_available() else None


def update_available() -> bool:
    """True when PyPI has a yt-dlp newer than both the running and an already-installed (pending) copy."""
    latest = cached_latest()
    best = max([current_version(), installed_version() or ""], key=_vkey)
    return _newer(latest, best)


def system_fields() -> dict:
    """SystemStatus fields: running version, and `ytdlp_latest` only when an update is worth offering."""
    return {"ytdlp_version": current_version(), "ytdlp_latest": cached_latest_version()}


def active_dir() -> Path | None:
    """The downloaded yt-dlp folder this process put on sys.path, if any."""
    return _state["activated"]


def describe() -> dict:
    """Everything the updater knows (diagnostics / self-test)."""
    running = current_version()
    mod = sys.modules.get("yt_dlp")
    return {"running": running, "module_file": getattr(mod, "__file__", None), "bundled": bundled_version(),
            "installed": installed_version(), "active_dir": str(_state["activated"]) if _state["activated"] else None,
            "pydeps_root": str(pydeps_root()), "latest": _state["latest"], "latest_error": _state["latest_error"]}


# ---------------------------------------------------------------- install

def _wheel(name: str, version: str) -> dict:
    data = _get_json(PYPI_RELEASE_JSON.format(name=name, version=version))
    for f in data.get("urls") or []:
        if f.get("packagetype") == "bdist_wheel" and str(f.get("filename", "")).endswith("-none-any.whl"):
            return {"name": name, "version": data["info"]["version"], "url": f["url"],
                    "sha256": f["digests"]["sha256"], "filename": f["filename"],
                    "requires_dist": data["info"].get("requires_dist") or []}
    raise UpdateError(f"no pure-Python wheel for {name} {version} on PyPI")


def _ejs_pin(requires_dist: list[str]) -> str | None:
    for req in requires_dist:
        if m := _EJS_PIN_RE.match(req):
            return m.group(1)
    return None


def _download(wheel: dict, dest_dir: Path) -> Path:
    body = _get(wheel["url"], timeout=120)
    digest = hashlib.sha256(body).hexdigest()
    if digest != wheel["sha256"]:
        raise UpdateError(f"{wheel['filename']}: checksum mismatch (got {digest[:12]}…)")
    path = dest_dir / wheel["filename"]
    path.write_bytes(body)
    return path


def _extract(wheel_path: Path, target: Path) -> None:
    """Unpack a wheel's importable contents (skipping .data/ scripts and man pages), refusing unsafe paths."""
    with zipfile.ZipFile(wheel_path) as zf:
        for info in zf.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise UpdateError(f"{wheel_path.name}: unsafe path {info.filename!r}")
            if name.parts[0].endswith(".data") or info.is_dir():
                continue
            out = target.joinpath(*name.parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _verify_command(folder: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, VERIFY_FLAG, str(folder)]
    project = str(Path(__file__).resolve().parent.parent)
    code = (f"import sys; sys.path.insert(0, {project!r}); "
            f"from seedbox.updater import verify_main; verify_main({str(folder)!r})")
    return [sys.executable, "-c", code]


def _verify(folder: Path, expected: str) -> dict:
    """Run `verify_main` on `folder` in a fresh process; returns its report or raises UpdateError."""
    env = {**os.environ, DISABLE_ENV: "1"}
    for var in ("PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONHOME"):
        env.pop(var, None)
    try:
        proc = subprocess.run(_verify_command(folder), capture_output=True, text=True, env=env,
                              timeout=VERIFY_TIMEOUT_S, cwd=str(folder), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError(f"verification did not run: {e}") from e
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(_RESULT_PREFIX)]
    try:
        report = json.loads(lines[-1][len(_RESULT_PREFIX):]) if lines else {}
    except ValueError:
        report = {}
    if not report.get("ok"):
        detail = report.get("error") or (proc.stderr.strip().splitlines() or ["no output"])[-1]
        raise UpdateError(f"the new yt-dlp failed its self-check: {detail}")
    if _vkey(report.get("version")) != _vkey(expected):
        raise UpdateError(f"self-check imported yt-dlp {report.get('version')}, expected {expected}")
    return report


def verify_main(folder: str) -> None:
    """Child-process entry: import yt-dlp from `folder` only, sanity-check it, precompile it, print a report."""
    target = Path(folder).resolve()
    sys.path.insert(0, str(target))
    report: dict[str, Any] = {"ok": False}
    try:
        import yt_dlp
        import yt_dlp.version
        import yt_dlp_ejs
        import yt_dlp_ejs.yt.solver as solver
        from yt_dlp.extractor import gen_extractor_classes

        for mod in (yt_dlp, yt_dlp_ejs):
            if not Path(mod.__file__).resolve().is_relative_to(target):
                raise RuntimeError(f"{mod.__name__} was imported from {mod.__file__}, not the new folder")
        if len(solver.core()) < 1000 or len(solver.lib()) < 1000:
            raise RuntimeError("yt-dlp-ejs solver scripts are missing")
        names = {ie.ie_key() for ie in gen_extractor_classes()}
        if not {"Youtube", "YoutubeSearch", "YoutubeMusicSearchURL"} <= names:
            raise RuntimeError("YouTube extractors are missing")
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            ydl.get_info_extractor("Youtube")
            ydl.get_info_extractor("YoutubeMusicSearchURL")
        compiled = compileall.compile_dir(str(target), quiet=2, workers=1)
        report = {"ok": True, "version": yt_dlp.version.__version__, "ejs": yt_dlp_ejs.version,
                  "extractors": len(names), "compiled": bool(compiled)}
    except BaseException as e:  # noqa: BLE001 - the report is the result
        report = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    sys.stdout.write(_RESULT_PREFIX + json.dumps(report) + "\n")
    sys.stdout.flush()
    sys.exit(0 if report["ok"] else 1)


def _point(link: Path, target: Path) -> None:
    """Atomically make `link` a symlink to `target` (relative, so the folder can move)."""
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    os.symlink(os.path.relpath(target, link.parent), tmp)
    os.replace(tmp, link)


def _prune(root: Path) -> None:
    """Delete version folders that `current`, `previous` and this process don't use (and stale staging dirs)."""
    keep = {(root / n).resolve() for n in ("current", "previous") if (root / n).is_symlink()}
    if _state["activated"] is not None:
        keep.add(Path(_state["activated"]).resolve())
    for d in (root / "versions").iterdir():
        if d.is_dir() and d.resolve() not in keep:
            shutil.rmtree(d, ignore_errors=True)


def update_ytdlp(version: str | None = None, force: bool = False) -> dict:
    """Download, verify and install yt-dlp (`version` or PyPI's newest) + its pinned yt-dlp-ejs.

    Returns {ok, version, message}; never raises. The running process keeps its yt-dlp; the new one is used
    from the next launch. `force` reinstalls even when nothing newer is available.
    """
    if not _lock.acquire(blocking=False):
        return {"ok": False, "version": current_version(), "message": "An update is already running."}
    try:
        return _update(version, force)
    except UpdateError as e:
        return {"ok": False, "version": current_version(), "message": f"yt-dlp update failed: {e}"}
    except Exception as e:  # noqa: BLE001
        log.exception("yt-dlp update crashed")
        return {"ok": False, "version": current_version(), "message": f"yt-dlp update failed: {e}"}
    finally:
        _lock.release()


def _update(version: str | None, force: bool) -> dict:
    running = current_version()
    target = display_version(version) if version else latest_version()
    if not version:
        _state.update(latest=target, latest_error=None, checked_at=time.time())
    pending = installed_version()
    if not force:
        if not _newer(target, max([running, pending or ""], key=_vkey)):
            if pending and _vkey(pending) == _vkey(target) and _vkey(pending) != _vkey(running):
                return {"ok": True, "version": pending,
                        "message": f"yt-dlp {pending} is already installed. Restart Seedbox to use it."}
            return {"ok": True, "version": running, "message": f"yt-dlp {running} is up to date."}

    root = pydeps_root()
    versions = root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    ytdlp = _wheel("yt-dlp", target)
    ejs_version = _ejs_pin(ytdlp["requires_dist"])
    ejs = _wheel("yt-dlp-ejs", ejs_version) if ejs_version else _wheel_latest("yt-dlp-ejs")

    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=versions))
    try:
        with tempfile.TemporaryDirectory(prefix=".wheels-", dir=versions) as wheels:
            for w in (ytdlp, ejs):
                _extract(_download(w, Path(wheels)), staging)
        report = _verify(staging, target)
        marker = {"ok": True, "yt_dlp": report["version"], "yt_dlp_ejs": report.get("ejs"),
                  "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "python": f"{sys.version_info[0]}.{sys.version_info[1]}"}
        (staging / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        staging.chmod(0o755)
        final = versions / report["version"]
        if final.exists():  # same version again (force): swap the folder name out of the way first
            old = versions / f".old-{report['version']}-{os.getpid()}"
            os.replace(final, old)
            shutil.rmtree(old, ignore_errors=True)
        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    current, previous = root / "current", root / "previous"
    if current.is_symlink() and current.resolve() != final.resolve() and current.resolve().is_dir():
        _point(previous, current.resolve())
    _point(current, final)
    _prune(root)
    new = report["version"]
    log.info("installed yt-dlp %s (ejs %s) in %s", new, report.get("ejs"), final)
    if _vkey(new) == _vkey(running):
        msg = f"yt-dlp {new} reinstalled."
    else:
        msg = f"yt-dlp {new} installed (was {running}). Restart Seedbox to use it."
    return {"ok": True, "version": new, "message": msg}


def _wheel_latest(name: str) -> dict:
    data = _get_json(PYPI_JSON.format(name=name))
    return _wheel(name, data["info"]["version"])


def rollback() -> dict:
    """Point `current` back at `previous` (or drop the downloaded copy so the bundled one is used)."""
    with _lock:
        root = pydeps_root()
        current, previous = root / "current", root / "previous"
        if previous.is_symlink() and _read_marker(previous):
            target = previous.resolve()
            _point(current, target)
            previous.unlink()
            _prune(root)
            v = _read_marker(target)["yt_dlp"]
            return {"ok": True, "version": v, "message": f"Rolled back to yt-dlp {v}. Restart Seedbox to use it."}
        if current.is_symlink():
            current.unlink()
            _prune(root)
            v = bundled_version() or "bundled"
            return {"ok": True, "version": v, "message": f"Back to the bundled yt-dlp {v} after a restart."}
        return {"ok": False, "version": current_version(), "message": "No downloaded yt-dlp to roll back."}
