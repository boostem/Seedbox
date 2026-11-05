"""A set to files (#70): the bus on disk, the master chain over it, and the encoder as it goes.

`SetRender` writes the set's bus before its master to a spool: raw float32, one file per checkpoint. Then the make-up is
measured over the spool, and a last pass through the chain goes to ffmpeg with `Meter` listening. The bus is what makes
this cheap to run: finding the make-up costs chain passes rather than renders, and a crash in any of it costs at most
what came after the last checkpoint. `.part` becomes the file's name only once the encoder has finished.

The spool's name is a hash of what goes into the bus, so a resume can't mix an old plan into a new one.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from musicreco.mixrender import _aac_encoder
from musicreco.paths import ffmpeg_path

from . import master as master_chain
from . import stream
from .plan import MixPlan
from .setrender import SetRender, Source
from .stream import CHUNK, SR

SPOOL_VERSION = 1  # bump when the same plans and files would give another bus
BYTES_PER_SECOND = 2 * 4 * SR
DISK_MARGIN = 1.1


@dataclass(frozen=True)
class Format:
    name: str
    extension: str
    args: tuple[str, ...]
    cue_type: str  # the cue sheet's word for the file
    chapters: bool = True
    label: str = ""


FORMATS = {
    "wav": Format("wav", "wav", ("-c:a", "pcm_s24le", "-rf64", "auto", "-f", "wav"), "WAVE", False, "WAV (24-bit)"),
    "flac": Format("flac", "flac", ("-c:a", "flac", "-sample_fmt", "s32", "-f", "flac"), "WAVE", True, "FLAC (24-bit)"),
    "mp3": Format("mp3", "mp3", ("-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3", "-f", "mp3"), "MP3", True, "MP3 320"),
    "aac": Format("aac", "m4a", ("-b:a", "256k", "-movflags", "+faststart", "-f", "ipod"), "WAVE", True, "AAC 256 (M4A)"),
}


@dataclass(frozen=True)
class Chapter:
    start_s: float
    end_s: float
    title: str


def spool_key(chain: list[MixPlan], sources: list[Source], sr: int = SR) -> str:
    """What the bus depends on: the plans as they play and the files they play, by content."""
    raw = {"version": SPOOL_VERSION, "sr": sr, "plans": [p.model_dump() for p in chain],
           "tracks": [[s.hash, s.gain_db, sorted(s.stems) if s.stems else None] for s in sources]}  # fmt: skip
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _fsync(path: Path) -> None:
    with open(path, "rb+") as f:
        os.fsync(f.fileno())


class Spool:
    """A folder of raw bus files and the latest checkpoint: `state.pickle` says how many files count."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.state_file = self.folder / "state.pickle"

    def file(self, index: int) -> Path:
        return self.folder / f"{index:05d}.f32"

    def load(self) -> tuple[SetRender, int] | None:
        """(the render at the last checkpoint, its file count), or None if there's nothing usable."""
        try:
            with open(self.state_file, "rb") as f:
                saved = pickle.load(f)
            render, files = saved["render"], saved["files"]
        except (OSError, EOFError, ValueError, AttributeError, ImportError, IndexError, pickle.UnpicklingError, KeyError):
            return None
        written = sum(self.file(i).stat().st_size for i in range(files) if self.file(i).exists())
        if written != render.at * 2 * 4:
            return None
        for stale in self.folder.glob("*.f32"):
            if int(stale.stem) >= files:
                stale.unlink()
        return render, files

    def save(self, render: SetRender, files: int) -> None:
        _fsync(self.file(files - 1))
        scratch = self.state_file.with_suffix(".tmp")
        with open(scratch, "wb") as f:
            pickle.dump({"render": render, "files": files}, f, protocol=pickle.HIGHEST_PROTOCOL)
            f.flush()
            os.fsync(f.fileno())
        os.replace(scratch, self.state_file)

    def chunks(self, files: int) -> Iterator[np.ndarray]:
        """The bus, (2, k) float32, at most `CHUNK` at a time."""
        for i in range(files):
            with open(self.file(i), "rb") as f:
                while True:
                    raw = f.read(CHUNK * 8)
                    if not raw:
                        break
                    yield np.frombuffer(raw, "<f4").reshape(-1, 2).T

    def remove(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


def render_bus(chain: list[MixPlan], sources: list[Source], spool: Spool, *, sr: int = SR,
               progress: Callable[[float, int], None] | None = None) -> tuple[int, int]:  # fmt: skip
    """The set's bus into `spool`, from its last checkpoint if it has one. (its length in samples, how many files).
    `progress(fraction, segment)` may raise to stop it: what's in the spool stays for the next run."""
    spool.folder.mkdir(parents=True, exist_ok=True)
    found = spool.load()
    if found is None:
        for old in spool.folder.glob("*.f32"):
            old.unlink()
        render, files = SetRender(chain, sources, sr), 0
    else:
        render, files = found
    out = None
    try:
        while not render.done:
            if out is None:
                out = open(spool.file(files), "wb")
                files += 1
            render.step().T.astype("<f4").tofile(out)
            if progress is not None:
                progress(render.at / render.total, render.segment)
            if render.checkpoint_due():
                out.close()
                out = None
                render.checkpointed_now()
                spool.save(render, files)
    finally:
        if out is not None:
            out.close()
    return render.total, files


def _chain_pass(spool: Spool, files: int, target_lufs: float, makeup_db: float, sr: int) -> Iterator[tuple[np.ndarray, master_chain.Master]]:
    """The mastered bus, in time with its input, each piece with the chain that made it, to read what it did."""
    chain = master_chain.Master(target_lufs, makeup_db, sr)
    skip = chain.limiter.latency
    for x in spool.chunks(files):
        y = chain.process(x)
        if skip:
            drop = min(skip, y.shape[1])
            y = y[:, drop:]
            skip -= drop
        if y.shape[1]:
            yield y.astype(np.float32), chain
    y = chain.flush()[:, skip:]
    yield y.astype(np.float32), chain


def find_makeup(spool: Spool, files: int, target_lufs: float, sr: int = SR, on_pass: Callable[[int], None] | None = None) -> float:
    """`master.find_makeup` over the spool: a whole pass per try."""
    passes = [0]

    def trial(makeup: float) -> tuple[float, float]:
        meter = stream.Loudness(sr)
        chain = None
        for y, chain in _chain_pass(spool, files, target_lufs, makeup, sr):
            meter.feed(y)
            if on_pass is not None:
                on_pass(passes[0])
        passes[0] += 1
        return meter.integrated(), round(chain.limiter.deepest_db, 2)

    return master_chain.search_makeup(trial, target_lufs)


def _metadata(chapters: list[Chapter], title: str) -> str:
    def esc(s: str) -> str:
        return "".join("\\" + c if c in "=;#\\\n" else c for c in s)

    lines = [";FFMETADATA1", f"title={esc(title)}"]
    for c in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(c.start_s * 1000)}", f"END={round(c.end_s * 1000)}", f"title={esc(c.title)}"]
    return "\n".join(lines) + "\n"


