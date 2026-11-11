"""Seedbox 2's own updates (#89): find a newer release on GitHub, download and verify it, install it on restart.

- **Check.** Shortly after startup, and then once a day, the installed app asks GitHub's releases API for the newest
  release tagged `seedbox2-v<version>` (TAG_PREFIX) that is newer than this one and has the installer for this
  computer (`Seedbox-2-<version>-macos-arm64.dmg`, `-macos-x86_64.dmg` or `-windows-x64-setup.exe`, as
  packaging/build_seedbox2.py names them). Pre-releases only count when this build is one. The `updates.check`
  setting (settings.json) turns the automatic check off; "Check now" still works. From source (or on Linux) it only
  says that there is a new release and links to it: there is nothing to replace.
- **Download and verify.** The installer goes to <cache>/updates/ in the background; its SHA-256 must match the line for
  it in the release's SHA256SUMS.txt (the release workflow writes one), else the digest GitHub keeps for every asset.
  A mismatch, or a release with neither, is an error and nothing is installed. It is checked again before installing.
- **Install on restart.** "Restart to update" (or quitting while an update is ready) starts a small helper outside the
  app, and the app exits. The helper waits for it to be gone, then on macOS copies the app out of the DMG next to the
  running one and swaps the two (the old one comes back if that fails); on Windows it runs the Inno Setup installer
  silently, which upgrades the same per-user install. Then it starts Seedbox 2 again, when restarting. It writes `ok` or
  `failed: <why>` to install-result.txt, which the next launch reads: a failed install is reported and stays ready.

$SEEDBOX2_UPDATE_REPO (owner/name) points the check at another repository, for testing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform as _platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from seedbox2 import __version__

log = logging.getLogger(__name__)

REPO_ENV = "SEEDBOX2_UPDATE_REPO"
DEFAULT_REPO = "boostem/Seedbox"
TAG_PREFIX = "seedbox2-v"
SUMS_ASSET = "SHA256SUMS.txt"
SETTINGS_KEY = "updates"  # settings.json: {"check": true}
RELEASES_URL = "https://api.github.com/repos/{repo}/releases?per_page=30"
HTTP_TIMEOUT_S = 20.0
STARTUP_DELAY_S = 15.0  # after the window opens: not part of the startup budget
CHECK_INTERVAL_S = 24 * 3600.0
CHUNK = 1 << 20
RESULT_FILE = "install-result.txt"
READY_FILE = "ready.json"

STATES = ("idle", "checking", "up_to_date", "available", "downloading", "ready", "error")


class UpdateError(RuntimeError):
    """A step failed; the message is shown to the user."""


# ---------------------------------------------------------------- releases

def parse_version(text: str | None) -> Any:
    """A comparable PEP 440 version, or None when `text` isn't one."""
    from packaging.version import InvalidVersion, Version

    try:
        return Version(str(text or "").strip())
    except InvalidVersion:
        return None


def machine_name(machine: str) -> str:
    m = machine.lower()
    return {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}.get(m, m)


def asset_suffix(platform: str = sys.platform, machine: str = _platform.machine()) -> str | None:
    """The end of this computer's installer's file name (packaging/build_seedbox2.py), or None (Linux)."""
    if platform == "darwin":
        return f"-macos-{machine_name(machine)}.dmg"
    if platform == "win32" and machine_name(machine) == "x86_64":
        return "-windows-x64-setup.exe"
    return None


