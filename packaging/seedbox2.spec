# PyInstaller spec for Seedbox 2: a one-folder app on Windows, Seedbox 2.app on macOS (for the architecture of the
# Python running it), a plain folder on Linux (only to check the build locally). Run it through build_seedbox2.py.
#
# Env, all required but the last:
#   SEEDBOX2_FFMPEG   LGPL ffmpeg to bundle    SEEDBOX2_DENO   deno to bundle
#   SEEDBOX2_WEB_DIST the built frontend2      SEEDBOX2_BUNDLE_ID  CFBundleIdentifier (default app.seedbox.seedbox2)
#
# ffmpeg and deno land in sys._MEIPASS/bin (found by musicreco.paths), the UI in sys._MEIPASS/web (seedbox2.shell).
# Model weights aren't bundled: the app downloads them on first run (seedbox2/models/). torch is only for exporting
# them (seedbox2/models/export/, dev-only), and imageio-ffmpeg carries a GPL ffmpeg; neither may get in.
# -*- mode: python -*-

import os
import platform
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

PROJECT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is injected by PyInstaller
NAME = "Seedbox 2"
ICON_DIR = PROJECT / "packaging" / "icon"
DEV_ONLY = ("seedbox2.models.export", "seedbox2.bench", "seedbox2.eval")  # server imports the eval it needs by itself


def required_file(env):
    path = Path(os.environ.get(env, "")).resolve()
    if not os.environ.get(env) or not path.exists():
        sys.exit(f"seedbox2.spec: set {env} (run packaging/build_seedbox2.py)")
    return path


def version():
    text = (PROJECT / "seedbox2" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M).group(1)


web_dist = required_file("SEEDBOX2_WEB_DIST")
if not (web_dist / "index.html").is_file():
    sys.exit(f"seedbox2.spec: no index.html in {web_dist}")

window_imports = {
    "darwin": ["webview.platforms.cocoa"],
    "win32": ["webview.platforms.edgechromium", "webview.platforms.winforms", "clr", "clr_loader", "pythonnet"],
}.get(sys.platform, [])
hiddenimports = [
    *collect_submodules("seedbox2", filter=lambda name: not name.startswith(DEV_ONLY)),
    *collect_submodules("musicreco"),  # engine.py imports previews/structure/transitions by name
    *collect_submodules("uvicorn"),
    *collect_submodules("yt_dlp"),  # lazy extractors are imported by name
    *collect_submodules("mutagen"),
    *collect_submodules("pedalboard"),  # mixrender: Rubber Band stretch and FX (a native extension)
    *collect_submodules("watchdog"),  # the observer of each OS is imported by name
    *window_imports,
]
datas = [
    (str(web_dist), "web"),
    (str(PROJECT / "packaging" / "seedbox2-third-party.txt"), "licenses"),
    *collect_data_files("webview", includes=["js/**/*.js", "lib/**/*"]),
    *collect_data_files("yt_dlp_ejs", includes=["**/*.js"]),
    *collect_data_files("cloudscraper"),
    *collect_data_files("certifi"),  # model downloads check TLS against these (a portable Python has no system CAs)
    *copy_metadata("yt-dlp"),
    *copy_metadata("yt-dlp-ejs"),
]
excludes = [
    "torch", "torchaudio", "torchvision", "onnx", "onnxscript", "tensorflow", "imageio_ffmpeg", "mir_eval",
    "seedbox", "tkinter", "_tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3", "IPython",
    "matplotlib", "pytest", "questionary", "prompt_toolkit", "webview.platforms.qt", "webview.platforms.gtk",
    "webview.platforms.cef", "webview.platforms.mshtml", "webview.platforms.android",
]

a = Analysis(  # noqa: F821
    [str(PROJECT / "seedbox2" / "__main__.py")],
    pathex=[str(PROJECT)],
    binaries=[
        (str(required_file("SEEDBOX2_FFMPEG")), "bin"),
        (str(required_file("SEEDBOX2_DENO")), "bin"),
        *collect_dynamic_libs("onnxruntime"),
    ],
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
    icon=str(ICON_DIR / ("Seedbox.ico" if sys.platform == "win32" else "Seedbox.icns")),
    target_arch=platform.machine() if sys.platform == "darwin" else None,
    codesign_identity=None,  # build_seedbox2.py signs the finished bundle
    upx=False,
    strip=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False, strip=False)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{NAME}.app",
        icon=str(ICON_DIR / "Seedbox.icns"),
        bundle_identifier=os.environ.get("SEEDBOX2_BUNDLE_ID", "app.seedbox.seedbox2"),
        version=version(),
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": version(),
            "CFBundleVersion": version(),
            "LSMinimumSystemVersion": "13.0",  # build_seedbox2.py sets the real one from every bundled Mach-O
            "LSApplicationCategoryType": "public.app-category.music",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "NSSupportsAutomaticGraphicsSwitching": True,
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},  # the window talks to 127.0.0.1
        },
    )
