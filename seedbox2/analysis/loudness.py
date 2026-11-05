"""Loudness (#48): ITU-R BS.1770-4 in numpy, streamed, so the whole track never has to be in RAM.

`Meter` takes 48 kHz stereo and keeps two small curves: K-weighted loudness in 400 ms blocks every 100 ms (the `lufs`
curve: the blocks BS.1770's gating uses and EBU R128's momentary loudness) and the true peak per second (4x
oversampled, as ffmpeg's `ebur128=peak=true`). Everything else comes from those curves, for any stretch of the track:
`stretch` for a section, `sections` for all of them and `overlap_gain_db` for the gain that matches two tracks where
they overlap. Sections can therefore change later without touching the audio again.

Checked against ffmpeg's ebur128 on real tracks (`tests/test_seedbox2_energy.py`, README).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

SAMPLE_RATE = 48_000
HOP_S = 0.1
BLOCK_S = 0.4
BLOCK_HOPS = 4
HOP = int(SAMPLE_RATE * HOP_S)
PEAK_HOP = SAMPLE_RATE  # a true peak per second
OVERSAMPLE = 4
PEAK_CONTEXT = 32  # samples of the neighbouring seconds, for the interpolation across the boundary
ABSOLUTE_GATE = -70.0
RELATIVE_GATE = -10.0
OFFSET = -0.691
SILENCE = -120.0  # what a silent block's loudness is stored as
TRACK_GAIN_LIMIT_DB = 12.0
BODY_SHARE = 0.25  # a track's body: its loudest sections, as many as cover this share of what's audible
BODY_WINDOW_S, BODY_HOP_S = 16.0, 8.0  # the stretches that stand in for sections when a track has none
MAX_MATCH_DB = 12.0  # a gain beyond this means one side is a breakdown, not that the tracks differ by that much

# BS.1770's two K-weighting biquads at 48 kHz
_SOS = np.array([[1.53512485958697, -2.69169618940638, 1.19839281085285, 1.0, -1.69065929318241, 0.73248077421585],
                 [1.0, -2.0, 1.0, 1.0, -1.99004745483398, 0.99007225036621]])  # fmt: skip


def energy_to_lufs(e: np.ndarray | float) -> np.ndarray:
    return np.where(np.asarray(e) > 0, OFFSET + 10 * np.log10(np.maximum(e, 1e-30)), SILENCE)


def lufs_to_energy(lufs: np.ndarray | Sequence[float]) -> np.ndarray:
    x = np.asarray(lufs, np.float64)
    return np.where(x <= SILENCE, 0.0, 10 ** ((x - OFFSET) / 10))


def gated(block_energy: np.ndarray) -> float | None:
    """BS.1770's integrated loudness of these blocks' energies; None if nothing is above the absolute gate."""
    e = np.asarray(block_energy, np.float64)
    above = e[e > 10 ** ((ABSOLUTE_GATE - OFFSET) / 10)]
    if above.size == 0:
        return None
    threshold = above.mean() * 10 ** (RELATIVE_GATE / 10)
    kept = above[above > threshold]
    return float(energy_to_lufs(kept.mean())) if kept.size else None


@dataclass
class Meter:
    """Feed float32 [frames, 2] at 48 kHz in chunks of any size, then `result()`."""

    _zi: np.ndarray = field(default_factory=lambda: np.zeros((2, 2, 2)))
    _hops: list[float] = field(default_factory=list)  # the K-weighted energy of each finished 100 ms
    _rest: np.ndarray = field(default_factory=lambda: np.zeros(0))
    _peaks: list[float] = field(default_factory=list)
    _peak_rest: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), np.float32))
    _context: np.ndarray = field(default_factory=lambda: np.zeros((PEAK_CONTEXT, 2), np.float32))

    def feed(self, x: np.ndarray) -> None:
        if x.size == 0:
            return
        weighted, self._zi = signal.sosfilt(_SOS, x, axis=0, zi=self._zi)
        power = np.concatenate([self._rest, (weighted.astype(np.float64) ** 2).sum(axis=1)])
        n = power.size // HOP
        if n:
            self._hops.extend(power[: n * HOP].reshape(n, HOP).mean(axis=1).tolist())
        self._rest = power[n * HOP :]
        self._peak_rest = np.concatenate([self._peak_rest, x]) if self._peak_rest.size else x
        while self._peak_rest.shape[0] >= PEAK_HOP + PEAK_CONTEXT:
            self._peak(self._peak_rest[:PEAK_HOP], self._peak_rest[PEAK_HOP : PEAK_HOP + PEAK_CONTEXT])
            self._peak_rest = self._peak_rest[PEAK_HOP:]

    def _peak(self, second: np.ndarray, after: np.ndarray) -> None:
        """The second's peak, interpolated with real samples on both sides, so its edges don't ring."""
        joined = np.concatenate([self._context, second, after])
        up = signal.resample_poly(joined, OVERSAMPLE, 1, axis=0)
        inner = up[PEAK_CONTEXT * OVERSAMPLE : (PEAK_CONTEXT + second.shape[0]) * OVERSAMPLE]
        self._peaks.append(float(np.abs(inner).max()) if inner.size else 0.0)
        self._context = np.concatenate([self._context, second])[-PEAK_CONTEXT:]

    def result(self) -> dict:
        while self._peak_rest.shape[0]:
            self._peak(self._peak_rest[:PEAK_HOP], self._peak_rest[PEAK_HOP : PEAK_HOP + PEAK_CONTEXT])
            self._peak_rest = self._peak_rest[PEAK_HOP:]
        hops = np.asarray(self._hops, np.float64)
        if hops.size >= BLOCK_HOPS:
            blocks = np.convolve(hops, np.ones(BLOCK_HOPS) / BLOCK_HOPS, mode="valid")
        else:
            blocks = np.zeros(0)
        peaks = np.asarray(self._peaks, np.float64)
        return summary(blocks, peaks)


def summary(blocks: np.ndarray, peaks: np.ndarray) -> dict:
    """The stored analysis: `lufs[i]` is the block from i x 0.1 s to i x 0.1 s + 0.4 s, `peak_dbtp[i]` is second i."""
    lufs = energy_to_lufs(blocks) if blocks.size else np.zeros(0)
    peak_db = 20 * np.log10(np.maximum(peaks, 1e-6)) if peaks.size else np.zeros(0)
    integrated = gated(blocks)
    return {
        "hop_s": HOP_S, "block_s": BLOCK_S,
        "integrated_lufs": None if integrated is None else round(integrated, 2),
        "true_peak_dbtp": round(float(peak_db.max()), 2) if peak_db.size else None,
        "lufs": np.round(lufs, 1).tolist(),
        "peak_dbtp": np.round(peak_db, 1).tolist(),
    }  # fmt: skip


def stretch(data: dict, start_s: float, end_s: float) -> dict:
    """Integrated loudness, gated as BS.1770, and true peak between two times: None where there's nothing to measure."""
    curve, peaks = data.get("lufs") or [], data.get("peak_dbtp") or []
    i0 = max(0, round(start_s / HOP_S))
    i1 = max(i0, round((end_s - BLOCK_S) / HOP_S) + 1)  # the blocks entirely inside; at least one
    window = np.asarray(curve[i0 : max(i1, i0 + 1)], np.float64)
    p0, p1 = max(0, math.floor(start_s)), math.ceil(end_s)
    seconds = peaks[p0:max(p1, p0 + 1)]
    integrated = gated(lufs_to_energy(window)) if window.size else None
    return {"start_s": round(float(start_s), 3), "end_s": round(float(end_s), 3),
            "integrated_lufs": None if integrated is None else round(integrated, 2),
            "true_peak_dbtp": round(float(max(seconds)), 1) if seconds else None}  # fmt: skip