def pick_release(releases: list[dict], current: str, suffix: str | None) -> dict | None:
    """The newest release newer than `current`: {version, tag, name, url, notes, published_at, asset, sums}. `asset`
    is the installer for `suffix` (None when there isn't one, or on a platform without installers)."""
    now = parse_version(current)
    best = None
    for rel in releases:
        tag = str(rel.get("tag_name") or "")
        if rel.get("draft") or not tag.startswith(TAG_PREFIX):
            continue
        version = parse_version(tag[len(TAG_PREFIX):])
        if version is None or (now is not None and version <= now):
            continue
        if (version.is_prerelease or rel.get("prerelease")) and (now is None or not now.is_prerelease):
            continue
        assets = [a for a in rel.get("assets") or [] if isinstance(a, dict)]
        asset = next((a for a in assets if suffix and str(a.get("name", "")).endswith(suffix)), None)
        if suffix and asset is None:
            continue  # this release has nothing for this computer (yet: the build may still be uploading)
        if best is None or version > best[0]:
            sums = next((a for a in assets if a.get("name") == SUMS_ASSET), None)
            best = (version, rel, asset, sums)
    if best is None:
        return None
    version, rel, asset, sums = best
    return {"version": str(version), "tag": rel.get("tag_name"), "name": rel.get("name") or rel.get("tag_name"),
            "url": rel.get("html_url"), "notes": str(rel.get("body") or "")[:4000],
            "published_at": rel.get("published_at"),
            "asset": None if asset is None else {"name": asset.get("name"), "size": int(asset.get("size") or 0),
                                                 "url": asset.get("browser_download_url"),
                                                 "digest": asset.get("digest")},
            "sums": None if sums is None else sums.get("browser_download_url")}  # fmt: skip


def sha256_from_sums(text: str, name: str) -> str | None:
    """The hash for `name` in a `sha256sum` listing ("<hex>  <name>" or "<hex> *<name>")."""
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1].lstrip("*").strip() == name and len(parts[0]) == 64:
            return parts[0].lower()
    return None


def expected_sha256(release: dict, fetch: Callable[[str], bytes]) -> str:
    """The installer's SHA-256: from the release's SHA256SUMS.txt, else from GitHub's digest of the asset."""
    asset = release["asset"]
    if release.get("sums"):
        found = sha256_from_sums(fetch(release["sums"]).decode("utf-8", "replace"), asset["name"])
        if found:
            return found
        raise UpdateError(f"{SUMS_ASSET} of {release['tag']} has no line for {asset['name']}")
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:") and len(digest) == 71:
        return digest[7:].lower()
    raise UpdateError(f"{release['tag']} publishes no checksum for {asset['name']}, so it can't be verified")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- install helpers

MAC_HELPER = r"""#!/bin/bash
# Seedbox 2's update helper (seedbox2/update.py): wait for the app to quit, swap in the app from the DMG, start it.
pid="$1" dmg="$2" app="$3" relaunch="$4" result="$5"
mnt=
fail() {
  echo "failed: $*" > "$result"
  [ -n "$mnt" ] && hdiutil detach "$mnt" -quiet -force
  [ "$relaunch" = 1 ] && [ -d "$app" ] && open "$app"
  exit 1
}
for _ in $(seq 1 600); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
kill -0 "$pid" 2>/dev/null && fail "Seedbox 2 did not quit"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/seedbox2-update.XXXXXX") || fail "no temporary folder"
hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$tmp" "$dmg" >/dev/null || fail "could not open $dmg"
mnt="$tmp"
src=$(find "$mnt" -maxdepth 1 -name '*.app' -type d | head -n 1)
[ -n "$src" ] || fail "no app in $dmg"
dir=$(dirname "$app")
staged="$dir/.Seedbox 2 update.app"
old="$dir/.Seedbox 2 old.app"
rm -rf "$staged" "$old"
ditto "$src" "$staged" || { rm -rf "$staged"; fail "could not copy the new app next to $app"; }
hdiutil detach "$mnt" -quiet
mnt=
mv "$app" "$old" || { rm -rf "$staged"; fail "could not move $app aside"; }
mv "$staged" "$app" || { mv "$old" "$app"; fail "could not put the new app in place"; }
rm -rf "$old" "$dmg"
echo ok > "$result"
[ "$relaunch" = 1 ] && open "$app"
exit 0
"""