@dataclass
class Encoder:
    """ffmpeg, taking float32 as it comes. The file only gets its name when `close` has succeeded."""

    fmt: Format
    out_path: Path
    sr: int = SR
    title: str = ""
    chapters: list[Chapter] = field(default_factory=list)

    def __post_init__(self) -> None:
        ffmpeg = ffmpeg_path()
        if ffmpeg is None:
            raise ValueError("ffmpeg not found")
        self.part = self.out_path.with_name(self.out_path.name + ".part")
        self.err = tempfile.TemporaryFile()  # noqa: SIM115 - it lives as long as ffmpeg
        self.meta = None
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(self.sr), "-ac", "2", "-i", "-"]
        if self.fmt.chapters or self.title:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(_metadata(self.chapters if self.fmt.chapters else [], self.title))
                self.meta = Path(f.name)
            cmd += ["-f", "ffmetadata", "-i", str(self.meta), "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        args = list(self.fmt.args)
        if self.fmt.name == "aac":
            args = ["-c:a", _aac_encoder(ffmpeg), *args]
        cmd += [*args, str(self.part)]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.err)
        except OSError as e:
            self._cleanup()
            raise ValueError(f"could not encode the mix: {e}") from e

    def _cleanup(self) -> None:
        self.err.close()
        if self.meta is not None:
            self.meta.unlink(missing_ok=True)

    def write(self, x: np.ndarray) -> None:
        try:
            self.proc.stdin.write(np.clip(x, -1.0, 1.0).T.astype("<f4").tobytes())
        except OSError:
            pass  # ffmpeg stopped reading: its exit code says why

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        code = self.proc.wait()
        self.err.seek(0)
        tail = self.err.read().decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        self._cleanup()
        if code != 0:
            self.part.unlink(missing_ok=True)
            raise ValueError(f"could not encode the mix: {tail[0]}")
        os.replace(self.part, self.out_path)

    def abort(self) -> None:
        self.proc.kill()
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait()
        self._cleanup()
        self.part.unlink(missing_ok=True)


