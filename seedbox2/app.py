"""Seedbox 2 launcher.

Until seedbox2 has its own server and UI it starts the Seedbox 1 app, with Seedbox 2's own settings, caches and
logs (`data_dirs`), so trying Seedbox 2 never touches Seedbox 1's sets or settings. The music library
(Settings.output_root, ~/Music/MusicReco) stays shared. $SEEDBOX_SUPPORT_DIR, $SEEDBOX_CACHE_DIR and
$SEEDBOX_LOG_DIR still override each folder.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

from seedbox2 import __version__

APP_NAME = "Seedbox 2"


def data_dirs(home: Path | None = None) -> dict[str, Path]:
    """Seedbox 2's settings, cache and log folders, keyed by the environment variable Seedbox 1 reads them from."""
    home = home or Path.home()
    lib = home / "Library"
    return {
        "SEEDBOX_SUPPORT_DIR": lib / "Application Support" / APP_NAME,
        "SEEDBOX_CACHE_DIR": lib / "Caches" / APP_NAME,
        "SEEDBOX_LOG_DIR": lib / "Logs" / APP_NAME,
    }


def apply_data_dirs(environ: MutableMapping[str, str] = os.environ) -> None:
    """Point Seedbox 1 at Seedbox 2's folders, leaving any the environment already sets."""
    for var, path in data_dirs().items():
        if not environ.get(var, "").strip():
            environ[var] = str(path)


def main(argv: list[str] | None = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    if "--version" in args:
        print(f"{APP_NAME} {__version__}")
        return 0
    apply_data_dirs()

    import multiprocessing

    multiprocessing.freeze_support()

    from seedbox.app import main as seedbox_main  # after apply_data_dirs: seedbox.updater reads them on import

    return seedbox_main(args)
