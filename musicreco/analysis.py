"""Audio analysis: EBU R128 loudness via one ffmpeg pass (ReplayGain 2.0 values). Thread-safe, no output."""

from __future__ import annotations

import logging
import math
import re
import subprocess
from pathlib import Path

from . import paths
from .models import Loudness

log = logging.getLogger(__name__)

RG2_REFERENCE_LUFS = -18.0
TIMEOUT_S = 600

_NUM = r"(-?\d+(?:\.\d+)?|-?inf)"
_INTEGRATED_RE = re.compile(r"Integrated loudness:\s*I:\s*" + _NUM + r"\s*LUFS")
_TRUE_PEAK_RE = re.compile(r"True peak:\s*Peak:\s*" + _NUM + r"\s*(?:dBTP|dBFS)")


def _ffmpeg() -> str | None:
    return paths.ffmpeg_path()


def _finite(raw: str) -> float | None:
    value = float(raw)
    return value if math.isfinite(value) else None


def measure_loudness(path: Path) -> Loudness | None:
    """Integrated LUFS + true peak in one ffmpeg `ebur128=peak=true` pass; None (logged) on any failure."""
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        log.warning("loudness %s: ffmpeg not found", path)
        return None

    cmd = [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-map", "0:a:0"]
    cmd += ["-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"]
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("loudness %s: ffmpeg failed to run (%s)", path, e)
        return None
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1:] or ["no output"]
        log.warning("loudness %s: ffmpeg exit %d (%s)", path, proc.returncode, tail[0])
        return None

    summary = proc.stderr.rsplit("Summary:", 1)[-1]  # ignore any per-frame lines before it
    integrated, true_peak = _INTEGRATED_RE.search(summary), _TRUE_PEAK_RE.search(summary)
    if not integrated or not true_peak:
        log.warning("loudness %s: no ebur128 summary in ffmpeg output", path)
        return None
    lufs, dbtp = _finite(integrated.group(1)), _finite(true_peak.group(1))
    if lufs is None or dbtp is None:  # digital silence
        log.warning("loudness %s: silent audio, no loudness", path)
        return None
    return Loudness(
        lufs=lufs,
        true_peak_dbtp=dbtp,
        replaygain_db=round(RG2_REFERENCE_LUFS - lufs, 2),
        peak=10 ** (dbtp / 20),
    )
