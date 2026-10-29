"""Seedbox 2's window: the API (seedbox2.server) on a background thread inside a native pywebview window.

Ported from seedbox/app.py. pywebview picks its platform backend (Cocoa on macOS, WebView2 on Windows, GTK/Qt on
Linux); the macOS-only touches (dark appearance, menu name) are skipped elsewhere. Without pywebview the default
browser opens instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from seedbox2 import __version__

APP_NAME = "Seedbox 2"
DEV_URL = "http://localhost:5174"  # frontend2's Vite (Seedbox 1's frontend uses 5173)
BACKGROUND = "#0b0d10"  # window colour before the page paints (no white flash)
WINDOW_SIZE = (1440, 900)
MIN_SIZE = (1100, 680)
HEALTH_TIMEOUT_S = 30.0
LOG_DIR_ENV = "SEEDBOX_LOG_DIR"
STATIC_DIR_ENV = "SEEDBOX2_STATIC_DIR"
SELF_TEST_MODULES = ("fastapi", "uvicorn", "webview", "musicreco.paths", "musicreco.settings", "seedbox2.server")

log = logging.getLogger("seedbox2.shell")


# ---------------------------------------------------------------- environment

def frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def static_dir() -> Path | None:
    """The built frontend: $SEEDBOX2_STATIC_DIR, the bundle's web/, or frontend2/dist from source (None if absent)."""
    if raw := os.environ.get(STATIC_DIR_ENV, "").strip():
        candidates = [Path(raw).expanduser()]
    elif frozen():
        candidates = [Path(sys._MEIPASS) / "web"]  # type: ignore[attr-defined]
    else:
        candidates = [project_root() / "frontend2" / "dist"]
    return next((d for d in candidates if (d / "index.html").is_file()), None)


def icon_path() -> Path | None:
    """Dock icon when running from source (a frozen app uses its own icon)."""
    p = project_root() / "packaging" / "icon" / "seedbox-512.png"
    return p if not frozen() and p.is_file() else None


def setup_logging(console: bool, debug: bool = False) -> Path:
    """Rotating log file in $SEEDBOX_LOG_DIR (+ stderr when `console`); returns the log file path."""
    log_dir = Path(os.environ.get(LOG_DIR_ENV) or Path.home() / "Library" / "Logs" / APP_NAME).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "seedbox2.log"
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


