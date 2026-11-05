"""Waveforms (#49): 3-band peak and RMS at 150 points/s, a 1,200-column overview and per-stem overviews, in one small
binary file.

The audio is decoded to mono at 22,050 Hz (147 samples per point) and split by 4th-order Bessel filters, as Mixxx
does: low-pass 600 Hz, band-pass 600-4,000 Hz, high-pass 4,000 Hz. Each point keeps each band's peak (transients stay
sharp) and RMS (the body under it). Values are log-scaled to a byte: 0 is FLOOR_DB below full scale or quieter,
255 is full scale, so `amplitude = 10 ** ((byte / 255 - 1) * FLOOR_DB / 20)`. They are rounded to LEVELS evenly
spaced bytes: full 8-bit precision is noise to gzip and would not fit the 200 KB budget for a 6-minute track. Levels
are absolute, not normalised per track, so a quiet track looks quiet next to a loud one.

The decode is streamed (a second at a time with the filter state carried over), so a track is never in RAM.

File format (little-endian), stored gzipped (`<hash>.sbwf.gz` under the cache folder's `waveforms/`) and served by
`GET /api/tracks/{hash}/waveform` (with `Content-Encoding: gzip` to clients that accept it):

    header, HEADER_SIZE (32) bytes, HEADER_FORMAT:
        magic b"SBWF", version u16, header size u16, points per second u16, bands u8 (3: low, mid, high),
        values per band u8 (2: peak, RMS), points u32, overview columns u16 (1,200), stems u8 (0 or 4),
        floor dB u8, duration f32 (seconds), sample rate u32, levels u8, 3 bytes of padding
    detail:   per band (low, mid, high): peak[points] then RMS[points], u8
    overview: the same layout with [overview columns] values per row
    stems:    per stem (STEMS order: drums, bass, other, vocals): full-band peak[overview columns], u8

Rows are planar (one band's values in a row), which gzip compresses far better than interleaved points, and which a
renderer can upload as texture rows as they are.

Stem detail (#78) is a second file (`<hash>.sbsw.gz`, `GET /api/tracks/{hash}/waveform/stems`), so the file every view
loads stays under its 200 KB budget and only the views that draw stems pay for them. Same scale, same points:

    header, STEM_HEADER_SIZE (20) bytes, STEM_HEADER_FORMAT:
        magic b"SBSW", version u16, header size u16, points per second u16, stems u8 (4), floor dB u8, points u32,
        duration f32 (seconds)
    detail: per stem (STEMS order): full-band peak[points], u8

The job (`waveform`, light pool) runs right after import: stems run in the heavy pool, so waveforms never wait for
them. The stems job (#44) queues `waveform` again when it is done, which adds the stem overviews.
"""

from __future__ import annotations

import gzip
import os
import struct
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

from seedbox2.jobs import register

MAGIC = b"SBWF"
VERSION = 1
HEADER_FORMAT = "<4sHHHBBIHBBfIB3x"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
SAMPLE_RATE = 22_050
POINTS_PER_S = 150
HOP = SAMPLE_RATE // POINTS_PER_S  # 147 samples per point
OVERVIEW_COLUMNS = 1_200
BANDS = ("low", "mid", "high")
VALUES = ("peak", "rms")
STEMS = ("drums", "bass", "other", "vocals")  # HTDemucs's stems, in the order the file stores them
LOW_HZ, HIGH_HZ = 600, 4_000
FLOOR_DB = 48  # quieter than this is invisible on a linear display (0.4 % of full height)
LEVELS = 56  # 0.87 dB steps: finer than Rekordbox's 5-bit heights, and it keeps a 6-minute track under 200 KB
CHUNK_POINTS = POINTS_PER_S  # decode and filter a second at a time
DECODE_TIMEOUT_S = 300
GZIP_LEVEL = 9
ARTIFACT_KIND = "waveform"
MODEL_VERSION = f"v{VERSION}"
STEM_MAGIC = b"SBSW"
STEM_VERSION = 1
STEM_HEADER_FORMAT = "<4sHHHBBIf"
STEM_HEADER_SIZE = struct.calcsize(STEM_HEADER_FORMAT)
STEM_MODEL_VERSION = f"stems-v{STEM_VERSION}"  # a `waveform` artifact of its own version, next to the main file's

assert HEADER_SIZE == 32
assert STEM_HEADER_SIZE == 20
assert SAMPLE_RATE % POINTS_PER_S == 0


class WaveformError(ValueError):
    """The audio can't be decoded, or a waveform file is not one this version reads."""


