"""The library's track key: a hash of the decoded audio, so retagging, renaming and moving a file keep its analysis.

content_hash() decodes the first audio stream with ffmpeg to 16-bit stereo PCM at 44.1 kHz and hashes the number
of samples, the first EDGE_S seconds and the last EDGE_S seconds (SHA-1). Tags (ID3, Vorbis comments, MP4 atoms,
cover art) are not audio, so they don't count; a re-encode is different audio and gets a new hash. The decode is
streamed: only the head and a rolling tail are kept in memory.

HASH_VERSION is part of the hash, so changing the recipe can never collide with keys made by an older one.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from musicreco.paths import ffmpeg_path

HASH_VERSION = 1
SAMPLE_RATE = 44_100
CHANNELS = 2
FRAME_BYTES = CHANNELS * 2  # s16le
EDGE_S = 30
CHUNK = 1 << 16
DECODE_TIMEOUT_S = 300


class HashError(ValueError):
    """The file can't be decoded (not audio, damaged, or no ffmpeg)."""


@dataclass(frozen=True)
class ContentHash:
    hash: str
    duration: float  # seconds of decoded audio


def content_hash(path: Path | str) -> ContentHash:
    path = Path(path)
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise HashError("ffmpeg not found")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-vn",
           "-map_metadata", "-1", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]  # fmt: skip
    edge = EDGE_S * SAMPLE_RATE * FRAME_BYTES
    head = bytearray()
    tail: deque[bytes] = deque()
    tail_len = total = 0
    errors = tempfile.TemporaryFile()  # a file, not a pipe: a chatty ffmpeg can't fill it and stall
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
    except OSError as e:
        errors.close()
        raise HashError(f"could not decode {path.name}: {e}") from e
    try:
        assert proc.stdout is not None
        while chunk := proc.stdout.read(CHUNK):
            total += len(chunk)
            if len(head) < edge:
                head += chunk[: edge - len(head)]
            tail.append(chunk)
            tail_len += len(chunk)
            while tail_len - len(tail[0]) >= edge:
                tail_len -= len(tail.popleft())
        code = proc.wait(timeout=DECODE_TIMEOUT_S)
        errors.seek(0)
        stderr = errors.read()
    except subprocess.TimeoutExpired as e:
        raise HashError(f"could not decode {path.name}: timed out") from e
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
        errors.close()
    if code != 0:
        last = stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise HashError(f"could not decode {path.name}: {last[0]}")
    frames = total // FRAME_BYTES
    if frames == 0:
        raise HashError(f"no audio in {path.name}")
    h = hashlib.sha1(f"seedbox2-audio-v{HASH_VERSION}:{SAMPLE_RATE}:{CHANNELS}:{frames}:".encode())
    h.update(head)
    h.update(b"".join(tail)[-edge:])
    return ContentHash(h.hexdigest(), round(frames / SAMPLE_RATE, 3))
