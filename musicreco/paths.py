"""Where the external binaries are: ffmpeg and deno, in the app bundle, a dev override folder or the system.

Lookup order (first hit wins): $SEEDBOX_BIN_DIR, the PyInstaller bundle's `bin/`, PATH, /opt/homebrew/bin,
and for ffmpeg finally the static build shipped with the imageio-ffmpeg package. GUI apps start with a bare
PATH, so the explicit locations matter. Nothing is cached: each call reflects the current environment.

The bundled/imageio ffmpeg comes without ffprobe; yt-dlp then probes codecs with `ffmpeg -i`, which is all
FFmpegExtractAudio needs (see youtube._base_opts).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

BIN_DIR_ENV = "SEEDBOX_BIN_DIR"
BREW_BIN = Path("/opt/homebrew/bin")


def bundled() -> bool:
    """True when running from the frozen (PyInstaller) app."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def _executable(path: Path) -> str | None:
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def _find(name: str) -> str | None:
    dirs = [os.environ.get(BIN_DIR_ENV, "").strip()]
    if meipass := getattr(sys, "_MEIPASS", None):
        dirs.append(os.path.join(meipass, "bin"))
    for d in filter(None, dirs):
        if hit := _executable(Path(d).expanduser() / name):
            return hit
    return shutil.which(name) or _executable(BREW_BIN / name)


def ffmpeg_path() -> str | None:
    """Absolute path of an ffmpeg binary, or None if there is none anywhere."""
    if hit := _find("ffmpeg"):
        return hit
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - package missing or no binary for this platform
        return None
    return _executable(Path(exe)) if exe else None


def deno_path() -> str | None:
    """Absolute path of the deno binary yt-dlp uses for YouTube's JS challenges, or None."""
    return _find("deno")
