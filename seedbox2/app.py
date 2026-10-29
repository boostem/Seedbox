"""Seedbox 2 launcher: `python -m seedbox2`. Flags:
  --server-only   no window; serve until Ctrl-C (tests, headless use)
  --port N        fixed port (default: a free one; 8766 with --dev, where frontend2's Vite proxies /api)
  --dev           the window loads frontend2's Vite dev server (http://localhost:5174) instead of frontend2/dist
  --debug         verbose logs, web inspector
  --self-test     print a JSON report (binaries, imports, the server and the built UI), exit
  --version       print the version, exit

Seedbox 2 keeps its own settings, caches and logs (`data_dirs`), so trying it never touches Seedbox 1's sets or
settings. The music library (Settings.output_root, ~/Music/MusicReco) stays shared. $SEEDBOX_SUPPORT_DIR,
$SEEDBOX_CACHE_DIR and $SEEDBOX_LOG_DIR still override each folder (musicreco reads the first two).
"""

from __future__ import annotations

import argparse
import os
from collections.abc import MutableMapping
from pathlib import Path

from seedbox2 import __version__

APP_NAME = "Seedbox 2"


def data_dirs(home: Path | None = None) -> dict[str, Path]:
    """Seedbox 2's settings, cache and log folders, keyed by the environment variable they are read from."""
    home = home or Path.home()
    lib = home / "Library"
    return {
        "SEEDBOX_SUPPORT_DIR": lib / "Application Support" / APP_NAME,
        "SEEDBOX_CACHE_DIR": lib / "Caches" / APP_NAME,
        "SEEDBOX_LOG_DIR": lib / "Logs" / APP_NAME,
    }


def apply_data_dirs(environ: MutableMapping[str, str] = os.environ) -> None:
    """Point musicreco and the log at Seedbox 2's folders, leaving any the environment already sets."""
    for var, path in data_dirs().items():
        if not environ.get(var, "").strip():
            environ[var] = str(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="seedbox2", description="Seedbox 2")
    p.add_argument("--server-only", action="store_true", help="run the API without a window")
    p.add_argument("--port", type=int, default=None, help="API port (default: free port; 8766 with --dev)")
    p.add_argument("--dev", action="store_true", help="load frontend2's Vite dev server (localhost:5174) in the window")
    p.add_argument("--debug", action="store_true", help="verbose logs, web inspector")
    p.add_argument("--self-test", action="store_true", help="print a diagnostics report and exit")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    # macOS may pass -psn_0_12345 to apps launched from Finder on old systems
    args, _unknown = p.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    apply_data_dirs()

    import multiprocessing

    multiprocessing.freeze_support()

    from seedbox2 import shell

    gui = not (args.server_only or args.self_test)
    log_file = shell.setup_logging(console=not (gui and shell.frozen()), debug=args.debug)
    if gui and shell.frozen():
        shell.redirect_std_streams(log_file)
    shell.log.info("Seedbox 2 %s starting (frozen=%s)", __version__, shell.frozen())
    if args.self_test:
        return shell.self_test()
    if args.server_only:
        return shell.run_server_only(args)
    return shell.run_window(args, log_file)
