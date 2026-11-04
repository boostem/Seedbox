"""Stems (#44): HTDemucs on the CPU through onnxruntime, cached per content hash.

The graph (see the `htdemucs` registry entry) has no complex tensors and no FFT: it takes a 7.8 s stereo segment and
that segment's spectrogram as real and imaginary channels, and gives back each source's spectrogram and waveform. The
STFT and iSTFT are done here with `scipy.fft`, within 3e-5 of torch's. The same transforms written as convolutions
inside the graph (measured on such an export) take 25 % longer per segment and make the file 304 MB instead of 94 MB.

`separate` streams: ffmpeg decodes to 44.1 kHz stereo, segments overlap by a quarter and are blended with the
triangular window `demucs.apply.apply_model` uses, and each finished part goes straight to one ffmpeg encoder per
stem. A track is never in RAM, whatever its length.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
from scipy import fft as sfft

from seedbox2.analysis import waveform
from seedbox2.analysis.waveform import (
    STEMS,  # the graph's source order, and the order in the waveform file
)
from seedbox2.jobs import register
from seedbox2.models.registry import MODELS

log = logging.getLogger(__name__)

MODEL = "htdemucs"
MODEL_LOW_POWER = "htdemucs_int8"
SAMPLE_RATE = 44_100
CHANNELS = 2
SEGMENT = 343_980  # samples: the 7.8 s HTDemucs was trained on
OVERLAP = 0.25
STRIDE = int((1 - OVERLAP) * SEGMENT)
NFFT = 4096
HOP = NFFT // 4
FRAMES = int(np.ceil(SEGMENT / HOP))  # 336
FFT_WORKERS = 2
ENCODE_TIMEOUT_S = 120
ARTIFACT_KIND = "stems"
FORMATS = {"flac": "flac", "opus": "opus"}  # the setting -> the file extension
DEFAULT_FORMAT = "flac"  # lossless and sample-accurate, at about ESTIMATE_BYTES_PER_S; Opus is a quarter of that
OPUS_BITRATE = "160k"
ESTIMATE_BYTES_PER_S = {"flac": 4 * 75_000, "opus": 4 * 20_000}  # all four stems
SETTINGS_KEY = "stems"
DEFAULT_LIMIT_GB = 20.0
GB = 1024**3
TMP_MARK = ".partial-"

_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(NFFT) / NFFT)).astype(np.float32)  # periodic Hann, as torch's
_NORM = float(np.sqrt(NFFT))  # torch's normalized=True


class StemsError(ValueError):
    """The audio can't be decoded, or the stems can't be written."""


# --- the transforms --------------------------------------------------------------------------------------------------


def spectrogram(x: np.ndarray) -> np.ndarray:
    """demucs' `HTDemucs._spec` and `_magnitude`: [channels, samples] -> float32 [channels * 2, 2048, frames], with
    re and im of each channel side by side and frames = ceil(samples / HOP)."""
    channels, samples = x.shape
    frames = int(np.ceil(samples / HOP))
    pad = HOP // 2 * 3
    # both reflections of `_spec` and of torch.stft's centring: x, padded by `pad`, then by NFFT // 2 on each side
    padded = np.pad(x, ((0, 0), (pad, pad + frames * HOP - samples)), mode="reflect")
    padded = np.pad(padded, ((0, 0), (NFFT // 2, NFFT // 2)), mode="reflect")
    # the frames `_spec` keeps start two hops in and end before the last two
    windows = np.lib.stride_tricks.sliding_window_view(padded, NFFT, axis=1)[:, 2 * HOP:(2 + frames) * HOP:HOP] * _WINDOW
    z = sfft.rfft(windows, axis=-1, workers=FFT_WORKERS)[..., :-1].transpose(0, 2, 1) / _NORM  # [c, 2048, frames]
    out = np.empty((channels * 2, NFFT // 2, frames), np.float32)
    out[::2], out[1::2] = z.real, z.imag
    return out


def inverse_spectrogram(spec: np.ndarray, samples: int) -> np.ndarray:
    """demucs' `HTDemucs._mask` and `_ispec`: float32 [..., channels * 2, 2048, frames] -> [..., channels, samples]."""
    *lead, chre, _, frames = spec.shape
    z = spec[..., ::2, :, :] + 1j * spec[..., 1::2, :, :]  # [..., channels, 2048, frames]
    flat = z.reshape(-1, NFFT // 2, frames).transpose(0, 2, 1)  # [n, frames, 2048]
    # `_ispec`: an empty Nyquist bin, and two empty frames on each side
    full = np.zeros((flat.shape[0], frames + 4, NFFT // 2 + 1), np.complex64)
    full[:, 2:2 + frames, :-1] = flat
    y = sfft.irfft(full, n=NFFT, axis=-1, workers=FFT_WORKERS) * _NORM * _WINDOW
    total = HOP * (frames + 3) + NFFT  # torch.istft's output before it cuts its centring off
    out = np.zeros((y.shape[0], total), np.float32)
    norm = np.zeros(total, np.float32)
    square = _WINDOW**2
    for i in range(frames + 4):
        s = i * HOP
        out[:, s:s + NFFT] += y[:, i]
        norm[s:s + NFFT] += square
    pad = HOP // 2 * 3
    start = NFFT // 2 + pad  # torch.istft's centring, then `_ispec`'s pad
    out = out[:, start:start + samples] / np.maximum(norm[start:start + samples], 1e-11)
    return out.reshape(*lead, chre // 2, samples)


def separate_segment(session: Any, x: np.ndarray) -> np.ndarray:
    """One full SEGMENT, float32 [2, SEGMENT], through the graph: [sources, 2, SEGMENT], the sum of its two branches."""
    if x.shape != (CHANNELS, SEGMENT):
        raise ValueError(f"a segment is [2, {SEGMENT}], not {list(x.shape)}")
    names = [i.name for i in session.get_inputs()]
    spec_out, wave_out = session.run(None, {names[0]: x[None].astype(np.float32),
                                            names[1]: spectrogram(x)[None]})  # fmt: skip
    return inverse_spectrogram(spec_out[0], SEGMENT) + wave_out[0]


# --- overlap-add -----------------------------------------------------------------------------------------------------


def blend_weight(length: int = SEGMENT) -> np.ndarray:
    """The triangular blend `demucs.apply.apply_model` uses (transition power 1), scaled to a peak of 1."""
    w = np.concatenate((np.arange(1, length // 2 + 1), np.arange(length - length // 2, 0, -1))).astype(np.float32)
    return w / w.max()


def overlap_add(chunks: Iterator[np.ndarray], run: Callable[[np.ndarray], np.ndarray],
                *, sources: int = len(STEMS)) -> Iterator[np.ndarray]:  # fmt: skip
    """The separated audio of a stream of float32 [2, n] chunks, as float32 [sources, 2, m] parts in order.

    `run` takes a full [2, SEGMENT] and returns [sources, 2, SEGMENT]. Holds one segment of input and one of output.
    As `apply_model`, a last segment shorter than SEGMENT gets the audio around it (zeros past the end) as context,
    and only its own samples count.
    """
    weight = blend_weight()
    buf = np.zeros((CHANNELS, 0), np.float32)  # the input from `buf_at` on
    buf_at = 0  # the sample `buf` starts at
    offset = 0  # the next segment starts here
    ended = False
    out = np.zeros((sources, CHANNELS, SEGMENT), np.float32)  # blending, from the sample `offset`
    total = np.zeros(SEGMENT, np.float32)
    source = iter(chunks)
    while True:
        while not ended and buf_at + buf.shape[1] < offset + SEGMENT:
            chunk = next(source, None)
            if chunk is None:
                ended = True
            else:
                buf = np.concatenate((buf, chunk), axis=1)
        known = buf_at + buf.shape[1]
        length = min(SEGMENT, known - offset)
        if length <= 0:
            return
        delta = SEGMENT - length
        start = offset - delta // 2  # as TensorChunk.padded: centred on what's left, with what came before it
        lo, hi = max(0, start), min(known, start + SEGMENT)
        segment = np.pad(buf[:, lo - buf_at:hi - buf_at], ((0, 0), (lo - start, start + SEGMENT - hi)))
        separated = run(segment)
        trim = delta // 2
        out[..., :length] += weight[:length] * separated[..., trim:trim + length]
        total[:length] += weight[:length]
        if known <= offset + STRIDE:  # that was the end of the audio
            yield out[..., :length] / total[:length]
            return
        yield out[..., :STRIDE] / total[:STRIDE]
        out = np.concatenate((out[..., STRIDE:], np.zeros((sources, CHANNELS, STRIDE), np.float32)), axis=-1)
        total = np.concatenate((total[STRIDE:], np.zeros(STRIDE, np.float32)))
        offset += STRIDE
        # the next segment may look back up to half a segment for its context
        keep = max(0, offset - SEGMENT // 2)
        if keep > buf_at:
            buf = buf[:, keep - buf_at:]
            buf_at = keep


# --- files -----------------------------------------------------------------------------------------------------------


def _encoder_args(fmt: str) -> list[str]:
    if fmt == "opus":
        return ["-c:a", "libopus", "-b:a", OPUS_BITRATE]
    return ["-c:a", "flac", "-sample_fmt", "s16"]


class _Encoders:
    """One ffmpeg per stem, each reading float32 stereo on stdin and writing `<stem>.<ext>` to `folder`."""

    def __init__(self, folder: Path, fmt: str) -> None:
        from musicreco.paths import ffmpeg_path

        ffmpeg = ffmpeg_path()
        if ffmpeg is None:
            raise StemsError("ffmpeg not found")
        self.errors = tempfile.TemporaryFile()  # noqa: SIM115 - closed by close()
        self.procs: list[subprocess.Popen] = []
        for stem in STEMS:
            cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(SAMPLE_RATE),
                   "-ac", str(CHANNELS), "-i", "-", *_encoder_args(fmt), str(folder / f"{stem}.{FORMATS[fmt]}")]  # fmt: skip
            self.procs.append(subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.errors))

    def write(self, part: np.ndarray) -> None:
        for proc, stem in zip(self.procs, part, strict=True):
            assert proc.stdin is not None
            proc.stdin.write(stem.T.astype("<f4").tobytes())

    def finish(self) -> None:
        for proc in self.procs:
            assert proc.stdin is not None
            proc.stdin.close()
        for proc in self.procs:
            if proc.wait(timeout=ENCODE_TIMEOUT_S) != 0:
                self.errors.seek(0)
                raise StemsError(f"could not encode the stems: {self.errors.read().decode(errors='replace').strip()[-300:]}")

    def close(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        self.errors.close()


def separate(path: Path | str, out_dir: Path | str, session: Any = None, *, fmt: str = DEFAULT_FORMAT,
             progress: Callable[[float], None] | None = None, duration_hint: float | None = None) -> dict:  # fmt: skip
    """Separate an audio file into `out_dir`/{drums,bass,other,vocals}.<ext>. Without `session`, opens the `htdemucs`
    model as the heavy worker would. `progress(fraction)` is called after each segment, and may raise to stop.
    Returns `{samples, seconds}`."""
    if fmt not in FORMATS:
        raise StemsError(f"format must be one of {', '.join(FORMATS)}")
    if session is None:
        from seedbox2.models import load_session

        session = load_session(MODEL, pool="heavy")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    decoded = waveform.decode_chunks(path, STRIDE, sample_rate=SAMPLE_RATE, channels=CHANNELS)
    chunks = (c.reshape(-1, CHANNELS).T for c in decoded)
    total = (duration_hint or 0) * SAMPLE_RATE
    encoders = _Encoders(out_dir, fmt)
    written = 0
    try:
        for part in overlap_add(chunks, lambda x: separate_segment(session, x)):
            encoders.write(part)
            written += part.shape[-1]
            if progress is not None:
                progress(min(0.99, written / total) if total else 0.0)
        if not written:
            raise StemsError("no audio")
        encoders.finish()
    finally:
        chunks.close()
        decoded.close()
        encoders.close()
    return {"samples": written, "seconds": round(written / SAMPLE_RATE, 3)}


# --- the cache: per content hash, with a size limit and LRU eviction --------------------------------------------------


def read_settings(settings: Any) -> dict:
    """The `stems` settings from a seedbox2.library.service.Settings (or None: the defaults)."""
    raw = settings.get(SETTINGS_KEY, {}) if settings is not None else {}
    raw = raw if isinstance(raw, dict) else {}
    limit = raw.get("limit_gb", DEFAULT_LIMIT_GB)
    fmt = raw.get("format")
    return {"limit_gb": float(limit) if isinstance(limit, (int, float)) and limit >= 0 else DEFAULT_LIMIT_GB,
            "format": fmt if fmt in FORMATS else DEFAULT_FORMAT, "low_power": bool(raw.get("low_power", False))}  # fmt: skip


def stems_dir() -> Path:
    from seedbox2.paths import folders

    return folders()["cache"] / "stems"


def folder_for(hash_: str, root: Path | None = None) -> Path:
    return (stems_dir() if root is None else root) / hash_[:2] / hash_


def current(lib: Any, hash_: str) -> Path | None:
    """The track's stems folder, if it has all four stems."""
    for art in reversed(lib.artifacts(hash_, ARTIFACT_KIND)):  # newest first
        if waveform.stem_files(art["path"]):
            return Path(art["path"])
    return None


def touch(folder: Path | str) -> None:
    """Mark the stems as just used: the eviction goes by the folder's modification time."""
    try:
        os.utime(folder)
    except OSError as e:
        log.debug("couldn't touch %s: %s", folder, e)


def _folders(root: Path) -> list[Path]:
    return [d for parent in root.iterdir() if parent.is_dir() for d in parent.iterdir()
            if d.is_dir() and TMP_MARK not in d.name] if root.is_dir() else []  # fmt: skip


def _size(folder: Path) -> int:
    total = 0
    for f in folder.iterdir():
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def usage(root: Path | None = None) -> dict:
    """What the cache holds: `{tracks, bytes}`."""
    found = _folders(stems_dir() if root is None else root)
    return {"tracks": len(found), "bytes": sum(_size(d) for d in found)}


def evict(lib: Any, limit_bytes: int, *, keep: Iterable[str] = (), root: Path | None = None) -> list[str]:
    """Delete the least recently used stems until the cache fits in `limit_bytes`, never those of `keep`. Returns the
    hashes evicted. Their waveform keeps its stem rows; separating them again is one job away."""
    keep = set(keep)
    found = []
    for d in _folders(stems_dir() if root is None else root):
        try:
            found.append((d.stat().st_mtime, d.name, d, _size(d)))
        except OSError:
            pass
    total = sum(f[3] for f in found)
    gone = []
    for _, hash_, d, size in sorted(found, key=lambda f: f[:2]):
        if total <= limit_bytes:
            break
        if hash_ in keep:
            continue
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            lib.delete_artifacts(hash_, ARTIFACT_KIND)
            total -= size
            gone.append(hash_)
    if gone:
        log.info("evicted the stems of %d tracks (%d MB left)", len(gone), total // 2**20)
    return gone


def _clear_partial(root: Path) -> None:
    """Folders a killed worker left half written. There is one heavy worker, so none of them is in use."""
    if root.is_dir():
        for parent in root.iterdir():
            for d in parent.iterdir() if parent.is_dir() else ():
                if TMP_MARK in d.name:
                    shutil.rmtree(d, ignore_errors=True)


def estimate_bytes(duration: float | None, fmt: str) -> int:
    return int((duration or 0) * ESTIMATE_BYTES_PER_S[fmt])


def model_names(settings: dict) -> tuple[str, ...]:
    """The models to try, best for these settings first: low-power mode falls back on the full model, and back."""
    return (MODEL_LOW_POWER, MODEL) if settings["low_power"] else (MODEL, MODEL_LOW_POWER)


# --- the job ---------------------------------------------------------------------------------------------------------


@register("stems", pool="heavy", per_track=True)
def stems_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"hash": ...}` (every new track, in the background) or with `"set": id` (the set the UI opened).

    A background job fills the cache up to its limit and then stops separating: it never evicts, or a big library
    would churn through its stems forever. A set's job evicts what was used longest ago, but nothing of that set.
    """
    hash_ = payload["hash"]
    lib = ctx.library
    track = lib.track(hash_)
    if track is None:
        raise StemsError(f"no track {hash_}")
    found = current(lib, hash_)
    if found is not None:
        touch(found)
        return {"hash": hash_, "path": str(found), "cached": True}
    settings = read_settings(ctx.settings)
    fmt, limit = settings["format"], int(settings["limit_gb"] * GB)
    keep = {hash_, *(lib.set_hashes(payload["set"]) or [] if payload.get("set") is not None else [])}
    if payload.get("set") is None and usage()["bytes"] + estimate_bytes(track["duration"], fmt) > limit:
        return {"hash": hash_, "skipped": "cache_full"}
    name, session = None, None
    for name in model_names(settings):
        session = ctx.session_or_none(name)
        if session is not None:
            break
    if session is None:
        return {"hash": hash_, "skipped": "model_missing"}
    sources = [Path(p) for p in lib.paths(hash_) if Path(p).is_file()]
    if not sources:
        raise StemsError("none of the track's files is there any more")
    final = folder_for(hash_)
    final.parent.mkdir(parents=True, exist_ok=True)
    _clear_partial(stems_dir())
    partial = final.with_name(f"{hash_}{TMP_MARK}{os.getpid()}")
    started = time.monotonic()
    try:
        info = separate(sources[0], partial, session, fmt=fmt, progress=ctx.progress, duration_hint=track["duration"])
        shutil.rmtree(final, ignore_errors=True)
        os.replace(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    touch(final)
    version = f"{name}:{MODELS[name].version}"
    lib.put_artifact(hash_, ARTIFACT_KIND, version, final)
    evicted = evict(lib, limit, keep=keep)
    seconds = time.monotonic() - started
    ctx.progress(1.0)
    return {"hash": hash_, "path": str(final), "model": version, "format": fmt, "bytes": _size(final),
            "seconds": round(seconds, 2), "audio_seconds": info["seconds"],
            "realtime_factor": round(seconds / max(info["seconds"], 1e-3), 3), "evicted": evicted}  # fmt: skip


def on_job_finished(jobs: Any, lib: Any, events: Any, row: dict) -> None:
    """The supervisor's listener: stems finished, so add their rows to the track's waveform and say how each set
    holding the track is doing (`stems.set`, see `set_status`)."""
    payload = row.get("payload")
    if row.get("kind") != "stems" or not isinstance(payload, dict) or not payload.get("hash"):
        return
    hash_ = payload["hash"]
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    if row.get("state") == "done" and result.get("path") and not result.get("cached"):
        waveform.submit(jobs, hash_)
    if events is not None:
        for set_id in lib.sets_with(hash_):
            status = set_status(lib, set_id)
            if status is not None:
                events.publish("stems.set", {k: v for k, v in status.items() if k != "tracks"}, key=f"stems.set:{set_id}")


def submit_for_set(jobs: Any, lib: Any, set_id: int) -> list[dict]:
    """The set is open: queue the stems it still needs ahead of everything else in the heavy queue, in running order.
    Jobs that were already waiting in the background for these tracks find the stems there and end at once."""
    hashes = lib.set_hashes(set_id) or []
    rows = [jobs.submit("stems", {"hash": h, "set": set_id}, priority="set") for h in hashes if current(lib, h) is None]
    jobs.bump(ids=[r["id"] for r in rows if r["state"] == "queued"])
    return rows


def set_status(lib: Any, set_id: int) -> dict | None:
    """`{set_id, ready, ready_tracks, total, progress, tracks}`: `ready` is the "stems ready for this set" indicator,
    `progress` runs from 0 to 1 over the whole set, and each of `tracks` is `{hash, stems, progress}` with `stems` as
    in the library view (true, "queued", "running", "failed" or false). None if there is no such set."""
    hashes = lib.set_hashes(set_id)
    if hashes is None:
        return None
    rows = {r["hash"]: r["status"] for r in lib.view_rows(hashes=hashes)}
    tracks = [{"hash": h, "stems": rows[h]["stems"] if h in rows else False,
               "progress": rows[h]["stems_progress"] if h in rows else None} for h in hashes]  # fmt: skip
    done = sum(t["stems"] is True for t in tracks)
    fraction = sum(1.0 if t["stems"] is True else t["progress"] or 0.0 for t in tracks)
    return {"set_id": set_id, "ready": bool(tracks) and done == len(tracks), "ready_tracks": done, "total": len(tracks),
            "progress": round(fraction / len(tracks), 4) if tracks else 0.0, "tracks": tracks}  # fmt: skip


def queue_missing(jobs: Any, lib: Any, settings: Any = None) -> int:
    """Queue background stems for the tracks without them, as many as fit in the cache's limit, if a model is there:
    what was imported before the model arrived, and what is left after a change of limit. Returns the number queued."""
    from seedbox2.models import store

    root = store.models_dir()
    s = read_settings(settings)
    if not any(store.installed(MODELS[n], root) for n in model_names(s)):
        return 0
    room = int(s["limit_gb"] * GB) - usage()["bytes"]
    queued = 0
    for t in lib.hashes_without_artifact(ARTIFACT_KIND):
        room -= estimate_bytes(t["duration"], s["format"])
        if room < 0:
            break
        jobs.submit("stems", {"hash": t["hash"]}, priority="background")
        queued += 1
    return queued