@dataclass
class Waveform:
    duration: float  # seconds
    detail: np.ndarray  # uint8 [bands, 2 (peak, rms), points]
    overview: np.ndarray  # uint8 [bands, 2, OVERVIEW_COLUMNS]
    stems: np.ndarray = field(default_factory=lambda: np.zeros((0, OVERVIEW_COLUMNS), np.uint8))  # [0 or 4, cols]
    stem_detail: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.uint8))  # [0 or 4, points]: its own file

    @property
    def points(self) -> int:
        return self.detail.shape[2]

    def to_bytes(self) -> bytes:
        header = struct.pack(HEADER_FORMAT, MAGIC, VERSION, HEADER_SIZE, POINTS_PER_S, len(BANDS), len(VALUES),
                             self.points, OVERVIEW_COLUMNS, len(self.stems), FLOOR_DB, self.duration,
                             SAMPLE_RATE, LEVELS)  # fmt: skip
        return b"".join((header, self.detail.tobytes(), self.overview.tobytes(), self.stems.tobytes()))

    @classmethod
    def from_bytes(cls, data: bytes) -> Waveform:
        h = read_header(data)
        n, cols, bands = h["points"], h["overview_columns"], h["bands"] * h["values_per_band"]
        sizes = (bands * n, bands * cols, h["stems"] * cols)
        if len(data) != h["header_size"] + sum(sizes):
            raise WaveformError(f"waveform is {len(data)} bytes, its header says {h['header_size'] + sum(sizes)}")
        flat = np.frombuffer(data, np.uint8, offset=h["header_size"])
        detail, overview, stems = np.split(flat, [sizes[0], sizes[0] + sizes[1]])
        shape = (h["bands"], h["values_per_band"])
        return cls(h["duration"], detail.reshape(*shape, n), overview.reshape(*shape, cols),
                   stems.reshape(h["stems"], cols))  # fmt: skip


    def stems_to_bytes(self) -> bytes:
        """The stem detail file (see the module docstring)."""
        header = struct.pack(STEM_HEADER_FORMAT, STEM_MAGIC, STEM_VERSION, STEM_HEADER_SIZE, POINTS_PER_S,
                             len(self.stem_detail), FLOOR_DB, self.points, self.duration)  # fmt: skip
        return header + self.stem_detail.tobytes()


def read_header(data: bytes) -> dict[str, Any]:
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        raise WaveformError("not a Seedbox waveform")
    fields = struct.unpack_from(HEADER_FORMAT, data)
    names = ("magic", "version", "header_size", "points_per_s", "bands", "values_per_band", "points",
             "overview_columns", "stems", "floor_db", "duration", "sample_rate", "levels")  # fmt: skip
    h = dict(zip(names, fields, strict=True))
    if h["version"] != VERSION:
        raise WaveformError(f"waveform format v{h['version']}, this Seedbox reads v{VERSION}")
    return h


# --- computing -------------------------------------------------------------------------------------------------------


def _filters() -> list[np.ndarray]:
    """Second-order sections of the low, mid and high Bessel filters (4th order each)."""
    return [signal.bessel(4, LOW_HZ, "lowpass", fs=SAMPLE_RATE, output="sos", norm="mag"),
            signal.bessel(2, [LOW_HZ, HIGH_HZ], "bandpass", fs=SAMPLE_RATE, output="sos", norm="mag"),
            signal.bessel(4, HIGH_HZ, "highpass", fs=SAMPLE_RATE, output="sos", norm="mag")]  # fmt: skip


def to_byte(amplitude: np.ndarray) -> np.ndarray:
    """Linear amplitude (1.0 = full scale) to the log-scaled byte, one of LEVELS values."""
    db = 20 * np.log10(np.maximum(amplitude, 1e-12))
    level = np.clip(np.rint((db + FLOOR_DB) * ((LEVELS - 1) / FLOOR_DB)), 0, LEVELS - 1)
    return np.rint(level * (255 / (LEVELS - 1))).astype(np.uint8)


def to_amplitude(byte: np.ndarray) -> np.ndarray:
    """The inverse of to_byte (0 comes back as the floor, not silence)."""
    return 10 ** ((np.asarray(byte, np.float64) / 255 - 1) * FLOOR_DB / 20)


