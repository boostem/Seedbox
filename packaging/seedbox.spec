# PyInstaller spec for Seedbox.app (onedir, windowed, arm64). Normally run through packaging/build.sh:
#   .venv/bin/python -m PyInstaller --noconfirm --clean --distpath <out>/dist --workpath <out>/build packaging/seedbox.spec
#
# Env:
#   SEEDBOX_WEB_DIST   built frontend (default <project>/frontend/dist; must contain index.html)
#   SEEDBOX_DENO       deno binary to bundle (default /opt/homebrew/bin/deno, only if it links system libs only)
#   SEEDBOX_FFMPEG     ffmpeg binary to bundle (default: imageio-ffmpeg's static build)
#   SEEDBOX_BUNDLE_ID  CFBundleIdentifier (default app.seedbox.desktop)
# The bundle gets Contents/Frameworks/bin/{ffmpeg,deno} (= sys._MEIPASS/bin, found by musicreco.paths) and
# Contents/Resources/web (the SPA, = sys._MEIPASS/web, found by seedbox.app.static_dir).
# -*- mode: python -*-

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

PROJECT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is injected by PyInstaller
WORK = Path(workpath)  # noqa: F821
SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")


def fail(msg):
    sys.exit(f"seedbox.spec: {msg}")


def app_version():
    text = (PROJECT / "seedbox" / "__init__.py").read_text(encoding="utf-8")
    if m := re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M):
        return m.group(1)
    fail("no __version__ in seedbox/__init__.py")


def linked_libs(path):
    out = subprocess.run(["otool", "-L", str(path)], capture_output=True, text=True, check=True).stdout
    return [ln.strip().split(" (")[0] for ln in out.splitlines()[1:] if ln.strip()]


def check_portable(path, what):
    """A bundled executable must only link macOS system libraries (no Homebrew dylibs)."""
    bad = [lib for lib in linked_libs(path) if not lib.startswith(SYSTEM_PREFIXES)]
    if bad:
        fail(f"{what} ({path}) links non-system libraries {bad}; it would not run on other Macs. "
             "Use packaging/build.sh (downloads the official self-contained deno) or set SEEDBOX_DENO.")
    arch = subprocess.run(["lipo", "-archs", str(path)], capture_output=True, text=True).stdout.split()
    if "arm64" not in arch:
        fail(f"{what} ({path}) has no arm64 slice ({arch})")


def stage_binary(src, name):
    stage = WORK / "stage_bin"
    stage.mkdir(parents=True, exist_ok=True)
    dest = stage / name
    shutil.copy2(os.path.realpath(src), dest)
    dest.chmod(0o755)
    return dest


def minos(path):
    out = subprocess.run(["otool", "-l", str(path)], capture_output=True, text=True).stdout
    m = re.search(r"cmd LC_BUILD_VERSION.*?minos ([0-9.]+)", out, re.S) or \
        re.search(r"cmd LC_VERSION_MIN_MACOSX.*?version ([0-9.]+)", out, re.S)
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


# ---- external binaries
ffmpeg_src = os.environ.get("SEEDBOX_FFMPEG")
if not ffmpeg_src:
    import imageio_ffmpeg

    ffmpeg_src = imageio_ffmpeg.get_ffmpeg_exe()
deno_src = os.environ.get("SEEDBOX_DENO") or "/opt/homebrew/bin/deno"
for src, what in ((ffmpeg_src, "ffmpeg"), (deno_src, "deno")):
    if not Path(src).is_file():
        fail(f"{what} not found at {src}")
    check_portable(os.path.realpath(src), what)
ffmpeg_bin, deno_bin = stage_binary(ffmpeg_src, "ffmpeg"), stage_binary(deno_src, "deno")

# ---- frontend
web_dist = Path(os.environ.get("SEEDBOX_WEB_DIST") or PROJECT / "frontend" / "dist").resolve()
if not (web_dist / "index.html").is_file():
    fail(f"no built frontend at {web_dist} (run `deno task build` in frontend/ or set SEEDBOX_WEB_DIST)")

# ---- python side
hiddenimports = [
    *collect_submodules("seedbox"),
    *collect_submodules("musicreco"),  # engine.py imports previews/structure/transitions by name
    *collect_submodules("uvicorn"),
    *collect_submodules("yt_dlp"),  # lazy extractors are imported by name
    *collect_submodules("mutagen"),
    *collect_submodules("pedalboard"),  # mixrender: Rubber Band stretch + FX (native extension)
    "webview.platforms.cocoa",
    "compileall", "py_compile",  # updater's verification child
    # stdlib a newer (downloaded) yt-dlp may start importing; cheap insurance
    "sqlite3", "xml.etree.ElementTree", "xml.dom.minidom", "hmac", "secrets", "uuid", "ctypes", "ctypes.util",
    "http.cookiejar", "netrc", "getpass", "shlex", "lzma", "bz2", "gzip", "tarfile", "zipfile", "html.parser",
    "email.utils", "concurrent.futures", "zoneinfo", "statistics", "difflib", "mimetypes", "pickle", "locale",
    "importlib.metadata", "importlib.resources", "asyncio", "selectors", "webbrowser", "unicodedata",
]
datas = [
    (str(web_dist), "web"),
    *collect_data_files("webview", includes=["js/**/*.js"]),
    *collect_data_files("yt_dlp_ejs", includes=["**/*.js"]),
    *collect_data_files("cloudscraper"),
    *collect_data_files("certifi"),
    *copy_metadata("yt-dlp"),  # updater.bundled_version()
    *copy_metadata("yt-dlp-ejs"),
]
excludes = [
    "tkinter", "_tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3", "clr", "pythonnet",
    "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cef", "webview.platforms.winforms",
    "webview.platforms.edgechromium", "webview.platforms.mshtml", "webview.platforms.win32",
    "webview.platforms.android", "IPython", "matplotlib", "pytest", "imageio_ffmpeg",  # ffmpeg is in bin/
    "questionary", "prompt_toolkit",  # terminal CLI only
]

a = Analysis(  # noqa: F821
    [str(PROJECT / "seedbox" / "__main__.py")],
    pathex=[str(PROJECT)],
    binaries=[(str(ffmpeg_bin), "bin"), (str(deno_bin), "bin")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Seedbox",
    console=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
    upx=False,
    strip=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Seedbox", upx=False, strip=False)  # noqa: F821

# LSMinimumSystemVersion = the newest macOS any bundled binary was built for (a Homebrew Python is built for
# the build machine's macOS, so the app inherits that requirement).
min_os = max((minos(src) for _dest, src, typ in a.binaries if typ in ("BINARY", "EXTENSION")), default=(11, 0))
min_os = max(min_os, (11, 0))
MIN_MACOS = ".".join(str(x) for x in (min_os + (0,))[:2])
VERSION = app_version()
print(f"seedbox.spec: Seedbox {VERSION}, LSMinimumSystemVersion {MIN_MACOS}, web from {web_dist}")

app = BUNDLE(  # noqa: F821
    coll,
    name="Seedbox.app",
    icon=str(PROJECT / "packaging" / "icon" / "Seedbox.icns"),
    bundle_identifier=os.environ.get("SEEDBOX_BUNDLE_ID", "app.seedbox.desktop"),
    version=VERSION,
    info_plist={
        "CFBundleName": "Seedbox",
        "CFBundleDisplayName": "Seedbox",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSArchitecturePriority": ["arm64"],
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,  # dark mode support
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},  # the window talks to 127.0.0.1
        "NSHumanReadableCopyright": "Seedbox: personal DJ practice / archival tool",
    },
)
