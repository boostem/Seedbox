"""/health and /system: is the server up, what is it running on and where are its folders; /system/reveal.

/system/updates (#89): Seedbox 2's own updates (seedbox2/update.py) and yt-dlp's (seedbox2/ytdlp.py), and
/system/report: "Report a problem", a zip of logs, system info, the self-test and the analysis versions.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from seedbox2 import __version__, paths, ytdlp
from seedbox2.server.deps import UpdaterDep
from seedbox2.update import UpdateError

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/system")
def system(request: Request) -> dict:
    from musicreco import paths as binaries  # reused from Seedbox 1, not forked

    static = request.app.state.static_dir
    return {
        "app": "Seedbox 2",
        "version": __version__,
        "python": platform.python_version(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "ffmpeg": binaries.ffmpeg_path(),
        "deno": binaries.deno_path(),
        "frontend": str(static) if static is not None else None,
        "folders": {name: str(path) for name, path in paths.folders().items()},
        "reveal_label": paths.reveal_label(),
    }


class RevealBody(BaseModel):
    path: str


@router.post("/system/reveal")
def reveal(body: RevealBody) -> dict:
    """Show a file or folder in Finder / Explorer / the file manager. Only inside Seedbox 2's folders (/system)."""
    target = Path(body.path).expanduser()
    roots = paths.folders()
    if not any(_inside(target, root) for root in roots.values()):
        raise HTTPException(403, "only Seedbox 2's folders can be shown")
    if target in (roots["settings"], roots["cache"], roots["logs"]):
        target.mkdir(parents=True, exist_ok=True)  # ours: there is nothing to show until something is written
    try:
        paths.reveal(target)
    except FileNotFoundError:
        raise HTTPException(404, f"{target} doesn't exist yet") from None
    except OSError as e:
        raise HTTPException(500, f"couldn't open the file manager ({e})") from None
    return {"ok": True}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- updates (#89)

@router.get("/system/updates")
def updates(updater: UpdaterDep) -> dict:
    return {"app": updater.status(), "ytdlp": ytdlp.status()}


class UpdateSettingsBody(BaseModel):
    check: bool


@router.post("/system/updates/settings")
def update_settings(body: UpdateSettingsBody, updater: UpdaterDep) -> dict:
    """Turn the check at startup on or off."""
    return {"app": updater.set_enabled(body.check), "ytdlp": ytdlp.status()}


@router.post("/system/updates/check")
def check_updates(updater: UpdaterDep) -> dict:
    """Check GitHub (and download a newer installer) and PyPI now, in the background: poll /system/updates."""
    ytdlp.check_in_background(force=True)
    return {"app": updater.check_in_background(), "ytdlp": ytdlp.status()}


class InstallBody(BaseModel):
    relaunch: bool = True


@router.post("/system/updates/install")
def install_update(body: InstallBody, request: Request, updater: UpdaterDep) -> dict:
    """"Restart to update": the app quits, installs the downloaded update and starts again."""
    try:
        status = updater.request_install(relaunch=body.relaunch)
    except UpdateError as e:
        raise HTTPException(409, str(e)) from None
    request.app.state.quit.set()  # seedbox2/shell.py closes the window; the install starts as the app exits
    return {"app": status, "ytdlp": ytdlp.status()}


@router.post("/system/ytdlp/update")
def update_ytdlp() -> dict:
    """Download, verify and install PyPI's newest yt-dlp now (used from the next launch). Takes up to a minute."""
    result = ytdlp.update_ytdlp()
    if not result["ok"]:
        raise HTTPException(502, result["message"])
    return {**result, "ytdlp": ytdlp.status()}


@router.post("/system/ytdlp/rollback")
def rollback_ytdlp() -> dict:
    result = ytdlp.rollback()
    if not result["ok"]:
        raise HTTPException(409, result["message"])
    return {**result, "ytdlp": ytdlp.status()}


# ---------------------------------------------------------------- report a problem (#89)

@router.post("/system/report")
def report_problem(request: Request, updater: UpdaterDep) -> dict:
    """Write the problem report zip (seedbox2/support.py) into reports/ in the logs folder. Runs the self-test in
    another process, so it takes a little while."""
    from seedbox2 import support

    try:
        return support.build_bundle(extra_system={"update": updater.status(), "url": str(request.base_url)})
    except OSError as e:
        raise HTTPException(500, f"couldn't write the report ({e})") from None
