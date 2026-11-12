#!/usr/bin/env python3
"""Smoke test of a built Seedbox 2, headless: `check_seedbox2.py <the app's executable>`.

Runs `--self-test` with empty data folders and holds the report to what a shipped app needs: ffmpeg and deno from
inside the bundle and running, ffmpeg still LGPL and able to write Opus, onnxruntime running the tiny model in-process
and in a spawned worker, pywebview's backend loading, the server serving the UI, no torch. Exit code 0 when it passes.
Stdlib only: it runs outside the app, and on any Python.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

SELF_TEST_TIMEOUT_S = 300
# onnxruntime's accelerator for each OS has to be in the bundle (seedbox2/models/runtime.py); it stays optional at run
# time, as the machine may not be able to use it
ACCELERATORS = {"darwin": "CoreMLExecutionProvider", "win32": "DmlExecutionProvider"}
FORBIDDEN_FOLDERS = {"torch", "torchaudio", "imageio_ffmpeg", "onnx"}


def problems(report: dict[str, Any], bundle: Path, platform: str = sys.platform) -> list[str]:
    """What is wrong with a `--self-test` report of the app in the folder `bundle`."""
    found = []
    if not report.get("ok"):
        found.append("the self-test says it failed")
    if not report.get("frozen"):
        found.append("not running frozen")
    for name in ("ffmpeg", "deno"):
        path = report.get(name)
        if not path or not Path(path).resolve().is_relative_to(bundle.resolve()):
            found.append(f"{name} isn't the bundled one: {path}")
        if not report.get(f"{name}_version"):
            found.append(f"{name} doesn't run")
    for module, status in report.get("modules", {}).items():
        if status != "ok":
            found.append(f"import {module}: {status}")
    if report.get("window_backend") != "ok":
        found.append(f"window backend: {report.get('window_backend')}")
    onnx = report.get("onnxruntime", {})
    if not onnx.get("ok"):
        found.append(f"onnxruntime: {onnx.get('error') or onnx}")
    wanted = ACCELERATORS.get(platform)
    if wanted and wanted not in onnx.get("providers", []):
        found.append(f"onnxruntime lacks {wanted}: {onnx.get('providers')}")
    if not report.get("frontend"):
        found.append("no built UI")
    if not report.get("server", {}).get("ok"):
        found.append(f"server: {report.get('server')}")
    return found


def forbidden_in(bundle: Path) -> list[str]:
    """Folders that mean torch, a GPL ffmpeg or the dev-only onnx package got into the bundle."""
    return [str(p) for p in bundle.rglob("*") if p.is_dir() and p.name.lower() in FORBIDDEN_FOLDERS]


def ffmpeg_problems(ffmpeg: str, workdir: Path) -> list[str]:
    """The bundled ffmpeg has to be LGPL and do what the app asks of it: write Opus and MP3 (every download) and measure loudness."""
    version = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True, timeout=60,
                             check=False).stdout
    found = [f"ffmpeg is built with {flag}" for flag in ("--enable-gpl", "--enable-nonfree") if flag in version]
    tone, opus = workdir / "tone.wav", workdir / "tone.opus"
    with wave.open(str(tone), "wb") as w:  # a second of noise
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(random.Random(0).randbytes(4 * 44100))
    steps = (["-i", str(tone), "-c:a", "libopus", "-b:a", "96k", str(opus)],
             ["-i", str(opus), "-map", "0:a:0", "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
             ["-i", str(opus), "-ac", "2", "-c:a", "aac", "-f", "ipod", str(workdir / "tone.m4a")],
             ["-i", str(opus), "-c:a", "libmp3lame", "-b:a", "192k", str(workdir / "tone.mp3")])
    for step in steps:
        r = subprocess.run([ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *step],
                           capture_output=True, text=True, timeout=120, check=False)
        if r.returncode != 0:
            found.append(f"ffmpeg {' '.join(step)}: {r.stderr.strip()[-300:]}")
    return found


def bundle_root(exe: Path) -> Path:
    """The folder holding the whole app: `Seedbox 2.app` on macOS, the executable's folder elsewhere."""
    return next((p for p in exe.parents if p.suffix == ".app"), exe.parent)


def check(exe: Path) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="seedbox2-check-") as tmp:
        work = Path(tmp)
        report_file = work / "report.json"
        env = {**os.environ, "SEEDBOX_SUPPORT_DIR": str(work / "support"), "SEEDBOX_CACHE_DIR": str(work / "cache"),
               "SEEDBOX_LOG_DIR": str(work / "logs"), "MUSICRECO_OUTPUT_ROOT": str(work / "music")}
        r = subprocess.run([str(exe), "--self-test", "--report", str(report_file)], env=env, capture_output=True,
                           text=True, timeout=SELF_TEST_TIMEOUT_S, check=False)
        if not report_file.is_file():
            logs = "".join(p.read_text(errors="replace") for p in (work / "logs").glob("*.log"))
            return [f"no report, exit code {r.returncode}\n{r.stdout}\n{r.stderr}\n{logs}"]
        report = json.loads(report_file.read_text(encoding="utf-8"))
        print(json.dumps(report, indent=2))
        bundle = bundle_root(exe)
        found = [*problems(report, bundle), *forbidden_in(bundle)]
        if report.get("ffmpeg"):
            found += ffmpeg_problems(report["ffmpeg"], work)
        if r.returncode != 0:
            found.append(f"exit code {r.returncode}")
        return found


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    found = check(Path(sys.argv[1]).resolve())
    for problem in found:
        print(f"FAILED: {problem}", file=sys.stderr)
    print("smoke test failed" if found else "smoke test passed")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
