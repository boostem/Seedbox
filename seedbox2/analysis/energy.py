"""Energy (#48): how intense each bar is, 0-1, and the whole track 1-10 in the style of Mixed In Key.

One decode gives this module and `seedbox2.analysis.loudness` everything, so the `energy` job stores both analyses.

Every bar of the beat grid gets five features, each 0-1 on a fixed scale, the same for every track:

- `loudness`: K-weighted loudness of the bar, -36 to -6 LUFS.
- `onsets`: spectral-flux onsets per second, 0 to ONSETS_FULL.
- `brightness`: spectral centroid, on a log scale from 250 Hz to 6 kHz.
- `drums`, `bass`: from the mix, how peaky the bar's level is for the drums and the power below 150 Hz for the bass. With
  cached stems, each stem's level relative to the mix decides how the bars differ, around the mix's value for the whole
  track. Measured on real tracks, the stems' raw levels sit on another scale (one track went from a 6 to a 9), and a
  rating mustn't depend on whether a track's stems happen to be cached. `source` says which (`stems` or `mix`); stems
  arriving later replace the analysis.

`score` is the features' weighted sum (`CALIBRATION["weights"]`), and `CALIBRATION["scale"]` maps it linearly to the
rating, so a bar's 0-1 and a track's 1-10 are one scale: `energy = (rating - 1) / 9`. A track's score uses each
feature's `0.7 x p75 + 0.3 x mean`, because what people rate is mostly a track's busy part, not its intro.
`python -m seedbox2.eval energy` fits the calibration: read the README before trusting its numbers.

The analysis: `{version, source, grid, rating: 1-10, energy: 0-1, score, bar_s: [n + 1 times], curve: [n], features:
{name: [n]}}`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import fft as sfft

from seedbox2.analysis import beats as beat_analysis
from seedbox2.analysis import loudness, stems, waveform
from seedbox2.analysis.grid import Grid
from seedbox2.jobs import register

log = logging.getLogger(__name__)

ANALYSER = "energy"
LOUDNESS_ANALYSER = "loudness"
VERSION = "1"
SAMPLE_RATE = loudness.SAMPLE_RATE
NFFT = 2048
HOP = 1024
FRAME_S = HOP / SAMPLE_RATE
LEVEL_HOP = SAMPLE_RATE // 10  # the mix's level, compared with the stems', every 100 ms
BASS_HZ = 150.0
FALLBACK_BAR_S = 2.0  # a track without a grid is measured in pieces about as long as a bar
BEATS_PER_BAR = 4
ONSETS_FULL = 8.0
FEATURES = ("loudness", "onsets", "brightness", "drums", "bass")
SILENT_LUFS = -60.0  # a bar this quiet has no energy, whatever its features say
STEM_RATE = 22_050
STEM_HOP = STEM_RATE // 10

# Expert prior for the weights, since no hand ratings exist yet. The scale comes from the real tracks in the README:
# the 5th percentile of their scores is a 3 and the 95th a 9.
CALIBRATION: dict[str, Any] = {
    "weights": {"loudness": 0.35, "onsets": 0.2, "brightness": 0.1, "drums": 0.2, "bass": 0.15},
    "scale": [16.534, -3.834],  # rating = scale[0] x score + scale[1]
    "ratings": 0,
}


class EnergyError(ValueError):
    pass


def _db(power: np.ndarray) -> np.ndarray:
    return 10 * np.log10(np.maximum(power, 1e-12))


def _unit(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


@dataclass
class Frames:
    power: np.ndarray  # per STFT frame
    bass: np.ndarray  # the part of `power` below BASS_HZ
    centroid: np.ndarray  # Hz
    flux: np.ndarray  # half-wave rectified log-spectrum difference
    level: np.ndarray  # the mono mix's power every 100 ms


class _Spectral:
    """The mono mix's frame features, streamed."""

    def __init__(self) -> None:
        self.window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(NFFT) / NFFT)).astype(np.float32)
        self.hz = np.fft.rfftfreq(NFFT, 1 / SAMPLE_RATE).astype(np.float32)
        self.bass_bins = int(np.searchsorted(self.hz, BASS_HZ))
        self.rest = np.zeros(0, np.float32)
        self.level_rest = np.zeros(0, np.float32)
        self.previous: np.ndarray | None = None
        self.parts: list[tuple[np.ndarray, ...]] = []
        self.levels: list[np.ndarray] = []

    def feed(self, mono: np.ndarray) -> None:
        joined = np.concatenate([self.level_rest, mono])
        n = joined.size // LEVEL_HOP
        if n:
            self.levels.append((joined[: n * LEVEL_HOP].reshape(n, LEVEL_HOP) ** 2).mean(axis=1))
        self.level_rest = joined[n * LEVEL_HOP :]
        x = np.concatenate([self.rest, mono])
        if x.size < NFFT:
            self.rest = x
            return
        frames = np.lib.stride_tricks.sliding_window_view(x, NFFT)[::HOP]
        self.rest = x[frames.shape[0] * HOP :]
        spec = sfft.rfft(frames * self.window, axis=1, workers=1)
        p = spec.real**2 + spec.imag**2
        total = p.sum(axis=1)
        centroid = (p * self.hz).sum(axis=1) / np.maximum(total, 1e-12)
        logmag = np.log1p(100 * np.sqrt(p))
        before = np.vstack([logmag[:1] if self.previous is None else self.previous[None], logmag[:-1]])
        flux = np.maximum(logmag - before, 0).mean(axis=1)
        self.previous = logmag[-1]
        self.parts.append((total, p[:, : self.bass_bins].sum(axis=1), centroid, flux))

    def result(self) -> Frames:
        if not self.parts:
            raise EnergyError("the track is too short to measure")
        total, bass, centroid, flux = (np.concatenate(c) for c in zip(*self.parts, strict=True))
        level = np.concatenate(self.levels) if self.levels else np.zeros(0)
        return Frames(total, bass, centroid, flux, level)