WINDOWS_HELPER = r"""# Seedbox 2's update helper (seedbox2/update.py): wait for the app to quit, run the setup silently, start it.
param([int]$ProcessId, [string]$Setup, [string]$App, [string]$Relaunch, [string]$Result)
try { Wait-Process -Id $ProcessId -Timeout 120 -ErrorAction Stop } catch {}
if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
  Set-Content -LiteralPath $Result -Value 'failed: Seedbox 2 did not quit'
  exit 1
}
try {
  $p = Start-Process -FilePath $Setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-' -Wait -PassThru
  if ($p.ExitCode -eq 0) {
    Set-Content -LiteralPath $Result -Value 'ok'
    Remove-Item -LiteralPath $Setup -ErrorAction SilentlyContinue
  } else {
    Set-Content -LiteralPath $Result -Value "failed: the installer exited with code $($p.ExitCode)"
  }
} catch {
  Set-Content -LiteralPath $Result -Value "failed: $($_.Exception.Message)"
}
if ($Relaunch -eq '1' -and (Test-Path -LiteralPath $App)) { Start-Process -FilePath $App }
"""


def mac_app_bundle(executable: str) -> Path | None:
    """The .app the frozen executable (<app>/Contents/MacOS/<name>) belongs to."""
    path = Path(executable).resolve()
    app = path.parents[2] if len(path.parents) > 2 else None
    return app if app is not None and app.suffix == ".app" and path.parent.name == "MacOS" else None


def install_command(installer: Path, folder: Path, *, platform: str, executable: str, pid: int,
                    relaunch: bool) -> list[str]:
    """Write the helper for `platform` into `folder` and return the command that runs it."""
    result = str(folder / RESULT_FILE)
    if platform == "darwin":
        app = mac_app_bundle(executable)
        if app is None:
            raise UpdateError(f"{executable} isn't inside an app bundle")
        script = folder / "install.sh"
        script.write_text(MAC_HELPER, encoding="utf-8")
        return ["/bin/bash", str(script), str(pid), str(installer), str(app), "1" if relaunch else "0", result]
    if platform == "win32":
        script = folder / "install.ps1"
        script.write_text(WINDOWS_HELPER, encoding="utf-8")
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle",
                "Hidden", "-File", str(script), "-ProcessId", str(pid), "-Setup", str(installer), "-App", executable,
                "-Relaunch", "1" if relaunch else "0", "-Result", result]  # fmt: skip
    raise UpdateError(f"no installer for {platform}")


def _spawn_detached(cmd: list[str], log_path: Path) -> None:
    """Start `cmd` so that it outlives this process."""
    with open(log_path, "ab") as out:
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, creationflags=flags,  # noqa: S603
                             close_fds=True)
        else:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,  # noqa: S603
                             close_fds=True)


# ---------------------------------------------------------------- the updater

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