def decode_chunks(path: Path | str, chunk_samples: int, *, sample_rate: int = SAMPLE_RATE, channels: int = 1) -> Any:
    """Float32 chunks of the file's first audio stream, streamed from ffmpeg: `chunk_samples` samples per channel,
    interleaved. Mono at SAMPLE_RATE by default."""
    from musicreco.paths import ffmpeg_path

    path = Path(path)
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise WaveformError("ffmpeg not found")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-vn",
           "-ac", str(channels), "-ar", str(sample_rate), "-f", "f32le", "-"]  # fmt: skip
    errors = tempfile.TemporaryFile()  # a file, not a pipe: a chatty ffmpeg can't fill it and stall
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
    except OSError as e:
        errors.close()
        raise WaveformError(f"could not decode {path.name}: {e}") from e
    try:
        assert proc.stdout is not None
        frame = 4 * channels
        while chunk := proc.stdout.read(chunk_samples * frame):  # whole chunks until the last one
            yield np.frombuffer(chunk[: len(chunk) - len(chunk) % frame], np.float32)
        code = proc.wait(timeout=DECODE_TIMEOUT_S)
        if code != 0:
            errors.seek(0)
            last = errors.read().decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
            raise WaveformError(f"could not decode {path.name}: {last[0]}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
        errors.close()


def band_envelopes(chunks: Any, *, bands: bool = True,
                   check: Callable[[int], None] | None = None) -> tuple[np.ndarray, int]:  # fmt: skip
    """Linear per-point (peak, RMS) of each band, float32 [bands, 2, points], and the number of samples.

    `bands=False` keeps only the full-band signal (stem overviews): [1, 2, points]. `check(samples)` is called after
    each chunk (progress and cancel). The last point covers what is left of the audio after the last full HOP.
    """
    sos = _filters() if bands else []
    zi = [np.zeros((len(s), 2)) for s in sos]  # silence before the track
    out: list[np.ndarray] = []
    carry = np.zeros(0, np.float32)
    total = 0
    for chunk in chunks:
        total += len(chunk)
        x = np.concatenate((carry, chunk)) if len(carry) else chunk
        whole = len(x) - len(x) % HOP
        carry, x = x[whole:], x[:whole]
        if whole:
            out.append(_points(x, sos, zi))
        if check is not None:
            check(total)
    if len(carry):  # the tail: one more, shorter point
        out.append(_points(carry, sos, zi, short=True))
    if not out:
        raise WaveformError("no audio")
    return np.concatenate(out, axis=2), total


def _points(x: np.ndarray, sos: list[np.ndarray], zi: list[np.ndarray], *, short: bool = False) -> np.ndarray:
    signals = []
    for i, s in enumerate(sos):
        y, zi[i] = signal.sosfilt(s, x, zi=zi[i])
        signals.append(y)
    if not sos:
        signals.append(x)
    frames = np.stack(signals).reshape(len(signals), 1 if short else -1, len(x) if short else HOP)
    peak = np.abs(frames).max(axis=2)
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=2))
    return np.stack((peak, rms), axis=1).astype(np.float32)


