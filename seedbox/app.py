"""The desktop apps: the API server on 127.0.0.1 (background thread) inside a native pywebview window.

`python -m seedbox` (find, preview, download, tag) and `python -m setbox` (sets, transitions, mixes, setlists) from
source, or Seedbox.app (PyInstaller); both run this launcher with their own profile (PROFILES). Flags:
  --server-only   no window; serve until Ctrl-C (tests, bundles, headless use)
  --port N        fixed port (default: a free one; with --dev 8765 for Seedbox / 8766 for Setbox, where Vite proxies
                  /api)
  --dev           the window loads the Vite dev server (Seedbox http://localhost:5173, Setbox http://localhost:5174)
                  instead of the built frontend
  --no-migrate    never move the legacy project downloads/ folder (see maybe_migrate_legacy)
  --self-test     print a JSON report (binaries, modules, yt-dlp, one YouTube Music search + format probe), exit
  --update-ytdlp  download + verify the newest yt-dlp (same as the UI button), print the result, exit

Logs go to ~/Library/Logs/Seedbox/seedbox.log or ~/Library/Logs/Setbox/setbox.log ($SEEDBOX_LOG_DIR overrides).
"""

from __future__ import annotations

from seedbox import updater

updater.activate()  # must precede every yt_dlp import (seedbox.server → musicreco.youtube)

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import logging.handlers  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
import webbrowser  # noqa: E402
from pathlib import Path  # noqa: E402
from collections.abc import Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any, NoReturn  # noqa: E402

from seedbox import __version__  # noqa: E402


@dataclass(frozen=True)
class Profile:
    """One app on the shared launcher."""

    key: str  # seedbox.server app mode
    name: str  # window, menu bar, logs
    dev_port: int  # API port with --dev (its Vite dev server proxies /api there)
    dev_url: str  # the Vite dev server page
    page: str  # the built frontend's HTML entry
    description: str


PROFILES = {
    "seedbox": Profile("seedbox", "Seedbox", 8765, "http://localhost:5173", "index.html",
                       "Seedbox: find songs, preview, download and tag DJ crates"),
    "setbox": Profile("setbox", "Setbox", 8766, "http://localhost:5174/setbox.html", "setbox.html",
                      "Setbox: set order, transitions, vocal stems, full mixes and setlists for your crates"),
}
PROFILE = PROFILES["seedbox"]  # main(app=...) picks the app
APP_NAME = "Seedbox"
DEV_URL = PROFILES["seedbox"].dev_url
DEV_PORT = PROFILES["seedbox"].dev_port
BACKGROUND = "#0b0d10"  # window colour before the page paints (no white flash)
WINDOW_SIZE = (1440, 900)
MIN_SIZE = (1100, 680)
HEALTH_TIMEOUT_S = 30.0
LOG_DIR_ENV = "SEEDBOX_LOG_DIR"
STATIC_DIR_ENV = "SEEDBOX_STATIC_DIR"
NO_MIGRATE_ENV = "SEEDBOX_NO_MIGRATE"
SELF_TEST_QUERY = "Daft Punk - One More Time"
_SELF_TEST_COMMON = ("numpy", "mutagen.id3", "uvicorn", "fastapi", "AppKit", "WebKit", "webview.platforms.cocoa", "seedbox.server")
SELF_TEST_MODULES = {  # what each app loads lazily
    "seedbox": (*_SELF_TEST_COMMON, "musicreco.previews", "cloudscraper"),
    "setbox": (*_SELF_TEST_COMMON, "scipy.signal", "scipy.ndimage", "musicreco.structure", "musicreco.transitions",
               "musicreco.mixplan", "musicreco.mixrender", "musicreco.setmix", "pedalboard", "setbox.routes"),
}

log = logging.getLogger("seedbox.app")


# ---------------------------------------------------------------- environment

def frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def bundle_bin_dir() -> Path | None:
    return Path(sys._MEIPASS) / "bin" if frozen() else None  # type: ignore[attr-defined]


