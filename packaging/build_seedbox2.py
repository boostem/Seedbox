#!/usr/bin/env python3
"""Build the Seedbox 2 installer for this machine's OS and architecture: a DMG on macOS (arm64 or x86_64), an Inno Setup
installer on Windows, and just the app folder on Linux (for checking the build). See packaging/README-seedbox2.md.

    uv run --no-project --python 3.13 packaging/build_seedbox2.py [--out DIR] [--skip-frontend] [--skip-installer]

Steps: release venv (uv-managed portable CPython, requirements.txt) → deno → LGPL ffmpeg → frontend2 → PyInstaller →
smoke test → sign (macOS) → installer → size limit → install it and smoke test what got installed.

Env:
  SEEDBOX2_SIGN_IDENTITY    macOS: a Developer ID Application identity; without it the app is ad-hoc signed
  SEEDBOX2_NOTARY_PROFILE   macOS: a `notarytool store-credentials` profile; notarises and staples the DMG
  SEEDBOX2_WEBVIEW2_SETUP   Windows: a WebView2 bootstrapper to use instead of downloading one
  ISCC                      Windows: Inno Setup's ISCC.exe, if it isn't on the PATH or in its usual folder
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(HERE))

from check_seedbox2 import check  # noqa: E402

NAME = "Seedbox 2"
PYTHON_VERSION = "3.13"  # the newest Python CI tests on
MACOS_WHEELS = "13.0"  # the oldest macOS whose wheels we take, the newest with onnxruntime for both architectures
INSTALLER_LIMIT = 250_000_000  # bytes

DENO_VERSION = "2.9.8"
DENO_SHA256 = {
    "aarch64-apple-darwin": "9f0bf660393da2eaff044cdc7444cb08c73f235849b61d224d825a194ce7c98e",
    "x86_64-apple-darwin": "4b2f5edd2639b7470f034238b321d7002d8dfb4726d8558f52c17afac370eb2c",
    "x86_64-pc-windows-msvc": "cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf",
    "x86_64-unknown-linux-gnu": "e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609",
}
# BtbN keeps a month's last build for a long time (daily ones go sooner). LGPL, ffmpeg 8.1.3 as build_ffmpeg.sh builds.
FFMPEG_WINDOWS_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/"
                      "ffmpeg-n8.1.3-27-g8dca1586f4-win64-lgpl-8.1.zip")  # fmt: skip
FFMPEG_WINDOWS_SHA256 = "d4c2a42673917b4a97104a3e9f082729a9730796b98664e37c0b7eba6c6c613d"
WEBVIEW2_BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"  # Microsoft's stable link
WINDOWS_START_MENU = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / NAME


def step(text: str) -> None:
    print(f"\n==> {text}", flush=True)


def die(text: str) -> None:
    sys.exit(f"error: {text}")


def run(*cmd: object, **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, **kwargs)  # type: ignore[call-overload]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, want_sha256: str | None = None) -> Path:
    """`url` to `dest`, checked against `want_sha256`. A file that is there and matches isn't fetched again."""
    if not (dest.is_file() and want_sha256 and sha256(dest) == want_sha256):
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {url}", flush=True)
        request = urllib.request.Request(url, headers={"User-Agent": "seedbox2-build"})
        with urllib.request.urlopen(request, timeout=120) as resp, dest.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
    if want_sha256 and sha256(dest) != want_sha256:
        dest.unlink()
        die(f"wrong checksum for {url}")
    return dest


def exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def machine() -> str:
    """The architecture as the installers name it: arm64 or x86_64."""
    m = platform.machine().lower()
    return {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}.get(m, m)


def app_version() -> str:
    text = (PROJECT / "seedbox2" / "__init__.py").read_text(encoding="utf-8")
    if m := re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M):
        return m.group(1)
    die("no __version__ in seedbox2/__init__.py")
    return ""


def release_venv(out: Path) -> Path:
    """A venv on a portable CPython holding requirements.txt and PyInstaller, not the dev tools (no torch, no onnx)."""
    step(f"release venv: uv-managed CPython {PYTHON_VERSION}")
    if not shutil.which("uv"):
        die("the build needs uv (https://docs.astral.sh/uv/)")
    venv = out / ".venv"
    run("uv", "venv", "--clear", "--managed-python", "--python", PYTHON_VERSION, venv)
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    env = {**os.environ, "MACOSX_DEPLOYMENT_TARGET": MACOS_WHEELS}
    run("uv", "pip", "install", "-q", "--python", python, "-r", PROJECT / "requirements.txt", "pyinstaller", env=env)
    versions = run("uv", "pip", "freeze", "--python", python, capture_output=True, text=True).stdout
    (out / "versions.txt").write_text(versions, encoding="utf-8")  # what went into this build
    return python