class AppUpdater:
    """Checks, downloads and installs Seedbox 2's updates (see the module docstring). Thread-safe."""

    def __init__(self, settings: Any = None, *, cache_dir: Path, publish: Callable[..., None] | None = None,
                 current: str = __version__, platform: str = sys.platform, machine: str = _platform.machine(),
                 installable: bool | None = None, executable: str = sys.executable,
                 open_url: Callable[[str, str], Any] | None = None, repo: str | None = None) -> None:  # fmt: skip
        self.settings = settings
        self.folder = Path(cache_dir) / "updates"
        self.current = current
        self.platform = platform
        self.suffix = asset_suffix(platform, machine)
        self.installable = (_frozen() if installable is None else installable) and self.suffix is not None
        self.executable = executable
        self.repo = repo or os.environ.get(REPO_ENV, "").strip() or DEFAULT_REPO
        self._open_url = open_url or self._urlopen
        self._publish = publish
        self._lock = threading.Lock()
        self._busy = threading.Lock()  # one check/download at a time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.install_on_exit = False  # "Restart to update" was pressed
        self.relaunch = False
        self._state: dict[str, Any] = {"state": "idle", "latest": None, "progress": None, "error": None,
                                       "checked_at": None, "ready": None}  # fmt: skip
        self._load()

    # ---- settings

    @property
    def enabled(self) -> bool:
        raw = self.settings.get(SETTINGS_KEY, {}) if self.settings is not None else {}
        return not (isinstance(raw, dict) and raw.get("check") is False)

    def set_enabled(self, on: bool) -> dict:
        if self.settings is not None:
            raw = self.settings.get(SETTINGS_KEY, {})
            self.settings.set(SETTINGS_KEY, {**(raw if isinstance(raw, dict) else {}), "check": bool(on)})
        if on:
            self.start()
        return self.status()

    # ---- state

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)
        if self._publish is not None:
            try:
                self._publish("update.status", self.status(), key="update.status")
            except Exception:  # noqa: BLE001 - events are best effort
                log.debug("publishing the update status failed", exc_info=True)

    def status(self) -> dict:
        with self._lock:
            s = dict(self._state)
        reason = None
        if not self.installable:
            reason = ("Seedbox 2 runs from source here: update it with git." if not _frozen() or self.suffix is None
                      else "There is no installer for this computer.")
        return {"current": self.current, "enabled": self.enabled, "installable": self.installable, "reason": reason,
                "state": s["state"], "latest": s["latest"], "progress": s["progress"], "error": s["error"],
                "checked_at": s["checked_at"], "install_on_exit": self.install_on_exit,
                "releases_url": f"https://github.com/{self.repo}/releases"}  # fmt: skip

    def _load(self) -> None:
        """An update downloaded by an earlier run: ready, installed (then tidy up), or failed to install."""
        result = None
        try:
            result = (self.folder / RESULT_FILE).read_text("utf-8", "replace").strip()
            (self.folder / RESULT_FILE).unlink()
        except OSError:
            pass
        try:
            ready = json.loads((self.folder / READY_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            return
        version, now = parse_version(ready.get("version")), parse_version(self.current)
        installer = self.folder / str(ready.get("file") or "")
        if version is None or (now is not None and version <= now) or not installer.is_file():
            log.info("update %s is installed or gone; removing its download", ready.get("version"))
            shutil.rmtree(self.folder, ignore_errors=True)
            return
        error = None
        if result and result.startswith("failed"):
            error = f"The update to {version} didn't install ({result[7:].strip() or 'see the log'})."
            log.warning("the last update didn't install: %s", result)
        self._state.update(state="ready", latest=ready.get("release"), ready=ready, error=error)

    # ---- network

    def _urlopen(self, url: str, accept: str) -> Any:
        from seedbox2.ytdlp import ssl_context

        if urllib.parse.urlparse(url).scheme != "https":
            raise UpdateError(f"refusing to download from {url}")
        req = urllib.request.Request(url, headers={"User-Agent": f"Seedbox2/{self.current} (update check)",
                                                   "Accept": accept})
        try:
            return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S, context=ssl_context())  # noqa: S310 - https
        except urllib.error.HTTPError as e:
            if e.code == 404 and "api.github.com" in url:
                raise UpdateError(f"GitHub has no releases of {self.repo} to show (is the repository private?)") from e
            raise UpdateError(f"{urllib.parse.urlparse(url).netloc} answered {e.code} {e.reason}") from e
        except Exception as e:  # noqa: BLE001
            raise UpdateError(f"could not reach {urllib.parse.urlparse(url).netloc}: {e}") from e

    def _fetch(self, url: str, accept: str = "application/octet-stream") -> bytes:
        with self._open_url(url, accept) as resp:
            return resp.read()

    def releases(self) -> list[dict]:
        body = self._fetch(RELEASES_URL.format(repo=self.repo), "application/vnd.github+json")
        try:
            data = json.loads(body)
        except ValueError as e:
            raise UpdateError(f"GitHub answered something that isn't JSON: {e}") from e
        if not isinstance(data, list):
            raise UpdateError(f"GitHub's answer has no releases: {str(data)[:200]}")
        return data

    # ---- check and download

    def start(self) -> None:
        """Check after STARTUP_DELAY_S, then every CHECK_INTERVAL_S, while the setting is on and the app can update
        itself. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        if not (self.enabled and self.installable):
            return
        self._thread = threading.Thread(target=self._run, name="seedbox2-update-check", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        delay = STARTUP_DELAY_S
        while not self._stop.wait(delay):
            delay = CHECK_INTERVAL_S
            if self.enabled:
                self.check()

    def close(self) -> None:
        self._stop.set()

    def check_in_background(self) -> dict:
        if not self._busy.locked():
            threading.Thread(target=self.check, name="seedbox2-update-check-now", daemon=True).start()
        return self.status()

    def check(self) -> dict:
        """Ask GitHub, and download a newer installer when there is one. Never raises."""
        if not self._busy.acquire(blocking=False):
            return self.status()
        try:
            if self.status()["state"] == "ready":
                return self.status()
            self._set(state="checking", error=None)
            release = pick_release(self.releases(), self.current, self.suffix)
            checked = time.time()
            if release is None:
                self._set(state="up_to_date", latest=None, checked_at=checked)
            elif not self.installable or release["asset"] is None:
                self._set(state="available", latest=release, checked_at=checked)
            else:
                self._set(latest=release, checked_at=checked)
                self._download(release)
        except UpdateError as e:
            log.info("update check failed: %s", e)
            self._set(state="error", error=str(e), progress=None, checked_at=time.time())
        except Exception as e:  # noqa: BLE001
            log.exception("update check crashed")
            self._set(state="error", error=f"{type(e).__name__}: {e}", progress=None, checked_at=time.time())
        finally:
            self._busy.release()
        return self.status()

    def _download(self, release: dict) -> None:
        asset = release["asset"]
        name = Path(str(asset["name"])).name
        sha = expected_sha256(release, self._fetch)
        self.folder.mkdir(parents=True, exist_ok=True)
        for old in self.folder.iterdir():  # an older download
            if old.is_file() and old.name not in (name, f"{name}.part"):
                old.unlink(missing_ok=True)
        final, part = self.folder / name, self.folder / f"{name}.part"
        if not (final.is_file() and sha256_of(final) == sha):
            total = int(asset.get("size") or 0)
            self._set(state="downloading", progress={"done": 0, "total": total})
            h, done, last = hashlib.sha256(), 0, 0.0
            with self._open_url(str(asset["url"]), "application/octet-stream") as resp, open(part, "wb") as out:
                while chunk := resp.read(CHUNK):
                    if self._stop.is_set():
                        raise UpdateError("cancelled")
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if time.monotonic() - last > 0.25:
                        last = time.monotonic()
                        self._set(progress={"done": done, "total": total})
            if h.hexdigest() != sha:
                part.unlink(missing_ok=True)
                raise UpdateError(f"{name} doesn't match its checksum (got {h.hexdigest()[:12]}…, expected "
                                  f"{sha[:12]}…); it was deleted")  # fmt: skip
            os.replace(part, final)
        ready = {"version": release["version"], "file": name, "sha256": sha, "release": release}
        (self.folder / READY_FILE).write_text(json.dumps(ready, indent=2), encoding="utf-8")
        log.info("update %s downloaded and verified: %s", release["version"], final)
        with self._lock:
            self._state["ready"] = ready
        self._set(state="ready", progress=None, error=None)

    # ---- install

    def request_install(self, relaunch: bool = True) -> dict:
        """"Restart to update": the app installs the update as it exits. UpdateError when none is ready."""
        if self.status()["state"] != "ready":
            raise UpdateError("No update is ready to install.")
        self.install_on_exit, self.relaunch = True, relaunch
        return self.status()

    def install_if_ready(self) -> bool:
        """At exit: start the helper when an update is ready (restarting when that was asked for). Never raises."""
        with self._lock:
            ready = self._state.get("ready") if self._state["state"] == "ready" else None
        if ready is None or not self.installable:
            return False
        try:
            installer = self.folder / ready["file"]
            if sha256_of(installer) != ready["sha256"]:
                raise UpdateError(f"{installer.name} changed since it was verified")
            (self.folder / RESULT_FILE).unlink(missing_ok=True)
            cmd = install_command(installer, self.folder, platform=self.platform, executable=self.executable,
                                  pid=os.getpid(), relaunch=self.install_on_exit and self.relaunch)  # fmt: skip
            _spawn_detached(cmd, self.folder / "install.log")
        except (OSError, UpdateError) as e:
            log.warning("couldn't start installing the update: %s", e)
            return False
        log.info("installing update %s after exit (%s)", ready["version"], cmd[0])
        return True
