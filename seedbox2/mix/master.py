"""The master chain (#58): bus glue, make-up to the target, true-peak limiter, and the numbers an export reports.

The same chain runs live in `frontend2/src/audio/engine/master.ts`, so it is causal here too: nothing looks further
ahead than the limiter's fixed latency, and `tests/test_seedbox2_engine_parity.py` nulls the two. Change both together.
It works in chunks of any size, so a streaming render can feed it (#70); `Master.__call__` is a whole render.

The target decides the make-up gain. The tracks reach the bus with their bodies at `DECK_LUFS`
(`loudness.track_gain_db`), so `open_loop_makeup_db` lands near the target with nothing measured, which is
all the live engine can do. An export measures its own render instead (`find_makeup`), and can hand that make-up to the
live engine so both play at the same level.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy import ndimage, signal

from musicreco import mixrender

SR = mixrender.SR
DECK_LUFS = -12.0  # where track gain puts a track's body

TARGETS = {"streaming": -14.0, "club": -9.0}
DEFAULT_TARGET = "streaming"
CEILING_DBTP = -1.6  # leaves room for AAC overshoot under -1 dBTP
# What glue and limiter take off a track whose body is at DECK_LUFS, and more the harder the limiter works (23 real tracks, README)
OPEN_LOOP_DB, OPEN_LOOP_PER_LU = 1.9, 0.64
MAX_MAKEUP_DB = 18.0
MAX_LIMITER_DB = 12.0
MAKEUP_TOLERANCE_LU = 0.1
MAKEUP_PASSES = 8

GLUE_HOP = 64
GLUE_RATIO = 2.0
GLUE_THRESHOLD_DB = -12.0  # RMS per channel
GLUE_KNEE_DB = 6.0
GLUE_DETECT_S = 0.01
GLUE_ATTACK_S = 0.03
GLUE_RELEASE_S = 0.3

LIMIT_WINDOW_S = 0.025
TP_TAPS = 24  # per input sample: 12 under-read noise by 0.3 dB, clipped audio by 1.4
TP_DELAY = TP_TAPS // 2
TP_PHASES = 4

LRA_BLOCK_S, LRA_HOP_S = 3.0, 0.1
LRA_ABSOLUTE_GATE, LRA_RELATIVE_GATE = -70.0, -20.0


def _tp_filters() -> np.ndarray:
    """(phase 1..3, taps): the Hann-windowed sinc that reads between samples, x[n - tap] being tap samples back."""
    rows = []
    for phase in range(1, TP_PHASES):
        offset = TP_DELAY - phase / TP_PHASES - np.arange(TP_TAPS)
        h = np.sinc(offset) * (0.5 + 0.5 * np.cos(np.pi * offset / TP_DELAY))
        rows.append(h / h.sum())
    return np.array(rows)


_TP = _tp_filters()


class Glue:
    """2:1 feed-forward bus compressor, stereo linked. A level every GLUE_HOP samples sets the gain the next hop ramps to."""

    def __init__(self, sr: int = SR) -> None:
        pole = lambda seconds: 1 - math.exp(-GLUE_HOP / (seconds * sr))
        self._detect, self._attack, self._release = pole(GLUE_DETECT_S), pole(GLUE_ATTACK_S), pole(GLUE_RELEASE_S)
        self._env = 0.0
        self._reduction = 0.0
        self._from = 1.0
        self._to = 1.0
        self._rest = np.zeros(0)
        self.deepest_db = 0.0

    def _close(self, hop_power: float) -> float:
        self._env += self._detect * (hop_power - self._env)
        over = 10 * math.log10(self._env + 1e-12) - GLUE_THRESHOLD_DB
        half = GLUE_KNEE_DB / 2
        if over <= -half:
            want = 0.0
        elif over >= half:
            want = over * (1 - 1 / GLUE_RATIO)
        else:
            want = (1 - 1 / GLUE_RATIO) * (over + half) ** 2 / (2 * GLUE_KNEE_DB)
        self._reduction += (self._attack if want > self._reduction else self._release) * (want - self._reduction)
        self.deepest_db = max(self.deepest_db, self._reduction)
        return 10 ** (-self._reduction / 20)

    def gain(self, x: np.ndarray) -> np.ndarray:
        """The gain per sample for (2, n) audio, and takes the audio in."""
        filled = self._rest.size
        power = np.concatenate([self._rest, (np.asarray(x, np.float64) ** 2).sum(axis=0) / 2])
        hops = power.size // GLUE_HOP
        knots = [self._from, self._to]
        if hops:
            knots += [self._close(p) for p in power[: hops * GLUE_HOP].reshape(hops, GLUE_HOP).mean(axis=1).tolist()]
        at = np.arange(filled, power.size)
        k = at // GLUE_HOP
        u = (at % GLUE_HOP) / GLUE_HOP
        points = np.asarray(knots)
        self._from, self._to = knots[-2], knots[-1]
        self._rest = power[hops * GLUE_HOP :]
        return points[k] + (points[k + 1] - points[k]) * u


class Limiter:
    """Look-ahead true-peak limiter: the peak at 4x, the minimum of what's required over 25 ms, averaged over 25 ms. The
    average of minima that all contain a sample's own requirement can't exceed it, so no peak gets through. Output is
    `latency` samples late."""

    def __init__(self, ceiling_db: float = CEILING_DBTP, sr: int = SR) -> None:
        self.width = int(LIMIT_WINDOW_S * sr) | 1
        self.latency = self.width - 1 + TP_DELAY
        self.ceiling = 10 ** (ceiling_db / 20)
        self._samples = np.zeros((2, TP_TAPS - 1))
        self._required = np.ones(self.width - 1)
        self._minima = np.ones(self.width - 1)
        self._line = np.zeros((2, self.latency))
        self.deepest_db = 0.0

    def run(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[1]
        if n == 0:
            return np.zeros((2, 0))
        joined = np.concatenate([self._samples, np.asarray(x, np.float64)], axis=1)
        peak = np.abs(joined[:, TP_TAPS - 1 - TP_DELAY : joined.shape[1] - TP_DELAY]).max(axis=0)
        for h in _TP:
            peak = np.maximum(peak, np.abs(signal.lfilter(h, 1.0, joined, axis=1)[:, TP_TAPS - 1 :]).max(axis=0))
        self._samples = joined[:, -(TP_TAPS - 1) :]
        required = np.concatenate([self._required, np.minimum(1.0, self.ceiling / np.maximum(peak, 1e-9))])
        half = (self.width - 1) // 2
        minima = np.concatenate([self._minima, ndimage.minimum_filter1d(required, self.width, mode="nearest")[half : half + n]])
        total = np.concatenate([[0.0], np.cumsum(minima)])
        gain = (total[self.width :] - total[: -self.width]) / self.width
        self._required, self._minima = required[-(self.width - 1) :], minima[-(self.width - 1) :]
        if gain.size:
            self.deepest_db = max(self.deepest_db, float(-20 * np.log10(gain.min())))
        line = np.concatenate([self._line, joined[:, TP_TAPS - 1 :]], axis=1)
        self._line = line[:, -self.latency :]
        return np.clip(line[:, :n] * gain, -self.ceiling, self.ceiling)

    def flush(self) -> np.ndarray:
        return self.run(np.zeros((2, self.latency)))


def open_loop_makeup_db(target_lufs: float) -> float:
    return target_lufs - DECK_LUFS + OPEN_LOOP_DB + OPEN_LOOP_PER_LU * (target_lufs - TARGETS[DEFAULT_TARGET])


class Master:
    """The chain for one target. `makeup_db` None: the open-loop make-up, as the live engine plays without a render.
    Call it with a render's whole mix, or `process` chunks and `flush`."""

    def __init__(self, target_lufs: float = TARGETS[DEFAULT_TARGET], makeup_db: float | None = None, sr: int = SR) -> None:
        self.target_lufs = target_lufs
        self.makeup_db = open_loop_makeup_db(target_lufs) if makeup_db is None else makeup_db
        self.sr = sr
        self.glue = Glue(sr)
        self.limiter = Limiter(CEILING_DBTP, sr)
        self._makeup = 10 ** (self.makeup_db / 20)

    def process(self, x: np.ndarray) -> np.ndarray:
        return self.limiter.run(np.asarray(x, np.float64) * self.glue.gain(x) * self._makeup)

    def flush(self) -> np.ndarray:
        return self.limiter.flush()

    def __call__(self, mix: np.ndarray, sr: int = SR) -> tuple[np.ndarray, dict]:
        """`mixrender.render_audio`'s `master`: (the mastered mix, in time with its input; what the chain did)."""
        chain = Master(self.target_lufs, self.makeup_db, sr)
        latency = chain.limiter.latency
        out = np.concatenate([chain.process(mix), chain.flush()], axis=1)[:, latency:]
        return out.astype(np.float32), {"glue_db": round(chain.glue.deepest_db, 2), "limiter_db": round(chain.limiter.deepest_db, 2),
                                        "makeup_db": round(chain.makeup_db, 2), "target_lufs": chain.target_lufs}  # fmt: skip