def static_dir() -> Path | None:
    """The built frontend: $SEEDBOX_STATIC_DIR, the bundle's web/, or frontend/dist from source (None if absent)."""
    if raw := os.environ.get(STATIC_DIR_ENV, "").strip():
        candidates = [Path(raw).expanduser()]
    elif frozen():
        candidates = [Path(sys._MEIPASS) / "web"]  # type: ignore[attr-defined]
    else:
        candidates = [project_root() / "frontend" / "dist"]
    return next((d for d in candidates if (d / PROFILE.page).is_file()), None)


def icon_path() -> Path | None:
    """Dock icon when running from source (the frozen app uses its .icns)."""
    icons = project_root() / "packaging" / "icon"
    p = next((i for i in (icons / f"{PROFILE.key}-512.png", icons / "seedbox-512.png") if i.is_file()), None)
    return p if not frozen() else None


def prepare_environment() -> None:
    """Frozen: put the bundled ffmpeg/deno first on PATH and point OpenSSL at certifi's CA bundle."""
    if (bin_dir := bundle_bin_dir()) is not None and bin_dir.is_dir():
        os.environ["PATH"] = os.pathsep.join([str(bin_dir), os.environ.get("PATH") or "/usr/bin:/bin"])
    if frozen() and not os.environ.get("SSL_CERT_FILE"):
        try:
            import certifi

            os.environ["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass


def setup_logging(console: bool, debug: bool = False) -> Path:
    """Rotating log file (+ stderr when `console`); returns the log file path."""
    log_dir = Path(os.environ.get(LOG_DIR_ENV) or Path.home() / "Library" / "Logs" / PROFILE.name).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{PROFILE.key}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)
    for noisy in ("httpx", "httpcore", "urllib3", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def _redirect_std_streams(log_file: Path) -> None:
    """A Finder-launched app has no terminal: send stray prints and thread tracebacks to the log file."""
    stream = open(log_file, "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115 - lives forever
    sys.stdout = sys.stderr = stream


# ---------------------------------------------------------------- legacy downloads/ migration

def legacy_downloads_dir() -> Path:
    """The project's old downloads/ folder (library.LEGACY_DOWNLOADS_ROOT; only meaningful from source)."""
    from musicreco import library

    return Path(getattr(library, "LEGACY_DOWNLOADS_ROOT", project_root() / "downloads"))


def migration_allowed(args: argparse.Namespace) -> bool:
    """Only from source, never when the crates folder is overridden by env (tests/dev) or disabled."""
    from musicreco import settings as msettings

    return not (frozen() or args.no_migrate or os.environ.get(NO_MIGRATE_ENV) == "1"
                or msettings.env_output_root() is not None)


def maybe_migrate_legacy(output_root: Path, legacy: Path | None = None) -> list[Path] | None:
    """Move crates from the project's legacy downloads/ (a real folder) into `output_root` via the CORE helper.

    Returns None when there was nothing to do, else library.migrate_legacy_downloads's list of entries left
    behind (name clashes). `legacy` defaults to <project>/downloads; tests pass temp folders.
    """
    legacy = Path(legacy) if legacy is not None else legacy_downloads_dir()
    root = Path(output_root).expanduser()
    if legacy.is_symlink() or not legacy.is_dir():
        return None
    if root.exists() and root.resolve() == legacy.resolve():
        return None
    if root.resolve().is_relative_to(legacy.resolve()):
        log.warning("crates folder %s is inside the legacy %s; not migrating", root, legacy)
        return None
    from musicreco import library

    left = library.migrate_legacy_downloads(legacy, root)
    if left:
        log.warning("moved legacy crates to %s; %d entries stayed behind: %s", root, len(left),
                    ", ".join(str(p) for p in left))
    else:
        log.info("moved legacy crates from %s to %s (%s is now a symlink)", legacy, root, legacy)
    return left


# ---------------------------------------------------------------- server

def _get_json(url: str, timeout: float = 2.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - our own localhost server
        return json.loads(resp.read())


def wait_healthy(base_url: str, timeout: float = HEALTH_TIMEOUT_S, alive: Any = None) -> None:
    """Poll /api/health until it answers {ok: true}; RuntimeError on timeout or when `alive()` turns False."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        if alive is not None and not alive():
            raise RuntimeError("the server stopped while starting (see the log)")
        try:
            if _get_json(f"{base_url}/api/health").get("ok"):
                return
        except Exception as e:  # noqa: BLE001 - not up yet
            last = e
        time.sleep(0.05)
    raise RuntimeError(f"the server did not answer /api/health within {timeout:.0f}s ({last})")


def start_api(args: argparse.Namespace) -> Any:
    """Load settings, migrate legacy crates if allowed, start the API thread; returns seedbox.server's handle."""
    from musicreco import settings as msettings
    from seedbox.server import start_server

    settings = msettings.load_settings()
    if PROFILE.key == "seedbox" and migration_allowed(args):  # moving crates around is Seedbox's job
        try:
            maybe_migrate_legacy(settings.output_root)
        except Exception:  # noqa: BLE001 - never block startup on it
            log.exception("legacy downloads/ migration failed")
    port = args.port if args.port is not None else (PROFILE.dev_port if args.dev else 0)
    static = None if args.dev else static_dir()
    if static is None and not args.dev:
        log.warning("no built frontend found (frontend/dist); the window will only show the API")
    t0 = time.monotonic()
    handle = start_server(settings, static, port=port, dev=args.dev, log_level="debug" if args.debug else "warning",
                          app=PROFILE.key)
    wait_healthy(handle.url, alive=lambda: handle.running)
    log.info("%s %s API on %s (%.2fs, static=%s)", PROFILE.name, __version__, handle.url, time.monotonic() - t0, static)
    return handle


def _finish(code: int) -> NoReturn:
    """End the process once the server has stopped, skipping the interpreter's teardown. A worker may still be inside
    native code (a vocal split in PyTorch, a render, a download), and tearing the runtime down under it aborts the
    process ("terminate called without an active exception", a crash report on macOS) or keeps it alive. Everything
    worth keeping is on disk by then."""
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


def run_server_only(args: argparse.Namespace) -> int:
    handle = start_api(args)
    print(f"{PROFILE.name} {__version__} API on {handle.url}  (Ctrl-C to stop)", flush=True)
    stop = threading.Event()
    try:
        import signal

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):  # SIGINT may be ignored when started with `&`
            signal.signal(sig, lambda *_: stop.set())
        while handle.running and not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    handle.stop()
    _finish(0)


# ---------------------------------------------------------------- window

def _dark_window(window: Any) -> None:
    """Cocoa tweaks on the main thread before the window shows: dark appearance, dark webview backdrop."""
    try:
        import AppKit
        from webview.platforms.cocoa import BrowserView

        AppKit.NSApplication.sharedApplication().setAppearance_(
            AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        view = BrowserView.instances.get(window.uid)
        if view is not None:
            view.webview.setValue_forKey_(False, "drawsBackground")  # the dark window shows until first paint
            color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0x0B / 255, 0x0D / 255, 0x10 / 255, 1.0)
            if hasattr(view.webview, "setUnderPageBackgroundColor_"):
                view.webview.setUnderPageBackgroundColor_(color)
    except Exception:  # noqa: BLE001 - cosmetic only
        log.debug("dark window tweaks failed", exc_info=True)


def _rename_menu_app() -> None:
    """From source the menu bar says "Python"; call it after the app (the bundle has CFBundleName)."""
    if frozen():
        return
    try:
        from Foundation import NSBundle

        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = PROFILE.name
    except Exception:  # noqa: BLE001
        pass


def _window_geometry(webview: Any) -> tuple[tuple[int, int], tuple[int, int]]:
    try:
        screen = webview.screens[0]
        sw, sh = int(screen.width), int(screen.height)
    except Exception:  # noqa: BLE001
        return WINDOW_SIZE, MIN_SIZE
    size = (min(WINDOW_SIZE[0], sw - 40), min(WINDOW_SIZE[1], sh - 60))
    return size, (min(MIN_SIZE[0], size[0]), min(MIN_SIZE[1], size[1]))


def make_folder_picker(window: Any) -> Callable[[str | None], str | None]:
    """seedbox.server's folder-picker hook: a native folder dialog over `window`.

    Called on a server worker thread: pywebview's Cocoa backend runs the NSOpenPanel on the main thread
    (AppHelper.callAfter) and blocks the caller on a semaphore until the user answers. Calling it from the main
    thread would deadlock, so that is refused. Returns the chosen folder, or None when cancelled.
    """
    import webview

    folder = getattr(getattr(webview, "FileDialog", None), "FOLDER", 20)

    def pick(initial_dir: str | None) -> str | None:
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError("the folder dialog must be opened from a worker thread")
        initial = initial_dir if initial_dir and os.path.isdir(initial_dir) else ""
        chosen = window.create_file_dialog(folder, directory=initial)
        if isinstance(chosen, (list, tuple)):
            chosen = chosen[0] if chosen else None
        return str(chosen) if chosen else None

    return pick


def _error_html(message: str, log_file: Path) -> str:
    import html

    return (f"<html><body style='background:{BACKGROUND};color:#e8e8e8;font:14px -apple-system;padding:48px'>"
            f"<h2 style='color:#ff6b6b'>{PROFILE.name} could not start</h2><p>{html.escape(message)}</p>"
            f"<p style='color:#999'>Details are in {html.escape(str(log_file))}</p></body></html>")


def run_window(args: argparse.Namespace, log_file: Path) -> int:
    try:
        import webview
    except Exception as e:  # noqa: BLE001 - no pywebview/pyobjc: fall back to the browser
        log.warning("pywebview unavailable (%s); opening the default browser instead", e)
        handle = start_api(args)
        webbrowser.open(PROFILE.dev_url if args.dev else handle.url)
        try:
            while handle.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        handle.stop()
        _finish(0)

    _rename_menu_app()
    size, min_size = _window_geometry(webview)
    common = dict(width=size[0], height=size[1], min_size=min_size, background_color=BACKGROUND, text_select=True)
    handle = None
    try:
        handle = start_api(args)
        url = PROFILE.dev_url if args.dev else f"{handle.url}/"
        window = webview.create_window(PROFILE.name, url, **common)
        from seedbox.server import set_folder_picker

        set_folder_picker(make_folder_picker(window))  # POST /api/system/pick-folder
    except Exception as e:  # noqa: BLE001 - show the problem instead of vanishing
        log.exception("startup failed")
        window = webview.create_window(PROFILE.name, html=_error_html(str(e), log_file), **common)
    stopped = threading.Event()

    def shutdown() -> None:
        # runs from `closing` too: the menu's Quit would end the process inside AppKit (exit(), which aborts while a
        # vocal split is inside PyTorch), so the process ends here once the server has stopped
        if handle is not None and not stopped.is_set():
            stopped.set()
            log.info("window closing; stopping the server")
            from seedbox.server import set_folder_picker

            set_folder_picker(None)
            handle.stop(timeout=4.0)
            _finish(0)

    window.events.before_show += _dark_window
    window.events.closing += shutdown
    webview.start(gui="cocoa", debug=bool(args.dev or args.debug), private_mode=False,
                  icon=str(icon_path()) if icon_path() else None)
    shutdown()
    _finish(0)


# ---------------------------------------------------------------- self-test

def self_test(query: str = SELF_TEST_QUERY, clip: bool = False) -> int:
    """Diagnostics as JSON on stdout. Seedbox: binaries, yt-dlp versions, a YT Music search, a format probe (deno), and
    with `clip` a 10 s preview clip + MP3 conversion in a temp dir (ffmpeg without ffprobe). Setbox: ffmpeg, the modules
    the analysis and mixes load, and whether vocal splitting can run. Exit code 0 when everything passed."""
    from musicreco import paths

    report: dict[str, Any] = {"app": PROFILE.name, "version": __version__, "frozen": frozen(),
                              "python": sys.version.split()[0], "executable": sys.executable}
    ok = True

    def run(cmd: list[str]) -> str:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return (out.stdout or out.stderr).strip().splitlines()[0]
        except Exception as e:  # noqa: BLE001
            return f"error: {e}"

    ffmpeg = paths.ffmpeg_path()
    report["ffmpeg"] = {"path": ffmpeg, "version": run([ffmpeg, "-version"]) if ffmpeg else None,
                        "ffprobe_next_to_it": bool(ffmpeg and (Path(ffmpeg).parent / "ffprobe").exists())}
    ok &= bool(ffmpeg)
    report["modules"] = _import_check()
    ok &= all(v == "ok" for v in report["modules"].values())
    if PROFILE.key == "setbox":
        from musicreco import separator

        can, why = separator.available()  # optional: mixes fall back to EQ-only vocals without it
        report["vocal_stems"] = {"available": can, "why_not": why or None, "model": str(separator.model_path()),
                                 "model_downloaded": separator.model_path().is_file()}
        report["ok"] = bool(ok)
        print(json.dumps(report, indent=2), flush=True)
        return 0 if ok else 1
    deno = paths.deno_path()
    report["deno"] = {"path": deno, "version": run([deno, "--version"]) if deno else None}
    ok &= bool(deno)
    report["updater"] = updater.describe()

    from musicreco import youtube

    t0 = time.monotonic()
    try:
        hits = youtube._search_ytmusic(query)  # the app's real search path
        top = next((h.cand for h in hits if h.cand.duration_s), None)
        report["search"] = {"query": query, "hits": len(hits), "seconds": round(time.monotonic() - t0, 2),
                            "top": top and {"title": top.title, "channel": top.channel, "url": top.url,
                                            "duration_s": top.duration_s}}
        ok &= top is not None
    except Exception as e:  # noqa: BLE001
        report["search"] = {"query": query, "error": f"{type(e).__name__}: {e}"}
        top, ok = None, False

    if top is not None:
        report["formats"] = _probe_formats(youtube, top.url)
        ok &= bool(report["formats"].get("audio_formats"))
        # the mweb client needs YouTube's JS challenges solved: proves deno + yt-dlp-ejs work
        report["deno_challenge"] = _probe_formats(youtube, top.url, client="mweb")
        ok &= bool(report["deno_challenge"].get("deno_solved"))
        if clip:
            report["clip"] = _clip_test(youtube, top)
            ok &= bool(report["clip"].get("ok"))
    report["ok"] = bool(ok)
    print(json.dumps(report, indent=2), flush=True)
    return 0 if ok else 1


def _import_check() -> dict[str, str]:
    """Import what the app loads lazily (a missing hidden import shows up here, not mid-session)."""
    import importlib

    result = {}
    for name in SELF_TEST_MODULES[PROFILE.key]:
        try:
            importlib.import_module(name)
            result[name] = "ok"
        except Exception as e:  # noqa: BLE001
            result[name] = f"{type(e).__name__}: {e}"
    if PROFILE.key == "seedbox":
        try:
            import cloudscraper

            cloudscraper.create_scraper()  # reads its bundled browser data files
            result["cloudscraper.create_scraper"] = "ok"
        except Exception as e:  # noqa: BLE001
            result["cloudscraper.create_scraper"] = f"{type(e).__name__}: {e}"
    return result


class _CaptureLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, msg: str) -> None:
        self.lines.append(msg)

    info = warning = error = debug


def _probe_formats(youtube: Any, url: str, client: str | None = None) -> dict:
    """Full metadata extraction (no download), optionally forcing one YouTube player client."""
    import yt_dlp

    logger = _CaptureLogger()
    t0 = time.monotonic()
    try:
        opts = {**youtube._base_opts(), "logger": logger, "verbose": True, "quiet": False, "skip_download": True}
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        fmts = [f for f in info.get("formats") or [] if f.get("acodec") not in (None, "none") and f.get("url")]
        jsc = [ln for ln in logger.lines if "[jsc" in ln and "Providers" not in ln]
        failed = [ln for ln in logger.lines if "challenge solving failed" in ln.lower()]
        return {"client": client or "default", "audio_formats": len(fmts), "seconds": round(time.monotonic() - t0, 2),
                "deno_solved": any("Solving JS challenges using deno" in ln for ln in jsc) and not failed,
                "js_runtime_log": [ln[:160] for ln in jsc[:4]],
                "warnings": [ln[:200] for ln in logger.lines if ln.startswith("WARNING")][:6]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "log_tail": logger.lines[-8:]}


def _clip_test(youtube: Any, cand: Any) -> dict:
    """The app's preview path (youtube.fetch_clip: 10 s section → m4a) plus yt-dlp's FFmpegExtractAudio to MP3 192
    on that clip, in a temp dir that is deleted. Neither step may need ffprobe."""
    import yt_dlp
    from yt_dlp.postprocessor import FFmpegExtractAudioPP

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="seedbox-selftest-") as tmp:
        try:
            clip = youtube.fetch_clip(cand, 30.0, 10.0, Path(tmp) / "clip.m4a")
            logger = _CaptureLogger()
            with yt_dlp.YoutubeDL({**youtube._base_opts(), "logger": logger}) as ydl:
                pp = FFmpegExtractAudioPP(ydl, preferredcodec="mp3", preferredquality="192")
                _files, info = pp.run({"filepath": str(clip), "ext": clip.suffix[1:]})
            mp3 = Path(info["filepath"])
            return {"ok": mp3.suffix == ".mp3" and mp3.stat().st_size > 0, "m4a_bytes": clip.stat().st_size,
                    "mp3_bytes": mp3.stat().st_size, "seconds": round(time.monotonic() - t0, 2),
                    "ffprobe_available": bool(pp.probe_available),
                    "ffprobe_warnings": [ln for ln in logger.lines if "ffprobe" in ln.lower()][:4]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- entry

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog=PROFILE.key, description=PROFILE.description)
    p.add_argument("--server-only", action="store_true", help="run the API without a window")
    p.add_argument("--port", type=int, default=None, help=f"API port (default: free port; {PROFILE.dev_port} with --dev)")
    p.add_argument("--dev", action="store_true", help=f"load the Vite dev server ({PROFILE.dev_url}) in the window")
    p.add_argument("--debug", action="store_true", help="verbose logs, web inspector")
    p.add_argument("--self-test", action="store_true", help="print a diagnostics report and exit")
    if PROFILE.key == "seedbox":  # downloads, YouTube and yt-dlp: Seedbox's side
        p.add_argument("--no-migrate", action="store_true", help="don't move the legacy project downloads/ folder")
        p.add_argument("--self-test-query", default=SELF_TEST_QUERY, help=argparse.SUPPRESS)
        p.add_argument("--self-test-clip", action="store_true", help="with --self-test: also convert a 10 s clip")
        p.add_argument("--update-ytdlp", action="store_true", help="update yt-dlp now (as the UI button does) and exit")
        p.add_argument("--rollback-ytdlp", action="store_true", help=argparse.SUPPRESS)
        p.add_argument("--force", action="store_true", help=argparse.SUPPRESS)  # with --update-ytdlp: reinstall
    else:
        p.set_defaults(no_migrate=True, self_test_query=SELF_TEST_QUERY, self_test_clip=False, update_ytdlp=False,
                       rollback_ytdlp=False, force=False)
    # macOS may pass -psn_0_12345 to apps launched from Finder on old systems
    args, _unknown = p.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None, app: str = "seedbox") -> int:
    """Run `app` ("seedbox" or "setbox"; `python -m setbox` passes the latter)."""
    global PROFILE
    PROFILE = PROFILES[app]
    args = parse_args(argv)
    prepare_environment()
    gui = not (args.server_only or args.self_test or args.update_ytdlp or args.rollback_ytdlp)
    log_file = setup_logging(console=not (gui and frozen()), debug=args.debug)
    if gui and frozen():
        _redirect_std_streams(log_file)
    ytdlp = (f", bundled yt-dlp {updater.bundled_version()}, downloaded yt-dlp {updater.installed_version()}, "
             f"active: {updater.active_dir() or 'bundled'}") if PROFILE.key == "seedbox" else ""
    log.info("%s %s starting (frozen=%s, python %s%s)", PROFILE.name, __version__, frozen(), sys.version.split()[0], ytdlp)
    if PROFILE.key == "seedbox" and os.environ.get("SEEDBOX_NO_UPDATE_CHECK") != "1":
        updater.check_in_background()
    if args.self_test:
        return self_test(args.self_test_query, clip=args.self_test_clip)
    if args.update_ytdlp or args.rollback_ytdlp:
        result = updater.rollback() if args.rollback_ytdlp else updater.update_ytdlp(force=args.force)
        print(json.dumps(result, indent=2), flush=True)
        return 0 if result.get("ok") else 1
    if args.server_only:
        return run_server_only(args)
    return run_window(args, log_file)
