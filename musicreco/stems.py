"""Vocal stems for chill mixes: each track's vocals split from the rest by a RoFormer model, so a transition can mute
the outgoing vocal (two vocals never at once), let A's vocal ride over B's beat (acapella), repeat an ending without
its vocal, and plan from where the vocals really are instead of a guess.

Files, per crate (next to the analyses):
  <crate>/.seedbox/stems/<track_id>.flac  the vocal stem: 44.1 kHz stereo, 16-bit FLAC, sample-aligned with
                                          mixrender.decode_stereo of the track (the instrumental is the track minus it)
  <crate>/.seedbox/stems/<track_id>.json  {"key": cache key, "model": id, "fps": ENV_FPS, "vocal_db": [...],
                                          "mix_db": [...]}: the stem's and the track's loudness every 1/ENV_FPS s
vocal_per_bar() turns the two envelopes into a per-bar vocal level for any analysis grid; enrich() puts it into an
analysis dict (vocal_source = "stems"), which is how the planner knows a track's vocals are split.

The model (see separator.py) downloads on first use into <app support>/models. Splitting needs torch; without it
available() says why and everything else keeps working on the heuristic vocal detection.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .paths import ffmpeg_path

log = logging.getLogger(__name__)

STEMS_VERSION = 1  # bump when the stem files or envelopes change: older ones are split again
SR = 44100
ENV_FPS = 20  # loudness envelope frames per second
_ENV_WIN_S = 0.1
_SUNG_REL_DB = -15.0  # a frame counts as sung when the stem is within this of the whole track...
_SUNG_FLOOR_DB = -32.0  # ...and within this of the stem's own loud level (not bleed in a quiet passage)
_SUNG_FULL = 0.35  # share of a bar's frames sung for it to count fully (words leave gaps)
ENCODE_TIMEOUT_S = 300

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class Cancelled(Exception):
    """Splitting was cancelled."""


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def stem_dir(seed_dir: Path) -> Path:
    return Path(seed_dir) / ".seedbox" / "stems"


def paths(seed_dir: Path, track_id: str) -> tuple[Path, Path]:
    """(vocal stem FLAC, sidecar JSON) of a crate track."""
    d = stem_dir(seed_dir)
    return d / f"{track_id}.flac", d / f"{track_id}.json"


def available() -> tuple[bool, str]:
    """(can split here, why not)."""
    try:
        from . import separator
    except ImportError as e:
        return False, f"the vocal stem model can't load ({e})"
    return separator.available()


def model_unavailable(e: BaseException) -> bool:
    """True when `e` is a failed model download (retry later), not a problem with the song."""
    try:
        from .separator import ModelUnavailable
    except ImportError:
        return False
    return isinstance(e, ModelUnavailable)


def model_id() -> str:
    try:
        from . import separator
    except ImportError:
        return "none"
    return separator.MODEL["id"]


def unload_model(idle_for_s: float = 0.0) -> None:
    """Drop the loaded vocal model when it has been idle at least `idle_for_s` (frees its memory)."""
    try:
        from . import separator
    except ImportError:
        return
    if separator.idle_s() >= idle_for_s:
        separator.unload()


# --- cache ---------------------------------------------------------------------------------------------------------


def _key(path: Path) -> dict:
    st = Path(path).stat()
    return {"size": st.st_size, "mtime": st.st_mtime_ns, "version": STEMS_VERSION, "model": model_id()}


def load(seed_dir: Path, track_id: str, path: Path) -> dict | None:
    """The track's stem sidecar when its FLAC exists and both still match the track file and the model; None
    otherwise. A size/mtime miss falls back to the audio fingerprint (retagged files keep their stems)."""
    flac, meta = paths(seed_dir, track_id)
    try:
        data = json.loads(meta.read_text())
    except (OSError, ValueError):
        return None
    if not (isinstance(data, dict) and flac.is_file() and isinstance(data.get("vocal_db"), list)):
        return None
    try:
        key = _key(path)
    except OSError:
        return None
    old = data.get("key") or {}
    if all(old.get(k) == key[k] for k in key):
        return data
    if old.get("version") == key["version"] and old.get("model") == key["model"] and old.get("fingerprint"):
        from .structure import _audio_fingerprint

        try:
            if _audio_fingerprint(Path(path)) == old["fingerprint"]:
                data["key"] = {**key, "fingerprint": old["fingerprint"]}
                _write_json(meta, data)
                return data
        except OSError:
            return None
    return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- envelopes + per-bar activity ------------------------------------------------------------------------------------


def envelope_db(x: np.ndarray, sr: int = SR, fps: int = ENV_FPS) -> np.ndarray:
    """RMS level (dB, channels summed) in _ENV_WIN_S windows every 1/fps s; frame k is centred on k / fps."""
    p = np.asarray(x, dtype=np.float64)
    p = (p * p).sum(axis=0) if p.ndim == 2 else p * p
    hop, win = sr // fps, int(round(_ENV_WIN_S * sr))
    c = np.concatenate([[0.0], np.cumsum(p)])
    centres = np.arange(0, p.size, hop)
    a = np.clip(centres - win // 2, 0, p.size)
    b = np.clip(centres + win // 2, 0, p.size)
    ms = (c[b] - c[a]) / np.maximum(1, b - a)
    return 10 * np.log10(ms + 1e-10)


def vocal_per_bar(meta: dict, an: dict) -> list[float] | None:
    """Per-bar vocal level (0-1) on the analysis' bar grid from a stem sidecar: the share of each bar's frames where
    the stem is a real part of the track (within _SUNG_REL_DB of it and within _SUNG_FLOOR_DB of the stem's own loud
    level), full at _SUNG_FULL. None when the inputs don't line up."""
    try:
        fps = float(meta.get("fps") or ENV_FPS)
        v = np.asarray(meta["vocal_db"], dtype=float)
        x = np.asarray(meta["mix_db"], dtype=float)
        fd, bar_s, n = float(an["first_downbeat_s"]), float(an["bar_s"]), int(an["bar_count"])
    except (KeyError, TypeError, ValueError):
        return None
    if v.size == 0 or v.size != x.size or bar_s <= 0 or not 0 < n <= 20000 or not math.isfinite(fd):
        return None
    loud = v[v > -90]
    ref = float(np.percentile(loud, 95)) if loud.size else -20.0
    sung = (v > x + _SUNG_REL_DB) & (v > ref + _SUNG_FLOOR_DB)
    c = np.concatenate([[0], np.cumsum(sung)])
    out = []
    for k in range(n):
        a = int(np.clip(round((fd + k * bar_s) * fps), 0, sung.size))
        b = int(np.clip(round((fd + (k + 1) * bar_s) * fps), 0, sung.size))
        frac = (c[b] - c[a]) / (b - a) if b > a else 0.0
        out.append(round(min(1.0, frac / _SUNG_FULL), 3))
    return out


