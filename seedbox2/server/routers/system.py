"""/health and /system: is the server up, what is it running on and where are its folders; /system/reveal."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from seedbox2 import __version__, paths

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
