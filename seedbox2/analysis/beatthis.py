"""Beat This! (CPJKU, ISMIR 2024) on onnxruntime: the mel front end in numpy, the network in chunks, the peak picker.

This reproduces upstream's `File2Beats` without torch: `beat_this.preprocessing.LogMelSpect` (22,050 Hz mono,
1024-point periodic Hann STFT centred with reflect padding, hop 441 = 50 frames/s, magnitude divided by
sqrt(1024) (torch.stft's normalized=True), 128 slaney-scale mel bands 30 Hz-11 kHz without area normalisation, log1p(1000 x)),
`inference.split_predict_aggregate` (1500-frame chunks, 6 frames of border discarded on each side, earlier chunks win
overlaps) and `Postprocessor("minimal")` (local maxima within ±70 ms whose logit is > 0, adjacent peaks merged,
downbeats moved to the nearest beat). `tests/test_seedbox2_beats.py` checks the front end against torchaudio when torch
is installed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np

SAMPLE_RATE = 22_050
N_FFT = 1024
HOP = 441
FPS = SAMPLE_RATE / HOP  # 50 frames/s
N_MELS = 128
F_MIN, F_MAX = 30.0, 11_000.0
CHUNK = 1500  # frames per network call (30 s), as upstream
BORDER = 6  # frames discarded at each side of a chunk


def _hz_to_mel(f: np.ndarray) -> np.ndarray:  # slaney, as torchaudio's mel_scale="slaney"
    f = np.asarray(f, dtype=np.float64)
    f_sp, min_log_hz = 200.0 / 3, 1000.0
    min_log_mel, logstep = min_log_hz / f_sp, np.log(6.4) / 27.0
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-9) / min_log_hz) / logstep, f / f_sp)


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float64)
    f_sp, min_log_hz = 200.0 / 3, 1000.0
    min_log_mel, logstep = min_log_hz / f_sp, np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), f_sp * m)


@lru_cache(maxsize=1)
def mel_filterbank() -> np.ndarray:
    """[N_FFT // 2 + 1, N_MELS] triangular filters, as torchaudio.functional.melscale_fbanks(norm=None, slaney)."""
    freqs = np.linspace(0, SAMPLE_RATE // 2, N_FFT // 2 + 1)
    pts = _mel_to_hz(np.linspace(_hz_to_mel(F_MIN), _hz_to_mel(F_MAX), N_MELS + 2))
    diff = np.diff(pts)
    slopes = pts[None, :] - freqs[:, None]
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    return np.maximum(0.0, np.minimum(down, up)).astype(np.float32)


@lru_cache(maxsize=1)
def _window() -> np.ndarray:
    n = np.arange(N_FFT)
    return (0.5 - 0.5 * np.cos(2 * np.pi * n / N_FFT)).astype(np.float32)  # periodic Hann (torch.hann_window)


def log_mel(x: np.ndarray, block: int = 2048) -> np.ndarray:
    """[frames, 128] float32 log-mel spectrogram of mono `x` at 22,050 Hz (frames = 1 + len(x) // 441)."""
    x = np.asarray(x, dtype=np.float32)
    if x.size < N_FFT // 2 + 1:
        x = np.pad(x, (0, N_FFT // 2 + 1 - x.size))
    pad = N_FFT // 2
    xp = np.pad(x, (pad, pad), mode="reflect")
    frames = 1 + x.size // HOP
    win = _window()
    norm = np.float32(1.0 / np.sqrt(N_FFT))  # torch.stft(normalized=True), what normalized="frame_length" means
    fb = mel_filterbank()
    out = np.empty((frames, N_MELS), np.float32)
    view = np.lib.stride_tricks.sliding_window_view(xp, N_FFT)[::HOP]
    for a in range(0, frames, block):  # a block at a time: a 6-minute track is never one big complex array
        seg = view[a : a + block] * win
        mag = np.abs(np.fft.rfft(seg, axis=1)).astype(np.float32) * norm
        out[a : a + block] = mag @ fb
    return np.log1p(1000.0 * out)


def chunk_starts(frames: int, chunk: int = CHUNK, border: int = BORDER) -> np.ndarray:
    """Start frames of the overlapping chunks, as upstream's split_piece(avoid_short_end=True)."""
    starts = np.arange(-border, frames - border, chunk - 2 * border)
    if frames > chunk - 2 * border:
        starts[-1] = frames - (chunk - border)
    return starts


Run = Callable[[np.ndarray], tuple[np.ndarray, np.ndarray]]  # [1, n, 128] -> beat, downbeat logits [n]


def session_runner(session: Any) -> Run:
    """A Run over an onnxruntime session (the input's name differs between the final0 and small0 files)."""
    name = session.get_inputs()[0].name

    def run(spect: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        beat, downbeat = session.run(None, {name: spect})
        return np.asarray(beat).reshape(-1), np.asarray(downbeat).reshape(-1)

    return run


def logits(spect: np.ndarray, run: Run, progress: Callable[[float], None] | None = None,
           chunk: int = CHUNK, border: int = BORDER) -> tuple[np.ndarray, np.ndarray]:  # fmt: skip
    """Frame-wise beat and downbeat logits over the whole spectrogram, a chunk at a time."""
    n = spect.shape[0]
    beat = np.full(n, -1000.0, np.float32)
    down = np.full(n, -1000.0, np.float32)
    starts = chunk_starts(n, chunk, border)
    preds = []
    for i, s in enumerate(starts):
        piece = spect[max(s, 0) : min(s + chunk, n)]
        left, right = max(0, -s), max(0, min(border, s + chunk - n))
        if left or right:
            piece = np.pad(piece, ((left, right), (0, 0)))
        b, d = run(piece[None].astype(np.float32))
        preds.append((s, b[border:-border], d[border:-border]))
        if progress:
            progress((i + 1) / len(starts))
    for s, b, d in reversed(preds):  # keep_first: earlier chunks overwrite later ones
        a = s + border
        k = min(len(b), n - a)
        beat[a : a + k], down[a : a + k] = b[:k], d[:k]
    return beat, down


def _dedupe(peaks: np.ndarray, width: int = 1) -> np.ndarray:
    out: list[float] = []
    if peaks.size == 0:
        return np.array(out)
    p, c = float(peaks[0]), 1
    for p2 in peaks[1:]:
        if p2 - p <= width:
            c += 1
            p += (p2 - p) / c
        else:
            out.append(p)
            p, c = float(p2), 1
    out.append(p)
    return np.array(out)


def _peaks(x: np.ndarray) -> np.ndarray:
    padded = np.pad(x, 3, constant_values=-np.inf)
    pooled = np.lib.stride_tricks.sliding_window_view(padded, 7).max(axis=1)
    return np.nonzero((x == pooled) & (x > 0))[0]


def peaks(beat: np.ndarray, downbeat: np.ndarray, fps: float = FPS) -> tuple[np.ndarray, np.ndarray]:
    """Beat and downbeat times (s) from the logits: upstream's minimal postprocessing."""
    bt = _dedupe(_peaks(beat)) / fps
    dt = _dedupe(_peaks(downbeat)) / fps
    if bt.size:
        dt = np.unique(bt[np.abs(bt[None, :] - dt[:, None]).argmin(axis=1)]) if dt.size else dt
    return bt, dt


@dataclass
class Activations:
    beat: np.ndarray  # logits per frame (50 frames/s)
    downbeat: np.ndarray
    beats: np.ndarray  # peak times (s)
    downbeats: np.ndarray
    fps: float = FPS


def run(x: np.ndarray, run_: Run, progress: Callable[[float], None] | None = None) -> Activations:
    """Mono audio at 22,050 Hz -> logits and peaks."""
    beat, down = logits(log_mel(x), run_, progress)
    bt, dt = peaks(beat, down)
    return Activations(beat, down, bt, dt)