def fetch_deno(cache: Path) -> Path:
    arch = {"arm64": "aarch64", "x86_64": "x86_64"}[machine()]
    triple = f"{arch}-" + {"darwin": "apple-darwin", "win32": "pc-windows-msvc"}.get(sys.platform, "unknown-linux-gnu")
    if triple not in DENO_SHA256:
        die(f"no deno pinned for {triple}")
    folder = cache / f"deno-{DENO_VERSION}-{triple}"
    binary = folder / exe("deno")
    if not binary.is_file():
        step(f"deno {DENO_VERSION}")
        url = f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/deno-{triple}.zip"
        archive = download(url, folder / "deno.zip", DENO_SHA256[triple])
        with zipfile.ZipFile(archive) as z:
            z.extract(exe("deno"), folder)
        binary.chmod(0o755)
        archive.unlink()
    return binary


def fetch_ffmpeg(cache: Path) -> Path:
    """An LGPL ffmpeg: the pinned build on Windows, built from the pinned source everywhere else."""
    if sys.platform != "win32":
        step("LGPL ffmpeg from source (only the first time)")
        folder = cache / f"ffmpeg-{machine()}"
        run("bash", HERE / "build_ffmpeg.sh", folder, env={**os.environ, "MACOSX_DEPLOYMENT_TARGET": MACOS_WHEELS})
        return folder / "ffmpeg"
    folder = cache / "ffmpeg-win64"
    binary = folder / "ffmpeg.exe"
    if not binary.is_file():
        step("LGPL ffmpeg (BtbN's win64 build)")
        archive = download(FFMPEG_WINDOWS_URL, folder / "ffmpeg.zip", FFMPEG_WINDOWS_SHA256)
        with zipfile.ZipFile(archive) as z:
            member = next(n for n in z.namelist() if n.endswith("/bin/ffmpeg.exe"))
            binary.write_bytes(z.read(member))
        archive.unlink()
    return binary


def build_frontend(deno: Path) -> None:
    step("frontend2: deno task build")
    env = {**os.environ, "PATH": f"{deno.parent}{os.pathsep}{os.environ['PATH']}"}
    run(deno, "install", "--frozen", cwd=PROJECT / "frontend2", env=env)
    run(deno, "task", "build", cwd=PROJECT / "frontend2", env=env)


def pyinstaller(python: Path, out: Path, ffmpeg: Path, deno: Path) -> Path:
    """Build the app in `out` and return its folder (`Seedbox 2.app` on macOS)."""
    step("PyInstaller")
    work = out / ".work"
    env = {**os.environ, "SEEDBOX2_FFMPEG": str(ffmpeg), "SEEDBOX2_DENO": str(deno),
           "SEEDBOX2_WEB_DIST": str(PROJECT / "frontend2" / "dist")}  # fmt: skip
    run(python, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN", "--distpath", work / "dist",
        "--workpath", work / "build", HERE / "seedbox2.spec", env=env, cwd=PROJECT)
    name = f"{NAME}.app" if sys.platform == "darwin" else NAME
    app = out / name
    shutil.rmtree(app, ignore_errors=True)
    shutil.move(work / "dist" / name, app)
    shutil.rmtree(work / "dist", ignore_errors=True)
    return app


def executable_in(app: Path) -> Path:
    return app / "Contents" / "MacOS" / NAME if app.suffix == ".app" else app / exe(NAME)


def smoke_test(binary: Path) -> None:
    step(f"smoke test: {binary}")
    t0 = time.monotonic()
    if found := check(binary):
        die("smoke test failed:\n" + "\n".join(f"  {problem}" for problem in found))
    print(f"smoke test passed in {time.monotonic() - t0:.1f} s", flush=True)


# ---------------------------------------------------------------- macOS

def sign_macos(python: Path, app: Path) -> str:
    """Set the real minimum macOS, sign (ad-hoc, or Developer ID with a hardened runtime), and return that minimum."""
    step("minimum macOS and signature")
    min_os = run(python, HERE / "minos.py", app, capture_output=True, text=True).stdout.split()[-1]
    plist = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist.read_bytes())
    info["LSMinimumSystemVersion"] = min_os
    plist.write_bytes(plistlib.dumps(info))
    run("xattr", "-cr", app)
    identity = os.environ.get("SEEDBOX2_SIGN_IDENTITY", "").strip()
    args = (["--options", "runtime", "--timestamp", "--entitlements", str(HERE / "seedbox2.entitlements")]
            if identity else [])  # fmt: skip
    run("codesign", "--force", "--deep", *args, "--sign", identity or "-", app)
    run("codesign", "--verify", "--deep", "--strict", app)
    print(f"macOS {min_os}+, signed by {identity or 'ad-hoc'}")
    return min_os