def measure(path: Path | str, progress: Callable[[float], None] | None = None,
            duration: float | None = None) -> tuple[Frames, dict]:  # fmt: skip
    """The mix's frames and the `loudness` analysis, in one streamed decode at 48 kHz stereo."""
    meter = loudness.Meter()
    spectral = _Spectral()
    seen = 0
    for chunk in waveform.decode_chunks(path, SAMPLE_RATE, sample_rate=SAMPLE_RATE, channels=2):
        stereo = chunk.reshape(-1, 2)
        meter.feed(stereo)
        spectral.feed(stereo.mean(axis=1))
        seen += stereo.shape[0]
        if progress and duration:
            progress(min(0.9, seen / SAMPLE_RATE / duration))
    return spectral.result(), meter.result()


def bar_times(beats: dict | None, duration: float) -> tuple[np.ndarray, str]:
    """The bars' start times with the end of the last one, covering the whole track, and where they came from."""
    grid = None
    if beats and beats.get("segments"):
        try:
            grid = Grid.from_json(beats["segments"])
        except (ValueError, KeyError, TypeError) as e:
            log.warning("unusable beat grid: %s", e)
    if grid is not None:
        first = int(np.ceil(float(grid.beat_at(0.0)) / BEATS_PER_BAR - 1e-9))
        last = int(np.ceil(float(grid.beat_at(duration)) / BEATS_PER_BAR - 1e-9)) - 1
        if last >= first:
            starts = np.asarray(grid.time_at(np.arange(first, last + 1) * BEATS_PER_BAR), np.float64).reshape(-1)
            starts = starts[(starts >= 0) & (starts < duration)]
            if starts.size:
                head = [0.0] if starts[0] > 0.05 else []
                return np.concatenate([head, starts, [duration]]), "beats"
    n = max(1, round(duration / FALLBACK_BAR_S))
    return np.linspace(0.0, duration, n + 1), "fixed"


def _per_bar(values: np.ndarray, hop_s: float, bars: np.ndarray, stat: Callable[[np.ndarray], float] = np.mean,
             offset_s: float = 0.0) -> np.ndarray:  # fmt: skip
    """`stat` of each bar's values; a bar too short to hold one takes the nearest."""
    out = np.zeros(bars.size - 1)
    if values.size == 0:
        return out
    idx = np.clip(np.round((bars - offset_s) / hop_s).astype(int), 0, values.size)
    for i in range(out.size):
        a, b = idx[i], max(idx[i + 1], idx[i] + 1)
        out[i] = stat(values[min(a, values.size - 1) : b])
    return out


