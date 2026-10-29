"""/health and /system: is the server up, and what is it running on."""

from __future__ import annotations

import platform
import sys

from fastapi import APIRouter, Request

from seedbox2 import __version__

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/system")
def system(request: Request) -> dict:
    from musicreco import paths  # reused from Seedbox 1, not forked

    static = request.app.state.static_dir
    return {
        "app": "Seedbox 2",
        "version": __version__,
        "python": platform.python_version(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "ffmpeg": paths.ffmpeg_path(),
        "deno": paths.deno_path(),
        "frontend": str(static) if static is not None else None,
    }
