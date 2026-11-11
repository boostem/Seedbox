"""Seedbox 2's window: the API (seedbox2.server) on a background thread inside a native pywebview window.

Ported from seedbox/app.py. The pywebview backend is Cocoa on macOS, EdgeChromium (WebView2) on Windows and GTK or
Qt on Linux (gui_backend), with a dark title bar on each where the platform allows it. Without pywebview (or a GUI
toolkit) the default browser opens instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
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
BUILD_COMMAND = "cd frontend2 && deno install && deno task build"
BUILD_TIMEOUT_S = 600.0
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


def build_frontend() -> Path | None:
    """From source with no frontend2/dist (a fresh clone): build it with deno, and return it. None if it can't be."""
    from musicreco import paths

    if frozen() or os.environ.get(STATIC_DIR_ENV, "").strip():
        return None
    deno = paths.deno_path()
    if deno is None:
        log.warning("no built frontend and no deno to build it with: install deno, then run: %s", BUILD_COMMAND)
        return None
    log.info("building the frontend (only the first time, up to a few minutes): %s", BUILD_COMMAND)
    t0 = time.monotonic()
    for step in (["install"], ["task", "build"]):
        try:
            r = subprocess.run([deno, *step], cwd=project_root() / "frontend2", capture_output=True, text=True,  # noqa: S603
                               timeout=BUILD_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("building the frontend failed (deno %s): %s", " ".join(step), e)
            return None
        if r.returncode != 0:
            log.warning("building the frontend failed (deno %s):\n%s", " ".join(step), (r.stdout + r.stderr)[-2000:])
            return None
    built = static_dir()
    if built is not None:
        log.info("built the frontend in %.0f s", time.monotonic() - t0)
    return built


def icon_path() -> Path | None:
    """Dock icon when running from source (a frozen app uses its own icon)."""
    p = project_root() / "packaging" / "icon" / "seedbox-512.png"
    return p if not frozen() and p.is_file() else None


def setup_logging(console: bool, debug: bool = False) -> Path:
    """Rotating log file in $SEEDBOX_LOG_DIR (+ stderr when `console`); returns the log file path."""
    from seedbox2.paths import data_dirs

    log_dir = Path(os.environ.get(LOG_DIR_ENV, "").strip() or data_dirs()[LOG_DIR_ENV]).expanduser()
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
        static = build_frontend()
    if static is None and not args.dev:
        log.warning("no built frontend found (frontend2/dist); the window only says how to build it")
    t0 = time.monotonic()
    handle = start_server(static, port=port, dev=args.dev, log_level="debug" if args.debug else "warning",
                          start_jobs=True, watch_folders=True, download_models=True, check_updates=True)
    wait_healthy(handle.url, alive=lambda: handle.running)
    log.info("Seedbox 2 %s API on %s (%.2fs, static=%s)", __version__, handle.url, time.monotonic() - t0, static)
    return handle


def install_update_at_exit(handle: Any) -> None:
    """A downloaded update is installed as the app exits (#89): "Restart to update" asked for it, or it was ready."""
    updater = getattr(handle.app.state, "updater", None) if handle is not None else None
    if updater is not None:
        updater.install_if_ready()


def quit_requested(handle: Any) -> bool:
    return bool(handle is not None and handle.app.state.quit.is_set())


def _finish(code: int) -> int:
    """Exit now if worker threads would keep the process alive."""
    logging.shutdown()
    if any(t.is_alive() and not t.daemon for t in threading.enumerate() if t is not threading.main_thread()):
        os._exit(code)
    return code


def run_server_only(args: argparse.Namespace) -> int:
    import signal

    stop = threading.Event()
    # before the server starts: a SIGTERM sent as soon as /api/health answers must stop it cleanly, not kill it
    sigs = [signal.SIGTERM, signal.SIGINT] + ([signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])
    for sig in sigs:  # SIGINT may be ignored when started with `&`
        signal.signal(sig, lambda *_: stop.set())
    handle = start_api(args)
    print(f"Seedbox 2 {__version__} API on {handle.url}  (Ctrl-C to stop)", flush=True)
    try:
        while handle.running and not quit_requested(handle) and not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    handle.stop()
    install_update_at_exit(handle)
    return _finish(0)


# ---------------------------------------------------------------- window

def gui_backend(platform: str = sys.platform, environ: Any = os.environ) -> str:
    """pywebview's backend: Cocoa (WKWebView) on macOS, EdgeChromium (WebView2) on Windows, GTK (WebKitGTK) on
    Linux, or Qt on KDE. pywebview falls back from GTK to Qt and back when one is missing."""
    if platform == "darwin":
        return "cocoa"
    if platform == "win32":
        return "edgechromium"
    return "qt" if "KDE_FULL_SESSION" in environ else "gtk"


def _dark_window(window: Any) -> None:
    """Before the window shows (on the GUI thread): a dark title bar and backdrop where the platform allows it."""
    try:
        if sys.platform == "darwin":
            _dark_cocoa(window)
        elif sys.platform == "win32":
            _dark_title_bar_windows(window)
        else:
            _dark_gtk()
    except Exception:  # noqa: BLE001 - cosmetic only
        log.debug("dark window tweaks failed", exc_info=True)


def _dark_cocoa(window: Any) -> None:
    """macOS, on the main thread: dark appearance and a dark webview backdrop until the first paint."""
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


def _dark_gtk() -> None:
    """GTK: ask the theme for its dark variant (title bar included). Qt follows the desktop's theme by itself."""
    import webview

    if webview.guilib is None or "gtk" not in webview.guilib.__name__:
        return
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    if (settings := Gtk.Settings.get_default()) is not None:
        settings.set_property("gtk-application-prefer-dark-theme", True)


DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # 19 before Windows 10 20H1
DWMWA_CAPTION_COLOR = 35  # Windows 11


def _colorref(hex_color: str) -> int:
    """'#rrggbb' as a Windows COLORREF (0x00bbggrr)."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | g << 8 | b << 16


def _dark_title_bar_windows(window: Any) -> None:
    """Windows, on the UI thread: a dark title bar (Windows 10 20H1+), in the app's colour on Windows 11.
    (pywebview only darkens it when the system theme is dark; Seedbox 2 is always dark.)"""
    import ctypes

    from webview.platforms.winforms import BrowserView

    form = BrowserView.instances.get(window.uid)
    if form is None:
        return
    hwnd = ctypes.c_void_p(form.Handle.ToInt64())
    dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
    on = ctypes.c_int(1)
    for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE - 1):
        if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
            break
    color = ctypes.c_int(_colorref(BACKGROUND))
    dwm.DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(color), ctypes.sizeof(color))


def dropped_paths(event: dict) -> list[str]:
    """The real paths of the files and folders in a pywebview `drop` event (the page itself only sees names)."""
    files = (event.get("dataTransfer") or {}).get("files") or []
    return [f["pywebviewFullPath"] for f in files if isinstance(f, dict) and f.get("pywebviewFullPath")]


def _accept_drops(window: Any, api_url: str) -> None:
    """Files and folders dropped onto the window go to POST /api/library/import (#40). Registered on every page
    load; the page shows the drop overlay and the import's progress."""
    from webview.dom import DOMEventHandler

    def on_drop(event: dict) -> None:
        paths = dropped_paths(event)
        if not paths:
            return
        req = urllib.request.Request(f"{api_url}/api/library/import", data=json.dumps({"paths": paths}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=10):  # noqa: S310 - our own localhost server
                pass
        except OSError as e:
            log.warning("importing %d dropped items failed: %s", len(paths), e)

    try:
        window.dom.document.events.drop += DOMEventHandler(on_drop, prevent_default=True)
    except Exception:  # noqa: BLE001 - an old pywebview without DOM events: dropping does nothing
        log.exception("can't accept dropped files")


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
        while handle.running and not quit_requested(handle):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    handle.stop()
    install_update_at_exit(handle)
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
            install_update_at_exit(handle)

    window.events.before_show += _dark_window
    if handle is not None:
        window.events.loaded += lambda: _accept_drops(window, handle.url)
    window.events.closing += shutdown
    if handle is not None:  # "Restart to update" (#89): the page asks the app to quit
        def close_on_quit() -> None:
            handle.app.state.quit.wait()
            if not stopped.is_set():
                log.info("quitting to install an update")
                window.destroy()

        threading.Thread(target=close_on_quit, name="seedbox2-quit-watch", daemon=True).start()
    icon = icon_path()
    webview.start(gui=gui_backend(), debug=bool(args.dev or args.debug),
                  private_mode=False, icon=str(icon) if icon else None)  # fmt: skip
    shutdown()
    return _finish(0)


# ---------------------------------------------------------------- self-test

def _binary_version(path: str | None) -> str | None:
    """The first line of `path --version`, or None when it doesn't run (say, a binary for another architecture)."""
    if not path:
        return None
    flag = "-version" if Path(path).stem == "ffmpeg" else "--version"
    try:
        r = subprocess.run([path, flag], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,  # noqa: S603
                           check=False, **_no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.partition("\n")[0].strip() or None if r.returncode == 0 else None


def _no_window() -> dict[str, Any]:
    """On Windows, a windowed app's child console programs would each flash a console window."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _window_backend_check() -> str:
    """Import pywebview's backend for this OS without opening a window: "ok", or what went wrong."""
    import importlib

    module = {"darwin": "webview.platforms.cocoa", "win32": "webview.platforms.edgechromium"}.get(sys.platform)
    if module is None:
        return "ok"
    try:
        importlib.import_module(module)
    except Exception as e:  # noqa: BLE001 - the report says what went wrong
        return f"{type(e).__name__}: {e}"
    return "ok"


def self_test(report_file: str | None = None) -> int:
    """Diagnostics as JSON on stdout (and in `report_file`): binaries, imports, onnxruntime running a tiny model, and
    the server answering /api/health and serving the UI. Exit code 0 when everything passed."""
    import importlib

    from musicreco import paths
    from seedbox2.models import selftest

    report: dict[str, Any] = {"app": __version__, "frozen": frozen(), "python": sys.version.split()[0],
                              "platform": sys.platform, "executable": sys.executable}
    report["ffmpeg"] = paths.ffmpeg_path()
    report["deno"] = paths.deno_path()
    report["ffmpeg_version"] = _binary_version(report["ffmpeg"])
    report["deno_version"] = _binary_version(report["deno"])
    ok = bool(report["ffmpeg_version"])
    modules = {}
    for name in SELF_TEST_MODULES:
        try:
            importlib.import_module(name)
            modules[name] = "ok"
        except Exception as e:  # noqa: BLE001
            modules[name] = f"{type(e).__name__}: {e}"
    report["modules"] = modules
    ok &= all(v == "ok" for k, v in modules.items() if k != "webview")  # no window still runs in the browser
    if frozen():  # the backend the installed app will open its window with (packaging/check_seedbox2.py requires it)
        report["window_backend"] = _window_backend_check()
    report["onnxruntime"] = selftest.check()
    ok &= bool(report["onnxruntime"]["ok"])
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
    text = json.dumps(report, indent=2)
    print(text, flush=True)
    if report_file:
        Path(report_file).write_text(text, encoding="utf-8")
    return 0 if ok else 1