def onset_rate(flux: np.ndarray, bars: np.ndarray) -> np.ndarray:
    """Onsets per second in each bar: local peaks of the flux above a moving threshold."""
    if flux.size < 3:
        return np.zeros(bars.size - 1)
    width = round(1.0 / FRAME_S)
    kernel = np.ones(width) / width
    threshold = np.convolve(flux, kernel, mode="same") * 1.3 + 0.002
    peak = (flux[1:-1] > flux[:-2]) & (flux[1:-1] >= flux[2:]) & (flux[1:-1] > threshold[1:-1])
    times = (np.flatnonzero(peak) + 1) * FRAME_S
    counts = np.histogram(times, bins=bars)[0]
    return counts / np.maximum(np.diff(bars), 1e-3)


def _stem_levels(file: Path, check: Callable[[], None] | None = None) -> np.ndarray:
    """One stem's mono power every 100 ms."""
    parts = []
    for chunk in waveform.decode_chunks(file, STEM_HOP * 50, sample_rate=STEM_RATE, channels=1):
        n = chunk.size // STEM_HOP
        if n:
            parts.append((chunk[: n * STEM_HOP].reshape(n, STEM_HOP) ** 2).mean(axis=1))
        if check:
            check()
    return np.concatenate(parts) if parts else np.zeros(0)


def bar_features(frames: Frames, curve: dict, bars: np.ndarray, stem_files: dict[str, Path] | None = None,
                 check: Callable[[], None] | None = None) -> dict[str, np.ndarray]:  # fmt: skip
    """Each feature for each bar, on the fixed scales in the module's docstring."""
    block_energy = loudness.lufs_to_energy(curve["lufs"]) if curve.get("lufs") else np.zeros(0)
    # a block is centred 200 ms after its index's time
    lufs = loudness.energy_to_lufs(_per_bar(block_energy, loudness.HOP_S, bars, offset_s=loudness.BLOCK_S / 2))
    power = frames.power
    centre_s = NFFT / 2 / SAMPLE_RATE
    centroid_w = _per_bar(frames.centroid * power, FRAME_S, bars, offset_s=centre_s)
    power_bar = _per_bar(power, FRAME_S, bars, offset_s=centre_s)
    centroid = centroid_w / np.maximum(power_bar, 1e-12)
    bass_ratio = _per_bar(frames.bass, FRAME_S, bars, offset_s=centre_s) / np.maximum(power_bar, 1e-12)
    out = {
        "loudness": _unit(lufs, -36.0, -6.0),
        "onsets": _unit(onset_rate(frames.flux, bars), 0.0, ONSETS_FULL),
        "brightness": _unit(np.log2(np.maximum(centroid, 1.0)), np.log2(250.0), np.log2(6000.0)),
    }
    peak = _per_bar(power, FRAME_S, bars, lambda v: float(np.percentile(v, 95)), offset_s=centre_s)
    median = _per_bar(power, FRAME_S, bars, np.median, offset_s=centre_s)
    out["drums"] = _unit(_db(peak) - _db(median), 3.0, 15.0)
    out["bass"] = _unit(_db(bass_ratio), -24.0, 0.0)
    if stem_files:
        level = _per_bar(frames.level, 0.1, bars)
        for name in ("drums", "bass"):
            presence = _unit(_db(_per_bar(_stem_levels(stem_files[name], check), 0.1, bars)) - _db(level), -24.0, 0.0)
            out[name] = np.clip(_busy(out[name]) + presence - _busy(presence), 0.0, 1.0)
    silent = lufs < SILENT_LUFS
    for name in FEATURES:
        out[name] = np.where(silent, 0.0, out[name])
    return out


def _busy(v: np.ndarray) -> float:
    """A feature's value for the whole track: its busy part counts most."""
    return 0.7 * float(np.percentile(v, 75)) + 0.3 * float(v.mean()) if v.size else 0.0


def track_features(features: dict[str, Iterable[float]]) -> dict[str, float]:
    """One value per feature for the whole track."""
    return {name: _busy(np.asarray(list(features[name]), np.float64)) for name in FEATURES}


def score(features: dict[str, Any], calibration: dict | None = None) -> Any:
    """The weighted sum, per bar or per track, depending on what `features` holds."""
    w = (calibration or CALIBRATION)["weights"]
    return sum(w[name] * np.asarray(features[name], np.float64) for name in FEATURES)