def sections(data: dict, spans: Iterable[dict]) -> list[dict]:
    """`stretch` for each `{kind, start_s, end_s}`."""
    return [{"kind": s.get("kind"), **stretch(data, float(s["start_s"]), float(s["end_s"]))} for s in spans]


def track_gain_db(data: dict, spans: Iterable[dict] | None = None, target_lufs: float = -12.0) -> float | None:
    """The one gain for a whole track (#58): its body at `target_lufs`, measured per section rather than where a blend
    happens to be. A quiet intro stays as much quieter than the drop as the track made it, so it isn't boosted into the
    next track's drop. Without sections, 16 s stretches. Within ±TRACK_GAIN_LIMIT_DB; None if the track is silent."""
    length = len(data.get("lufs") or []) * HOP_S + BLOCK_S - HOP_S
    stretches = list(spans) if spans else []
    if not stretches and length > 0:
        starts = np.arange(0.0, max(length - BODY_WINDOW_S, 0.0) + BODY_HOP_S / 2, BODY_HOP_S)
        stretches = [{"start_s": float(t), "end_s": min(float(t) + BODY_WINDOW_S, length)} for t in starts]
    found = [(m["integrated_lufs"], m["end_s"] - m["start_s"]) for m in sections(data, stretches)
             if m["integrated_lufs"] is not None]  # fmt: skip
    if not found:
        return None
    found.sort(key=lambda f: -f[0])
    need = BODY_SHARE * sum(seconds for _, seconds in found)
    energy, covered = 0.0, 0.0
    for lufs, seconds in found:
        energy += float(lufs_to_energy([lufs])[0]) * seconds
        covered += seconds
        if covered >= need:
            break
    body = float(energy_to_lufs(energy / covered))
    return round(float(np.clip(target_lufs - body, -TRACK_GAIN_LIMIT_DB, TRACK_GAIN_LIMIT_DB)), 2)


def overlap_gain_db(outgoing: dict, out_span: tuple[float, float], incoming: dict,
                    in_span: tuple[float, float]) -> float | None:  # fmt: skip
    """The gain for the incoming track that matches it to the outgoing one over the overlap: None if either is silent
    there. Limited to ±MAX_MATCH_DB, and to what the incoming track's true peak leaves room for below 0 dBTP."""
    a, b = stretch(outgoing, *out_span), stretch(incoming, *in_span)
    if a["integrated_lufs"] is None or b["integrated_lufs"] is None:
        return None
    gain = float(np.clip(a["integrated_lufs"] - b["integrated_lufs"], -MAX_MATCH_DB, MAX_MATCH_DB))
    if b["true_peak_dbtp"] is not None:
        gain = min(gain, -b["true_peak_dbtp"])
    return round(gain, 2)
