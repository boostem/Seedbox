"""Where the external binaries are: ffmpeg and deno, in the app bundle, a dev override folder or the system.

Lookup order (first hit wins): $SEEDBOX_BIN_DIR, the PyInstaller bundle's `bin/`, PATH, the usual install folders
of this OS (`extra_dirs`: Homebrew on Apple silicon and Intel and MacPorts on macOS; winget, Scoop and Chocolatey on
Windows; /usr/local/bin, ~/.local/bin and snaps on Linux; ~/.deno/bin everywhere), and for ffmpeg finally the
static build shipped with the imageio-ffmpeg package. Windows binaries end in `.exe`. GUI apps start with a bare
PATH, so the explicit locations matter. Nothing is cached: each call reflects the current environment.

The bundled/imageio ffmpeg comes without ffprobe; yt-dlp then probes codecs with `ffmpeg -i`, which is all
FFmpegExtractAudio needs (see youtube._base_opts).
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

BIN_DIR_ENV = "SEEDBOX_BIN_DIR"


def bundled() -> bool:
    """True when running from the frozen (PyInstaller) app."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def exe_name(name: str, platform: str = sys.platform) -> str:
    """The file name of a program: `ffmpeg.exe` on Windows, `ffmpeg` elsewhere."""
    return f"{name}.exe" if platform == "win32" and not name.lower().endswith(".exe") else name


def extra_dirs(platform: str = sys.platform, environ: Mapping[str, str] = os.environ) -> list[Path]:
    """Install folders outside a GUI app's PATH where package managers put ffmpeg and deno, for this OS."""
    home = Path.home()
    if platform == "darwin":
        dirs = [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/opt/local/bin")]
    elif platform == "win32":
        dirs = []
        if local := environ.get("LOCALAPPDATA"):
            dirs.append(Path(local) / "Microsoft" / "WinGet" / "Links")
        dirs.append(Path(environ.get("SCOOP") or home / "scoop") / "shims")
        choco = environ.get("ChocolateyInstall") or Path(environ.get("ProgramData", r"C:\ProgramData")) / "chocolatey"
        dirs.append(Path(choco) / "bin")
        if program_files := environ.get("ProgramFiles"):
            dirs.append(Path(program_files) / "ffmpeg" / "bin")
    else:
        dirs = [Path("/usr/local/bin"), home / ".local" / "bin", Path("/snap/bin"), Path("/usr/bin")]
    return [*dirs, home / ".deno" / "bin"]


def _executable(path: Path) -> str | None:
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def _find(name: str) -> str | None:
    exe = exe_name(name)
    dirs = [os.environ.get(BIN_DIR_ENV, "").strip()]
    if meipass := getattr(sys, "_MEIPASS", None):
        dirs.append(os.path.join(meipass, "bin"))
    for d in filter(None, dirs):
        if hit := _executable(Path(d).expanduser() / exe):
            return hit
    if hit := shutil.which(name):  # also finds name.exe through PATHEXT on Windows
        return hit
    return next(filter(None, (_executable(d / exe) for d in extra_dirs())), None)


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