def find_makeup(mix: np.ndarray, target_lufs: float, sr: int = SR) -> float:
    """The make-up that brings this mix (before the chain) to the target, measured. Past the limiter a dB of make-up buys
    less than a LU, down to nothing, so it brackets the target and closes in. It won't make the limiter work harder than
    MAX_LIMITER_DB to get there: a mix that dynamic stays under the target, and the tracklist says what it reached."""
    makeup = open_loop_makeup_db(target_lufs)
    best, best_error = None, math.inf
    low: float | None = None  # the most make-up known to fall short
    high: float | None = None  # the least known to overshoot, or to take too much limiting
    for _ in range(MAKEUP_PASSES):
        out, report = Master(target_lufs, makeup, sr)(mix, sr)
        error = target_lufs - mixrender.integrated_lufs(out, sr)
        too_hard = report["limiter_db"] > MAX_LIMITER_DB
        if not too_hard and abs(error) < best_error:
            best, best_error = makeup, abs(error)
        if abs(error) <= MAKEUP_TOLERANCE_LU and not too_hard:
            break
        if too_hard or error < 0:
            high = makeup
        else:
            low = makeup
        if low is not None and high is not None:
            if high - low < MAKEUP_TOLERANCE_LU:
                break
            makeup = (low + high) / 2
        else:
            makeup = min(max(makeup + (error if not too_hard else -MAX_LIMITER_DB / 2), -MAX_MAKEUP_DB), MAX_MAKEUP_DB)
    return round(open_loop_makeup_db(target_lufs) if best is None else best, 2)


