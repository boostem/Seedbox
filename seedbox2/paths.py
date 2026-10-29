"""Seedbox 2's folders on each OS (from platformdirs) and "Show in Finder/Explorer/file manager".

| Folder   | macOS                                | Windows                              | Linux                     |
|----------|--------------------------------------|--------------------------------------|---------------------------|
| settings | ~/Library/Application Support/Seedbox 2 | %LOCALAPPDATA%\\Seedbox 2          | ~/.local/share/seedbox2   |
| cache    | ~/Library/Caches/Seedbox 2           | %LOCALAPPDATA%\\Seedbox 2\\Cache     | ~/.cache/seedbox2         |
| logs     | ~/Library/Logs/Seedbox 2             | %LOCALAPPDATA%\\Seedbox 2\\Logs      | ~/.local/state/seedbox2/log |
| music    | ~/Music/MusicReco                    | the Music folder\\MusicReco          | $XDG_MUSIC_DIR/MusicReco  |

$SEEDBOX_SUPPORT_DIR, $SEEDBOX_CACHE_DIR, $SEEDBOX_LOG_DIR and $MUSICRECO_OUTPUT_ROOT override them. The music
folder is the library shared with Seedbox 1 (musicreco's Settings.output_root).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

APP_NAME = "Seedbox 2"
LINUX_APP_NAME = "seedbox2"  # XDG folders are lower case without spaces
MUSIC_SUBDIR = "MusicReco"

SUPPORT_DIR_ENV = "SEEDBOX_SUPPORT_DIR"
CACHE_DIR_ENV = "SEEDBOX_CACHE_DIR"
LOG_DIR_ENV = "SEEDBOX_LOG_DIR"
MUSIC_DIR_ENV = "MUSICRECO_OUTPUT_ROOT"

# the folders /api/system lists, by name, and the environment variable that overrides each
FOLDER_ENV = {"settings": SUPPORT_DIR_ENV, "cache": CACHE_DIR_ENV, "logs": LOG_DIR_ENV, "music": MUSIC_DIR_ENV}


def _platform_dirs(platform: str) -> Any:
    if platform == "darwin":
        from platformdirs.macos import MacOS as Dirs
    elif platform == "win32":
        from platformdirs.windows import Windows as Dirs
    else:
        from platformdirs.unix import Unix as Dirs
    return Dirs(appname=LINUX_APP_NAME if Dirs.__name__ == "Unix" else APP_NAME, appauthor=False)


def data_dirs(platform: str = sys.platform) -> dict[str, Path]:
    """Seedbox 2's default settings, cache and log folders, keyed by the environment variable that overrides them."""
    dirs = _platform_dirs(platform)
    return {
        SUPPORT_DIR_ENV: Path(dirs.user_data_dir),
        CACHE_DIR_ENV: Path(dirs.user_cache_dir),
        LOG_DIR_ENV: Path(dirs.user_log_dir),
    }


def default_music_dir(platform: str = sys.platform) -> Path:
    """The default music library: MusicReco in the user's Music folder."""
    return Path(_platform_dirs(platform).user_music_dir) / MUSIC_SUBDIR


def apply_data_dirs(environ: MutableMapping[str, str] = os.environ) -> None:
    """Point musicreco and the log at Seedbox 2's folders, leaving any the environment already sets."""
    for var, path in data_dirs().items():
        if not environ.get(var, "").strip():
            environ[var] = str(path)


def folders(environ: Mapping[str, str] = os.environ) -> dict[str, Path]:
    """The folders in use, by name (FOLDER_ENV): the environment's overrides, else the platform defaults."""
    defaults = {**data_dirs(), MUSIC_DIR_ENV: default_music_dir()}
    out = {}
    for name, var in FOLDER_ENV.items():
        raw = environ.get(var, "").strip()
        out[name] = Path(raw).expanduser() if raw else defaults[var]
    return out


# ---------------------------------------------------------------- reveal

def reveal_label(platform: str = sys.platform) -> str:
    """What the reveal button says on this OS."""
    if platform == "darwin":
        return "Show in Finder"
    if platform == "win32":
        return "Show in Explorer"
    return "Show in file manager"


def reveal_command(path: Path, platform: str = sys.platform) -> list[str]:
    """The command that opens a folder, or shows a file selected in its folder, in the OS's file manager."""
    is_dir = path.is_dir()
    if platform == "darwin":
        return ["open", str(path)] if is_dir else ["open", "-R", str(path)]
    if platform == "win32":
        return ["explorer", str(path)] if is_dir else ["explorer", f"/select,{path}"]
    if not is_dir and shutil.which("gdbus"):  # the freedesktop FileManager1 interface selects the file
        return ["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                "--object-path", "/org/freedesktop/FileManager1", "--method",
                "org.freedesktop.FileManager1.ShowItems", f"['{path.resolve().as_uri()}']", ""]  # fmt: skip
    return ["xdg-open", str(path if is_dir else path.parent)]


def reveal(path: Path) -> None:
    """Show `path` in Finder, Explorer or the file manager; returns at once. FileNotFoundError if it doesn't exist."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    cmd = reveal_command(path)
    if cmd[0] == "gdbus":
        try:  # no FileManager1 service (some desktops): open the folder instead
            subprocess.run(cmd, check=True, capture_output=True, timeout=5)
            return
        except (OSError, subprocess.SubprocessError):
            cmd = ["xdg-open", str(path.parent)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603 - fixed program