def overview(env: np.ndarray, columns: int = OVERVIEW_COLUMNS) -> np.ndarray:
    """[..., 2, points] linear (peak, RMS) to [..., 2, columns]: the peak's max and the RMS's power mean per column.

    Tracks shorter than `columns` points repeat points over neighbouring columns.
    """
    n = env.shape[-1]
    starts = np.minimum((np.arange(columns) * n) // columns, n - 1)
    # reduceat over an empty range (starts[i] == starts[i + 1]) gives the element at starts[i], a count of 1
    counts = np.maximum(np.diff(np.append(starts, n)), 1)
    peak = np.maximum.reduceat(env[..., 0, :], starts, axis=-1)
    power = np.add.reduceat(np.square(env[..., 1, :], dtype=np.float64), starts, axis=-1)
    return np.stack((peak, np.sqrt(power / counts)), axis=-2).astype(np.float32)


def compute(path: Path | str, stems: dict[str, Path] | None = None, progress: Callable[[float], None] | None = None,
            duration_hint: float | None = None) -> Waveform:
    """The waveform of an audio file, with the overviews of `stems` ({name: file}, STEMS) when given.

    `progress(fraction)` is called after each second of audio (and may raise to cancel); fractions come from
    `duration_hint` (seconds), else stay 0 until the end.
    """
    stems = {k: Path(v) for k, v in (stems or {}).items()}
    if stems and set(stems) != set(STEMS):
        raise WaveformError(f"stems must be {', '.join(STEMS)}, not {', '.join(sorted(stems))}")
    work = (duration_hint or 0) * SAMPLE_RATE * (1 + len(stems) * 0.25)  # stems skip the filters: about 1/4 the work
    done = 0.0

    def check(weight: float) -> Callable[[int], None]:
        def f(samples: int) -> None:
            if progress is not None:
                progress(min(0.99, (done + samples * weight) / work) if work else 0.0)

        return f

    env, total = band_envelopes(decode_chunks(path, CHUNK_POINTS * HOP), check=check(1.0))
    done += total
    stem_rows = []
    stem_detail = []
    for name in STEMS if stems else ():
        stem_env, samples = band_envelopes(decode_chunks(stems[name], CHUNK_POINTS * HOP), bands=False,
                                           check=check(0.25))  # fmt: skip
        done += samples * 0.25
        stem_env = _fit(stem_env, env.shape[-1])  # stems are cut from the track, but may be a few samples off
        stem_rows.append(overview(stem_env)[0, 0])
        stem_detail.append(stem_env[0, 0])
    wf = Waveform(round(total / SAMPLE_RATE, 3), to_byte(env), to_byte(overview(env)),
                  to_byte(np.array(stem_rows, np.float32).reshape(len(stem_rows), OVERVIEW_COLUMNS)),
                  to_byte(np.array(stem_detail, np.float32).reshape(len(stem_detail), env.shape[-1] if stems else 0)))  # fmt: skip
    if progress is not None:
        progress(1.0)
    return wf


def _fit(env: np.ndarray, points: int) -> np.ndarray:
    if env.shape[-1] >= points:
        return env[..., :points]
    pad = np.zeros((*env.shape[:-1], points - env.shape[-1]), env.dtype)
    return np.concatenate((env, pad), axis=-1)


# --- files and the job -----------------------------------------------------------------------------------------------


def waveform_dir() -> Path:
    from seedbox2.paths import folders

    return folders()["cache"] / "waveforms"


def artifact_path(hash_: str) -> Path:
    return waveform_dir() / hash_[:2] / f"{hash_}.sbwf.gz"


def stem_artifact_path(hash_: str) -> Path:
    return waveform_dir() / hash_[:2] / f"{hash_}.sbsw.gz"


def write(wf: Waveform, path: Path) -> int:
    """Write the gzipped waveform file atomically; returns its size in bytes."""
    return _write_gz(wf.to_bytes(), path)


def write_stems(wf: Waveform, path: Path) -> int:
    """Write the gzipped stem detail file atomically; returns its size in bytes."""
    return _write_gz(wf.stems_to_bytes(), path)


def _write_gz(raw: bytes, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(raw, GZIP_LEVEL, mtime=0)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(data)


def read(path: Path) -> Waveform:
    return Waveform.from_bytes(gzip.decompress(path.read_bytes()))


def stem_files(folder: Path | str) -> dict[str, Path] | None:
    """`{stem: file}` in a stems artifact folder (one `<stem>.<ext>` per STEMS name), or None if any is missing."""
    found: dict[str, Path] = {}
    for f in sorted(Path(folder).iterdir()) if Path(folder).is_dir() else ():
        if f.stem in STEMS and f.stem not in found and f.is_file():
            found[f.stem] = f
    return found if len(found) == len(STEMS) else None


def current_artifact(lib: Any, hash_: str) -> Path | None:
    """The track's waveform file in this version, if computed and still there."""
    for art in lib.artifacts(hash_, ARTIFACT_KIND):
        if art["model_version"] == MODEL_VERSION and Path(art["path"]).is_file():
            return Path(art["path"])
    return None


def current_stem_artifact(lib: Any, hash_: str) -> Path | None:
    """The track's stem detail file in this version, if computed and still there."""
    for art in lib.artifacts(hash_, ARTIFACT_KIND):
        if art["model_version"] == STEM_MODEL_VERSION and Path(art["path"]).is_file():
            return Path(art["path"])
    return None


@register("waveform", pool="light")
def waveform_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"hash": ...}`: compute the track's waveform from its first readable file (and its stems, if the
    library has them), store it and record it as the track's `waveform` artifact, with the stem detail file next to it
    under STEM_MODEL_VERSION."""
    hash_ = payload["hash"]
    lib = ctx.library
    track = lib.track(hash_)
    if track is None:
        raise WaveformError(f"no track {hash_}")
    sources = [Path(p) for p in lib.paths(hash_) if Path(p).is_file()]
    if not sources:
        raise WaveformError("none of the track's files is there any more")
    stems = None
    for art in reversed(lib.artifacts(hash_, "stems")):  # newest first
        if stems := stem_files(art["path"]):
            break
    wf = compute(sources[0], stems, progress=ctx.progress, duration_hint=track["duration"])
    path = artifact_path(hash_)
    size = write(wf, path)
    lib.put_artifact(hash_, ARTIFACT_KIND, MODEL_VERSION, path)
    if stems:
        stem_path = stem_artifact_path(hash_)
        write_stems(wf, stem_path)
        lib.put_artifact(hash_, ARTIFACT_KIND, STEM_MODEL_VERSION, stem_path)
    return {"hash": hash_, "path": str(path), "bytes": size, "points": wf.points, "stems": len(wf.stems)}


def submit(jobs: Any, hash_: str, *, priority: int | str = "library", front: bool = False) -> dict:
    """Queue the waveform job for a track (the importer calls this first, before anything heavier)."""
    return jobs.submit("waveform", {"hash": hash_}, priority=priority, front=front)