def redirect_std_streams(log_file: Path) -> None:
    """An app launched from Finder/Explorer has no terminal: send stray prints and tracebacks to the log file."""
    stream = open(log_file, "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115 - lives forever
    sys.stdout = sys.stderr = stream


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
    """Start the API thread; returns seedbox2.server's ServerHandle."""
    from seedbox2.server import DEFAULT_PORT, start_server

    port = args.port if args.port is not None else (DEFAULT_PORT if args.dev else 0)
    static = None if args.dev else static_dir()
    if static is None and not args.dev:
        log.warning("no built frontend found (frontend2/dist); the window will only show the API")
    t0 = time.monotonic()
    handle = start_server(static, port=port, dev=args.dev, log_level="debug" if args.debug else "warning")
    wait_healthy(handle.url, alive=lambda: handle.running)
    log.info("Seedbox 2 %s API on %s (%.2fs, static=%s)", __version__, handle.url, time.monotonic() - t0, static)
    return handle


def _finish(code: int) -> int:
    """Exit now if worker threads would keep the process alive."""
    logging.shutdown()
    if any(t.is_alive() and not t.daemon for t in threading.enumerate() if t is not threading.main_thread()):
        os._exit(code)
    return code


def run_server_only(args: argparse.Namespace) -> int:
    handle = start_api(args)
    print(f"Seedbox 2 {__version__} API on {handle.url}  (Ctrl-C to stop)", flush=True)
    stop = threading.Event()
    try:
        import signal

        sigs = [signal.SIGTERM, signal.SIGINT] + ([signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])
        for sig in sigs:  # SIGINT may be ignored when started with `&`
            signal.signal(sig, lambda *_: stop.set())
        while handle.running and not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    handle.stop()
    return _finish(0)


# ---------------------------------------------------------------- window

def _dark_window(window: Any) -> None:
    """macOS, on the main thread before the window shows: dark appearance and a dark webview backdrop."""
    if sys.platform != "darwin":
        return
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
    """macOS from source: the menu bar says "Python"; call it Seedbox 2."""
    if frozen() or sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle

        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = APP_NAME
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


def _error_html(message: str, log_file: Path) -> str:
    import html

    return (f"<html><body style='background:{BACKGROUND};color:#e8e8e8;font:14px system-ui;padding:48px'>"
            f"<h2 style='color:#ff6b6b'>Seedbox 2 could not start</h2><p>{html.escape(message)}</p>"
            f"<p style='color:#999'>Details are in {html.escape(str(log_file))}</p></body></html>")


def run_in_browser(args: argparse.Namespace) -> int:
    handle = start_api(args)
    webbrowser.open(DEV_URL if args.dev else f"{handle.url}/")
    try:
        while handle.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    handle.stop()
    return _finish(0)


def run_window(args: argparse.Namespace, log_file: Path) -> int:
    try:
        import webview
    except Exception as e:  # noqa: BLE001 - no pywebview (or no GUI toolkit): fall back to the browser
        log.warning("pywebview unavailable (%s); opening the default browser instead", e)
        return run_in_browser(args)

    _rename_menu_app()
    size, min_size = _window_geometry(webview)
    common = dict(width=size[0], height=size[1], min_size=min_size, background_color=BACKGROUND, text_select=True)
    handle = None
    try:
        handle = start_api(args)
        window = webview.create_window(APP_NAME, DEV_URL if args.dev else f"{handle.url}/", **common)
    except Exception as e:  # noqa: BLE001 - show the problem instead of vanishing
        log.exception("startup failed")
        window = webview.create_window(APP_NAME, html=_error_html(str(e), log_file), **common)
    stopped = threading.Event()

    def shutdown() -> None:
        # runs from `closing` too: macOS's Quit menu ends the process inside AppKit, after webview.start can return
        if handle is not None and not stopped.is_set():
            stopped.set()
            log.info("window closing; stopping the server")
            handle.stop(timeout=4.0)

    window.events.before_show += _dark_window
    window.events.closing += shutdown
    icon = icon_path()
    webview.start(gui="cocoa" if sys.platform == "darwin" else None, debug=bool(args.dev or args.debug),
                  private_mode=False, icon=str(icon) if icon else None)  # fmt: skip
    shutdown()
    return _finish(0)


# ---------------------------------------------------------------- self-test

def self_test() -> int:
    """Diagnostics as JSON on stdout: binaries, imports, and the server answering /api/health and serving the UI.
    Exit code 0 when everything passed."""
    import importlib

    from musicreco import paths

    report: dict[str, Any] = {"app": __version__, "frozen": frozen(), "python": sys.version.split()[0],
                              "platform": sys.platform, "executable": sys.executable}
    report["ffmpeg"] = paths.ffmpeg_path()
    report["deno"] = paths.deno_path()
    ok = bool(report["ffmpeg"])
    modules = {}
    for name in SELF_TEST_MODULES:
        try:
            importlib.import_module(name)
            modules[name] = "ok"
        except Exception as e:  # noqa: BLE001
            modules[name] = f"{type(e).__name__}: {e}"
    report["modules"] = modules
    ok &= all(v == "ok" for k, v in modules.items() if k != "webview")  # no window still runs in the browser
    static = static_dir()
    report["frontend"] = str(static) if static else None
    try:
        from seedbox2.server import start_server

        t0 = time.monotonic()
        handle = start_server(static)
        try:
            wait_healthy(handle.url, timeout=10)
            index = urllib.request.urlopen(f"{handle.url}/", timeout=5).status if static else None  # noqa: S310
            report["server"] = {"ok": True, "seconds": round(time.monotonic() - t0, 2), "index": index}
            ok &= static is not None and index == 200
        finally:
            handle.stop()
    except Exception as e:  # noqa: BLE001
        report["server"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ok = False
    report["ok"] = bool(ok)
    print(json.dumps(report, indent=2), flush=True)
    return 0 if ok else 1