def chapters_of(starts: list[int], names: list[str], total: int, sr: int = SR) -> list[Chapter]:
    ends = [*starts[1:], total]
    return [Chapter(a / sr, b / sr, name) for a, b, name in zip(starts, ends, names)]


def _cue_time(seconds: float) -> str:
    frames = round(seconds * 75)
    return f"{frames // 4500:02d}:{frames // 75 % 60:02d}:{frames % 75:02d}"


def _cue_text(s: str) -> str:
    return " ".join(s.replace('"', "'").split())


def cue_sheet(entries: list[tuple[float, str, str | None]], title: str, file_name: str, cue_type: str, measured: dict, target_lufs: float) -> str:
    """A cue sheet: (start in seconds, title, performer) per track, with the render's loudness as REM lines."""
    lra = measured["lra_lu"]
    lines = [f'REM TARGET_LUFS {target_lufs:g}', f"REM INTEGRATED_LUFS {measured['integrated_lufs']:.1f}",
             f"REM LRA_LU {'n/a' if lra is None else f'{lra:.1f}'}", f"REM TRUE_PEAK_DBTP {measured['true_peak_dbtp']:.1f}",
             f'TITLE "{_cue_text(title)}"', f'FILE "{_cue_text(file_name)}" {cue_type}']  # fmt: skip
    for i, (start, name, performer) in enumerate(entries, 1):
        lines += [f"  TRACK {i:02d} AUDIO", f'    TITLE "{_cue_text(name)}"']
        if performer:
            lines.append(f'    PERFORMER "{_cue_text(performer)}"')
        lines.append(f"    INDEX 01 {_cue_time(start)}")
    return "\n".join(lines) + "\n"


def free_name(folder: Path, stem: str, extension: str) -> Path:
    """`<stem>.<extension>`, or `<stem> (2).<extension>`: an export never replaces an earlier one."""
    for i in range(1, 10000):
        path = folder / f"{stem if i == 1 else f'{stem} ({i})'}.{extension}"
        if not path.exists():
            return path
    raise ValueError(f"too many exports called {stem}")


def check_disk(folder: Path, seconds: float) -> None:
    """The spool is 4 bytes per sample and channel. Better said now than two hours in."""
    need = seconds * BYTES_PER_SECOND * DISK_MARGIN
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if free < need:
        raise ValueError(f"not enough disk space: rendering this set takes about {need / 1e9:.1f} GB, and {free / 1e9:.1f} GB is free")


def write_mix(spool: Spool, files: int, fmt: Format, out_path: Path, target_lufs: float, makeup_db: float, *, sr: int = SR, title: str = "",
              chapters: list[Chapter] | None = None, progress: Callable[[], None] | None = None) -> dict:  # fmt: skip
    """The last pass: the chain into the encoder, with the meter listening. `{measured, master}`."""
    encoder = Encoder(fmt, out_path, sr, title, chapters or [])
    meter = master_chain.Meter(sr)
    chain = None
    try:
        for y, chain in _chain_pass(spool, files, target_lufs, makeup_db, sr):
            meter.feed(y)
            encoder.write(y)
            if progress is not None:
                progress()
    except BaseException:
        encoder.abort()
        raise
    encoder.close()
    report = {"glue_db": round(chain.glue.deepest_db, 2), "limiter_db": round(chain.limiter.deepest_db, 2), "makeup_db": round(makeup_db, 2), "target_lufs": target_lufs}  # fmt: skip
    return {"measured": meter.result(), "master": report}

