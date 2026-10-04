# PyInstaller spec for Seedbox.app or Setbox.app (onedir, windowed, arm64). Normally run through packaging/build.sh:
#   SEEDBOX_APP=setbox .venv/bin/python -m PyInstaller --noconfirm --clean --distpath <out>/dist \
#       --workpath <out>/build packaging/seedbox.spec
#
# Env:
#   SEEDBOX_APP        which app: seedbox (default; find, preview, download, tag) or setbox (sets, transitions, vocal
#                      stems, mixes). Seedbox.app leaves out PyTorch, the analysis and the mix engine; Setbox.app
#                      leaves out deno (YouTube only).
#   SEEDBOX_WEB_DIST   built frontend (default <project>/frontend/dist; must contain the app's page: index.html for
#                      Seedbox, setbox.html for Setbox)
#   SEEDBOX_DENO       deno binary to bundle in Seedbox.app (default /opt/homebrew/bin/deno, only if it links system
#                      libs only)
#   SEEDBOX_FFMPEG     ffmpeg binary to bundle (default: imageio-ffmpeg's static build)
#   SEEDBOX_BUNDLE_ID  CFBundleIdentifier of Seedbox.app (default app.seedbox.desktop)
#   SETBOX_BUNDLE_ID   CFBundleIdentifier of Setbox.app (default app.seedbox.setbox)
# The bundle gets Contents/Frameworks/bin/ffmpeg (+ deno for Seedbox) (= sys._MEIPASS/bin, found by musicreco.paths)
# and Contents/Resources/web (the SPA, = sys._MEIPASS/web, found by seedbox.app.static_dir).
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
APPS = {
    "seedbox": {"name": "Seedbox", "page": "index.html", "bundle_id": "app.seedbox.desktop", "deno": True,
                "copyright": "Seedbox: personal DJ practice / archival tool"},
    "setbox": {"name": "Setbox", "page": "setbox.html", "bundle_id": "app.seedbox.setbox", "deno": False,
               "copyright": "Setbox: sets and transitions for Seedbox crates"},
}
# Setbox's side: Seedbox.app leaves these out (python -m seedbox never imports them; see test_server_seedbox)
SETBOX_ONLY = ("setbox", "torch", "torchaudio", "torchvision", "einops", "pedalboard", "scipy",
               "musicreco.structure", "musicreco.transitions", "musicreco.mixplan", "musicreco.mixrender",
               "musicreco.setmix", "musicreco.stems", "musicreco.separator", "musicreco.roformer")


def fail(msg):
    sys.exit(f"seedbox.spec: {msg}")


KEY = os.environ.get("SEEDBOX_APP", "seedbox")
if KEY not in APPS:
    fail(f"SEEDBOX_APP={KEY!r}: use one of {', '.join(APPS)}")
APP = APPS[KEY]
NAME = APP["name"]


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
tools = [(ffmpeg_src, "ffmpeg")]
if APP["deno"]:  # yt-dlp solves YouTube's JS challenges with it: Seedbox only
    tools.append((os.environ.get("SEEDBOX_DENO") or "/opt/homebrew/bin/deno", "deno"))
for src, what in tools:
    if not Path(src).is_file():
        fail(f"{what} not found at {src}")
    check_portable(os.path.realpath(src), what)
binaries = [(str(stage_binary(src, what)), "bin") for src, what in tools]

# ---- frontend
web_dist = Path(os.environ.get("SEEDBOX_WEB_DIST") or PROJECT / "frontend" / "dist").resolve()
if not (web_dist / APP["page"]).is_file():
    fail(f"no built frontend at {web_dist} (no {APP['page']}; run `deno task build` in frontend/ or set SEEDBOX_WEB_DIST)")

# ---- python side
if KEY == "setbox":
    app_modules = [
        *collect_submodules("setbox"),
        *collect_submodules("musicreco"),  # setbox imports structure/stems/mixplan/setmix by name (engine.optional)
        *collect_submodules("pedalboard"),  # mixrender: Rubber Band stretch + FX (native extension)
        *collect_submodules("einops"),  # musicreco.roformer (vocal stems; torch comes with its own PyInstaller hook)
    ]
else:
    app_modules = [m for m in collect_submodules("musicreco") if not m.startswith(SETBOX_ONLY)]
hiddenimports = [
    *collect_submodules("seedbox"),
    *app_modules,
    *collect_submodules("uvicorn"),
    # both: the launcher activates a downloaded yt-dlp (shared support folder) before seedbox.engine imports it
    *collect_submodules("yt_dlp"),  # lazy extractors are imported by name
    *collect_submodules("mutagen"),
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
    *(SETBOX_ONLY if KEY == "seedbox" else ()),
]

a = Analysis(  # noqa: F821
    [str(PROJECT / KEY / "__main__.py")],
    pathex=[str(PROJECT)],
    binaries=binaries,
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
    name=NAME,
    console=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
    upx=False,
    strip=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False, strip=False)  # noqa: F821

# LSMinimumSystemVersion = the newest macOS any bundled binary was built for (a Homebrew Python is built for
# the build machine's macOS, so the app inherits that requirement).
min_os = max((minos(src) for _dest, src, typ in a.binaries if typ in ("BINARY", "EXTENSION")), default=(11, 0))
min_os = max(min_os, (11, 0))
MIN_MACOS = ".".join(str(x) for x in (min_os + (0,))[:2])
VERSION = app_version()
print(f"seedbox.spec: {NAME} {VERSION}, LSMinimumSystemVersion {MIN_MACOS}, web from {web_dist}")

app = BUNDLE(  # noqa: F821
    coll,
    name=f"{NAME}.app",
    icon=str(PROJECT / "packaging" / "icon" / f"{NAME}.icns"),
    bundle_identifier=os.environ.get(f"{KEY.upper()}_BUNDLE_ID", APP["bundle_id"]),
    version=VERSION,
    info_plist={
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSArchitecturePriority": ["arm64"],
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,  # dark mode support
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},  # the window talks to 127.0.0.1
        "NSHumanReadableCopyright": APP["copyright"],
    },
)