def enrich(an: dict | None, meta: dict | None) -> dict | None:
    """A copy of the analysis with vocal_per_bar from the stems (vocal_source "stems"; the heuristic one is kept as
    vocal_per_bar_heuristic), or the analysis itself when there are no usable stems."""
    if not isinstance(an, dict) or not isinstance(meta, dict):
        return an
    per_bar = vocal_per_bar(meta, an)
    if per_bar is None:
        return an
    return {**an, "vocal_per_bar": per_bar, "vocal_per_bar_heuristic": an.get("vocal_per_bar"),
            "vocal_source": "stems"}


# --- splitting -------------------------------------------------------------------------------------------------------


def _encode_flac(audio: np.ndarray, out_path: Path, sr: int = SR) -> None:
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise ValueError("ffmpeg not found")
    pcm = np.ascontiguousarray(np.clip(audio, -1.0, 1.0).T.astype("<f4")).tobytes()
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "2",
           "-i", "-", "-c:a", "flac", "-sample_fmt", "s16", "-compression_level", "5", "-f", "flac", str(out_path)]
    try:
        proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=ENCODE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"could not write the vocal stem: {e}") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise ValueError(f"could not write the vocal stem: {tail[0]}")


def split(seed_dir: Path, track_id: str, path: Path, progress: Callable[[float], None] | None = None,
          cancel: threading.Event | None = None) -> dict:
    """The track's stem sidecar, splitting it first when there is none (thread-safe per track, atomic writes).
    progress(0-1) covers the model download too. ValueError on undecodable audio or a model that can't load,
    Cancelled when `cancel` is set (nothing is written)."""
    from . import separator
    from .mixrender import decode_stereo

    flac, meta_path = paths(seed_dir, track_id)
    with _lock_for(str(meta_path)):
        if (hit := load(seed_dir, track_id, path)) is not None:
            return hit
        ok, why = separator.available()
        if not ok:
            raise ValueError(why)

        def report(f: float) -> None:
            if progress is not None:
                try:
                    progress(max(0.0, min(1.0, f)))
                except Exception:  # noqa: BLE001 - a progress sink never breaks a split
                    pass

        key = _key(path)
        audio = decode_stereo(Path(path), SR)
        report(0.02)
        vocals = separator.separate(audio, SR, progress=lambda f: report(0.02 + 0.93 * f), cancel=cancel)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        flac.parent.mkdir(parents=True, exist_ok=True)
        tmp = flac.with_name(f".{flac.stem}.{os.getpid()}.{threading.get_ident()}.part.flac")
        try:
            _encode_flac(vocals, tmp)
            os.replace(tmp, flac)
        finally:
            tmp.unlink(missing_ok=True)
        from .structure import _audio_fingerprint

        data = {
            "key": {**key, "fingerprint": _audio_fingerprint(Path(path))}, "model": separator.MODEL["id"],
            "fps": ENV_FPS, "duration_s": round(audio.shape[1] / SR, 3),
            "vocal_db": [round(float(v), 1) for v in envelope_db(vocals)],
            "mix_db": [round(float(v), 1) for v in envelope_db(audio)],
        }  # fmt: skip
        _write_json(meta_path, data)
        report(1.0)
        return data