def dmg(app: Path, out: Path, version: str, min_os: str) -> Path:
    step("DMG")
    path = out / f"Seedbox-2-{version}-macos-{machine()}.dmg"
    path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="seedbox2-dmg-") as tmp:
        stage = Path(tmp)
        run("ditto", app, stage / app.name)
        (stage / "Applications").symlink_to("/Applications")
        readme = (HERE / "README-seedbox2-macos.txt").read_text(encoding="utf-8").replace("{{MIN_MACOS}}", min_os)
        (stage / "Read Me First.txt").write_text(readme, encoding="utf-8")
        run("hdiutil", "create", "-quiet", "-volname", f"{NAME} {version}", "-srcfolder", stage, "-fs", "HFS+",
            "-format", "ULMO", "-ov", path)  # fmt: skip
    run("hdiutil", "verify", "-quiet", path)
    if identity := os.environ.get("SEEDBOX2_SIGN_IDENTITY", "").strip():
        run("codesign", "--force", "--timestamp", "--sign", identity, path)
        if profile := os.environ.get("SEEDBOX2_NOTARY_PROFILE", "").strip():
            step("notarise")
            run("xcrun", "notarytool", "submit", path, "--keychain-profile", profile, "--wait")
            run("xcrun", "stapler", "staple", path)
    return path


def smoke_test_dmg(path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="seedbox2-mount-") as tmp:
        run("hdiutil", "attach", "-quiet", "-nobrowse", "-readonly", "-mountpoint", tmp, path)
        try:
            smoke_test(executable_in(Path(tmp) / f"{NAME}.app"))
        finally:
            run("hdiutil", "detach", "-quiet", tmp)


# ---------------------------------------------------------------- Windows

def webview2_bootstrapper(cache: Path) -> Path:
    if given := os.environ.get("SEEDBOX2_WEBVIEW2_SETUP", "").strip():
        return Path(given)
    step("WebView2 bootstrapper")
    path = download(WEBVIEW2_BOOTSTRAPPER_URL, cache / "MicrosoftEdgeWebview2Setup.exe")
    check_script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
                    "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') "
                    "{ Write-Error $s.StatusMessage; exit 1 }")  # fmt: skip
    run("powershell", "-NoProfile", "-Command", check_script)  # Microsoft moves this file: its signature is the pin
    return path


def find_iscc() -> str:
    candidates = [os.environ.get("ISCC"), shutil.which("iscc")]
    candidates += [str(Path(os.environ[v]) / "Inno Setup 6" / "ISCC.exe")
                   for v in ("ProgramFiles(x86)", "ProgramFiles") if v in os.environ]  # fmt: skip
    if hit := next((c for c in candidates if c and Path(c).is_file()), None):
        return hit
    die("Inno Setup 6 not found (choco install innosetup, or set $ISCC)")
    return ""


def inno_setup(app: Path, out: Path, cache: Path, version: str) -> Path:
    bootstrapper = webview2_bootstrapper(cache)
    step("Inno Setup")
    name = f"Seedbox-2-{version}-windows-x64-setup"
    run(find_iscc(), "/Q", f"/DAppVersion={version}", f"/DSourceDir={app}", f"/DOutputDir={out}", f"/DOutputName={name}",
        f"/DWebView2Setup={bootstrapper}", f"/DIconFile={HERE / 'icon' / 'Seedbox.ico'}", HERE / "seedbox2.iss")
    return out / f"{name}.exe"


def smoke_test_windows_installer(path: Path) -> None:
    """Install silently for this user, smoke test the installed app, check the Start menu entry, and uninstall."""
    with tempfile.TemporaryDirectory(prefix="seedbox2-install-") as tmp:
        target = Path(tmp) / NAME
        run(path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={target}")
        smoke_test(executable_in(target))
        shortcut = WINDOWS_START_MENU / f"{NAME}.lnk"
        if not shortcut.is_file():
            die(f"the installer made no Start menu entry at {shortcut}")
        run(target / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
        time.sleep(5)  # the uninstaller hands over to a copy of itself and returns at once
        if shortcut.exists():
            die("the uninstaller left the Start menu entry behind")


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=HERE / "dist" / "seedbox2", help="output folder")
    parser.add_argument("--skip-frontend", action="store_true", help="use the frontend2/dist that is there")
    parser.add_argument("--skip-installer", action="store_true", help="stop after the app and its smoke test")
    args = parser.parse_args()

    t0 = time.monotonic()
    out = args.out.resolve()
    cache = out / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    version = app_version()

    python = release_venv(out)
    deno = fetch_deno(cache)
    ffmpeg = fetch_ffmpeg(cache)
    if not args.skip_frontend:
        build_frontend(deno)
    app = pyinstaller(python, out, ffmpeg, deno)
    min_os = sign_macos(python, app) if sys.platform == "darwin" else ""
    smoke_test(executable_in(app))

    installer = None
    if not args.skip_installer and sys.platform in ("darwin", "win32"):
        installer = dmg(app, out, version, min_os) if sys.platform == "darwin" else inno_setup(app, out, cache, version)
        size = installer.stat().st_size
        print(f"{installer.name}: {size / 1e6:.1f} MB (limit {INSTALLER_LIMIT / 1e6:.0f} MB)")
        if size >= INSTALLER_LIMIT:
            die("the installer is over the size limit")
        if sys.platform == "darwin":
            smoke_test_dmg(installer)
        else:
            smoke_test_windows_installer(installer)

    step(f"done in {time.monotonic() - t0:.0f} s")
    print(f"app: {app}")
    if installer:
        print(f"installer: {installer} ({installer.stat().st_size / 1e6:.1f} MB, sha256 {sha256(installer)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
