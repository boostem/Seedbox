"""Seedbox 2 launcher: `python -m seedbox2`. Flags:
  --server-only   no window; serve until Ctrl-C (tests, headless use)
  --port N        fixed port (default: a free one; 8766 with --dev, where frontend2's Vite proxies /api)
  --dev           the window loads frontend2's Vite dev server (http://localhost:5174) instead of frontend2/dist
  --debug         verbose logs, web inspector
  --self-test     print a JSON report (binaries, imports, onnxruntime, the server and the built UI), exit
  --report FILE   with --self-test: also write the report to FILE (a windowed app on Windows has no stdout)
  --version       print the version, exit
  --update-ytdlp  download, verify and install the newest yt-dlp (as Settings does), print the result, exit
  --report-problem [FOLDER]  write the "Report a problem" zip (seedbox2/support.py) and print its path, exit

Seedbox 2 keeps its own settings, caches and logs in each OS's usual folders (seedbox2/paths.py), so trying it never
touches Seedbox 1's sets or settings. The music library (Settings.output_root, MusicReco in the Music folder) stays
shared. $SEEDBOX_SUPPORT_DIR, $SEEDBOX_CACHE_DIR and $SEEDBOX_LOG_DIR still override each folder (musicreco reads the
first two).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from seedbox2 import __version__
from seedbox2.paths import APP_NAME, apply_data_dirs, data_dirs  # noqa: F401 - data_dirs re-exported


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="seedbox2", description="Seedbox 2")
    p.add_argument("--server-only", action="store_true", help="run the API without a window")
    p.add_argument("--port", type=int, default=None, help="API port (default: free port; 8766 with --dev)")
    p.add_argument("--dev", action="store_true", help="load frontend2's Vite dev server (localhost:5174) in the window")
    p.add_argument("--debug", action="store_true", help="verbose logs, web inspector")
    p.add_argument("--self-test", action="store_true", help="print a diagnostics report and exit")
    p.add_argument("--report", default=None, help="with --self-test: also write the report to this file")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    p.add_argument("--update-ytdlp", action="store_true", help="update yt-dlp now (as Settings does) and exit")
    p.add_argument("--report-problem", nargs="?", const="", default=None, metavar="FOLDER",
                   help="write a problem report zip (default: reports/ in the logs folder), print its path, exit")
    # macOS may pass -psn_0_12345 to apps launched from Finder on old systems
    args, _unknown = p.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    apply_data_dirs()
    from seedbox2 import ytdlp

    ytdlp.activate()  # a newer yt-dlp downloaded at runtime (#89); before anything imports yt_dlp

    import multiprocessing

    multiprocessing.freeze_support()

    from seedbox2 import shell

    gui = not (args.server_only or args.self_test or args.update_ytdlp or args.report_problem is not None)
    log_file = shell.setup_logging(console=not (gui and shell.frozen()), debug=args.debug)
    if shell.frozen() and (gui or sys.stdout is None):  # a windowed app on Windows has no streams, uvicorn needs them
        shell.redirect_std_streams(log_file)
    shell.log.info("Seedbox 2 %s starting (frozen=%s, yt-dlp from %s)", __version__, shell.frozen(),
                   ytdlp.active_dir() or "the bundle")
    if args.self_test:
        return shell.self_test(args.report)
    if args.update_ytdlp:
        result = ytdlp.update_ytdlp()
        print(result["message"], flush=True)
        return 0 if result["ok"] else 1
    if args.report_problem is not None:
        from seedbox2 import support

        bundle = support.build_bundle(Path(args.report_problem) if args.report_problem else None)
        print(bundle["path"], flush=True)
        return 0
    if gui:
        ytdlp.check_in_background()  # PyPI's newest yt-dlp, for Settings
    if args.server_only:
        return shell.run_server_only(args)
    return shell.run_window(args, log_file)