def loudness_range(x: np.ndarray, sr: int = SR) -> float | None:
    """EBU Tech 3342 LRA in LU: short-term blocks, gated, 10th to 95th percentile. None if it's too short or silent."""
    lufs = -0.691 + 10 * np.log10(mixrender._block_power(x, sr, LRA_BLOCK_S, LRA_HOP_S) + 1e-12)
    above = lufs[lufs > LRA_ABSOLUTE_GATE]
    if above.size == 0:
        return None
    energy = (10 ** (above / 10)).mean()
    kept = above[above > 10 * math.log10(energy) + LRA_RELATIVE_GATE]
    return round(float(np.percentile(kept, 95) - np.percentile(kept, 10)), 2)


def measure(mix: np.ndarray, sr: int = SR) -> dict:
    """The numbers an export reports: integrated LUFS, LRA and true peak."""
    return {"integrated_lufs": round(mixrender.integrated_lufs(mix, sr), 2), "lra_lu": loudness_range(mix, sr),
            "true_peak_dbtp": round(mixrender.true_peak_db(mix), 2)}  # fmt: skip


def _clock(seconds: float) -> str:
    total = int(seconds)
    h, m, s = total // 3600, total // 60 % 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def tracklist(entries: list[tuple[float, str]], measured: dict, target_lufs: float) -> str:
    """The tracklist file: (start in seconds, name) per track, then the loudness measured on the render."""
    lra = measured["lra_lu"]
    lines = [f"{_clock(t)}  {name}" for t, name in entries]
    lines += ["", f"Target: {target_lufs:g} LUFS", f"Integrated: {measured['integrated_lufs']:.1f} LUFS",
              f"Loudness range: {'n/a' if lra is None else f'{lra:.1f} LU'}",
              f"True peak: {measured['true_peak_dbtp']:.1f} dBTP"]  # fmt: skip
    return "\n".join(lines) + "\n"


def target_name(settings: Any) -> str:
    """The target chosen in Settings (`master.target` in settings.json)."""
    chosen = (settings.get("master") or {}).get("target") if settings is not None else None
    return chosen if chosen in TARGETS else DEFAULT_TARGET


def status(settings: Any) -> dict:
    """`GET /api/mixes/master`: what the chain aims for, and the make-up the live engine starts from."""
    name = target_name(settings)
    return {"target": name, "target_lufs": TARGETS[name], "ceiling_dbtp": CEILING_DBTP,
            "makeup_db": round(open_loop_makeup_db(TARGETS[name]), 2), "targets": TARGETS}  # fmt: skip


def set_target(settings: Any, name: str) -> dict:
    settings.set("master", {**(settings.get("master") or {}), "target": name})
    return status(settings)


def from_settings(settings: Any, sr: int = SR) -> Master:
    return Master(TARGETS[target_name(settings)], sr=sr)