def to_rating(s: Any, calibration: dict | None = None) -> Any:
    """Score to the continuous 1-10 scale."""
    a, b = (calibration or CALIBRATION)["scale"]
    return np.clip(a * np.asarray(s, np.float64) + b, 1.0, 10.0)


def analyse(frames: Frames, curve: dict, beats: dict | None, stem_files: dict[str, Path] | None = None,
            check: Callable[[], None] | None = None, calibration: dict | None = None) -> dict:  # fmt: skip
    duration = frames.power.size * FRAME_S + (NFFT - HOP) / SAMPLE_RATE
    bars, grid = bar_times(beats, duration)
    features = bar_features(frames, curve, bars, stem_files, check)
    curve_01 = (to_rating(score(features, calibration), calibration) - 1) / 9
    silent = features["loudness"] <= 0
    curve_01 = np.where(silent, 0.0, curve_01)
    whole = track_features(features)
    s = float(score(whole, calibration))
    rating = float(to_rating(s, calibration))
    return {
        "version": VERSION, "source": "stems" if stem_files else "mix", "grid": grid,
        "rating": round(rating), "energy": round((rating - 1) / 9, 3), "score": round(s, 4),
        "bar_s": np.round(bars, 3).tolist(), "curve": np.round(curve_01, 3).tolist(),
        "features": {k: np.round(v, 3).tolist() for k, v in features.items()},
        "track_features": {k: round(v, 4) for k, v in whole.items()},
    }  # fmt: skip


@register("energy", pool="light")
def energy_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"hash": ...}`: stores the track's `energy` and `loudness` analyses."""
    hash_ = payload["hash"]
    lib = ctx.library
    track = lib.track(hash_)
    if track is None:
        raise EnergyError(f"no track {hash_}")
    sources = [Path(p) for p in lib.paths(hash_) if Path(p).is_file()]
    if not sources:
        raise EnergyError("none of the track's files is there any more")
    row = lib.analysis(hash_, beat_analysis.ANALYSER, beat_analysis.VERSION)
    folder = stems.current(lib, hash_)
    stem_files = waveform.stem_files(folder) if folder is not None else None
    frames, curve = measure(sources[0], ctx.progress, track.get("duration"))
    data = analyse(frames, curve, row["data"] if row else None, stem_files, ctx.check)
    lib.put_analysis(hash_, LOUDNESS_ANALYSER, VERSION, curve)
    lib.put_analysis(hash_, ANALYSER, VERSION, data)
    ctx.progress(1.0)
    return {"hash": hash_, "rating": data["rating"], "source": data["source"], "grid": data["grid"],
            "integrated_lufs": curve["integrated_lufs"], "true_peak_dbtp": curve["true_peak_dbtp"]}  # fmt: skip


def submit(jobs: Any, hash_: str, *, priority: int | str = "library", front: bool = False) -> dict:
    return jobs.submit("energy", {"hash": hash_}, priority=priority, front=front)


def on_job_finished(jobs: Any, lib: Any, row: dict) -> None:
    """The supervisor's listener: energy runs once the beats have finished, whether they worked or not, and again when
    a grid or stems arrive that the analysis was made without."""
    payload = row.get("payload")
    kind = row.get("kind")
    if kind not in ("beats", "stems") or not isinstance(payload, dict) or not payload.get("hash"):
        return
    hash_ = payload["hash"]
    if row.get("state") not in ("done", "failed"):
        return
    existing = lib.analysis(hash_, ANALYSER, VERSION)
    if existing is None:
        if kind == "beats":
            submit(jobs, hash_)
        return
    if row["state"] != "done":
        return
    data = existing["data"]
    grid_arrived = kind == "beats" and data.get("grid") != "beats"
    stems_arrived = kind == "stems" and data.get("source") != "stems" and stems.current(lib, hash_) is not None
    if grid_arrived or stems_arrived:
        submit(jobs, hash_, priority="background")


def queue_missing(jobs: Any, lib: Any) -> int:
    """The tracks whose beats were analysed before this analysis existed; the others get it when their beats finish.
    Returns the number queued."""
    queued = 0
    for hash_ in lib.hashes_without_analysis(ANALYSER, VERSION):
        if lib.analysis(hash_, beat_analysis.ANALYSER) is not None:
            submit(jobs, hash_, priority="background")
            queued += 1
    return queued
