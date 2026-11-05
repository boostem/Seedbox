"""Phrases and sections from the stems (#46): what DJs mix on.

1. **Bar features** (`bar_features`): the four cached stems are streamed from ffmpeg at 22,050 Hz. Each bar of the
   beat_this grid gets every stem's level and onset density, and the mix's chroma and MFCC (without separating
   anything again: the mix is the sum of the stems' spectra). Each beat gets the drums' strength per sixteenth.
2. **Boundaries** (`novelty`, `phrase_offset`, `segment`): novelty is how much the stems change (in or out) plus a
   Foote checkerboard on a banded self-similarity matrix of chroma and MFCC. Bars on the 8/16/32-bar phrase grid are
   the candidates, and a DP over segments of at most MAX_SEGMENT bars chooses among them. Memory is O(n): prefix sums
   and two arrays, not Seedbox 1's (n+1)² x 7 tables.
3. **Labels** (`label`): intro, build, drop, breakdown, body and outro from which stems play.
4. **Mood** (`mood`): high, mid or low for the whole track, in the spirit of Rekordbox's phrase analysis.
5. **Fill-ins** (`fills`): the last 1-4 beats of a phrase where the drums play more than their groove.
6. **Mix-in and mix-out candidates** (`mix_candidates`): scored places to start and to leave a track, whichever way
   its edges are arranged. They replace Seedbox 1's intro and outro edges (`musicreco/structure.py`).

The result (`analyse`, stored as the track's `sections` analysis):

    {"version", "source": "stems"|"seedbox1", "duration_s", "first_bar_beat", "bar_count", "phrase_offset", "mood",
     "mood_score", "sections": [{kind, start_bar, end_bar, start_s, end_s, novelty}],
     "phrases": [{bar, start_s, length: 8|16|32, novelty}], "fills": [{bar, start_beat, start_s, beats, strength}],
     "mix_in": [{bar, start_s, bars, score, section, vocals, bass, drums}], "mix_out": [the same],
     "activity": {"drums": [0-1 per bar], "bass", "other", "vocals"}}

A bar is an index into the track's bars; bar `i` starts at beat `first_bar_beat + 4 * i` of the grid (the first bar can
be before beat 0, when music starts before the first detected beat). Without stems (model missing, or the cache is
full) the job gives Seedbox 1's sections and edges in the same shape, `"source": "seedbox1"`, with no activity or fills;
stems arriving later replace them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import fft as sfft
from scipy import ndimage

from seedbox2.analysis import beats as beat_analysis
from seedbox2.analysis import stems as stem_cache
from seedbox2.analysis import waveform
from seedbox2.analysis.grid import Grid
from seedbox2.jobs import register

log = logging.getLogger(__name__)

ANALYSER = "sections"
VERSION = "1"

SAMPLE_RATE = 22_050
NFFT = 2048
HOP = 512
CHUNK_FRAMES = 512  # frames per decoded chunk: 12 s of audio in memory at a time
BEATS_PER_BAR = 4
STEMS = ("drums", "bass", "other", "vocals")

# levels: a stem plays in a bar when it is within ON_DB of its own loud level, and not ON_FLOOR_DB below the mix
ON_DB = 12.0
ON_FLOOR_DB = 30.0
LEVEL_RANGE_DB = 36.0

# segmentation: costs are in the units of the feature matrix (6 dB of a stem = 1)
LAMBDA = 4.0  # the cost of a boundary on the phrase grid, before its novelty
GRID_WEIGHT = 25.0  # ... and how much more it costs for each unit of GRID_COST off the grid
NOVELTY_GAIN = 5.0  # what a boundary of typical high novelty earns back
MIN_SEGMENT = 4
MAX_SEGMENT = 128  # the DP's band; a longer unchanged stretch gets split, and its parts keep one label
CHROMA_WEIGHT = 0.8
MFCC_WEIGHT = 0.5
GRID_COST = ((32, 0.0), (16, 0.1), (8, 0.2), (4, 0.5), (2, 0.9), (1, 1.5))  # the coarsest multiple a bar is on -> cost
LOCAL_DISCOUNT = 0.7  # a whole phrase since the last boundary counts almost as much as the global grid
EARLY_BARS = 32.0  # bars early in the track weigh more when choosing the phrase offset: DJs mix there

# labels
MAIN_MARGIN = 0.8  # stems' worth of fullness under the main sections that makes a leading/trailing part an edge
EDGE_MAX_BARS = 64
VOCAL_EDGE_MAX_BARS = 16  # a full part without the vocals is an edge only up to here
BUILD_MAX_BARS = 16
MERGED_KINDS = ("intro", "outro", "breakdown", "build")
RISE_DB_PER_BAR = 0.25
RISE_TOTAL_DB = 3.0
SNARE_RISE = 0.75  # onsets per beat from the first to the last quarter of a build

# fills
FILL_EXCESS = 0.5  # strong sixteenths per beat above the groove
FILL_MIN_ONSETS = 2.5  # ... or this many in a beat when the groove is silent

_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(NFFT) / NFFT)).astype(np.float32)


class SectionsError(ValueError):
    """Nothing to analyse: too few bars, or the stems can't be read."""


@dataclass
class BarFeatures:
    """Everything the decisions below need, per bar (and for the drums per beat). Small enough to cache."""

    bar_s: np.ndarray  # bar start times, n + 1 (the last one is the end of the last bar)
    first_bar_beat: int
    bpm: float
    db: np.ndarray  # (n, 4) each stem's level, dB relative to the mix's loud level
    onsets: np.ndarray  # (n, 4) each stem's onsets per beat
    mix_db: np.ndarray  # (n,) the mix's level on the same scale
    high_db: np.ndarray  # (n,) the mix above 5 kHz
    kick: np.ndarray  # (n,) drum onsets per beat below 150 Hz
    snare: np.ndarray  # (n,) drum onsets per beat above 1.5 kHz
    chroma: np.ndarray  # (n, 12) unit vectors
    mfcc: np.ndarray  # (n, 13) coefficients 1-13
    drum_grid: np.ndarray  # (beats, 4) the drums' strength per sixteenth, 0-1

    @property
    def n(self) -> int:
        return self.db.shape[0]

    def on(self) -> np.ndarray:
        """(n, 4) whether each stem plays in each bar."""
        ref = np.percentile(self.db, 90, axis=0)
        return self.db >= np.maximum(ref - ON_DB, -ON_FLOOR_DB)

    def save(self, path: Path | str) -> None:
        np.savez_compressed(path, **{k: np.asarray(getattr(self, k)) for k in self.__dataclass_fields__})

    @classmethod
    def load(cls, path: Path | str) -> BarFeatures:
        with np.load(path) as z:
            d = {k: z[k] for k in z.files}
        return cls(**{**d, "first_bar_beat": int(d["first_bar_beat"]), "bpm": float(d["bpm"])})


# --- from the stems to frames ----------------------------------------------------------------------------------------


def _band(lo: float, hi: float) -> slice:
    return slice(int(round(lo * NFFT / SAMPLE_RATE)), int(round(hi * NFFT / SAMPLE_RATE)) + 1)


def _mel_filters(bands: int = 40) -> np.ndarray:
    """(bands, bins) triangular mel filters up to 8 kHz."""
    mel = lambda f: 2595.0 * np.log10(1.0 + f / 700.0)  # noqa: E731
    pts = 700.0 * (10.0 ** (np.linspace(mel(30.0), mel(8000.0), bands + 2) / 2595.0) - 1.0)
    freqs = np.arange(NFFT // 2 + 1) * SAMPLE_RATE / NFFT
    out = np.zeros((bands, freqs.size), np.float32)
    for i in range(bands):
        a, b, c = pts[i:i + 3]
        out[i] = np.clip(np.minimum((freqs - a) / (b - a), (c - freqs) / (c - b)), 0.0, None)
    return out


def _chroma_filters() -> np.ndarray:
    """(12, bins): each bin between 65 Hz and 2.1 kHz to its pitch class (0 = A)."""
    freqs = np.arange(NFFT // 2 + 1) * SAMPLE_RATE / NFFT
    out = np.zeros((12, freqs.size), np.float32)
    use = (freqs >= 65.0) & (freqs <= 2100.0)
    out[np.round(12 * np.log2(freqs[use] / 440.0)).astype(int) % 12, np.flatnonzero(use)] = 1.0
    return out


def _dct(bands: int = 40, coefficients: int = 13) -> np.ndarray:
    """(coefficients, bands) DCT-II, coefficients 1-13 (the 0th is the level, which the stems already say)."""
    k = np.arange(1, coefficients + 1)[:, None]
    return np.cos(np.pi * k * (np.arange(bands) + 0.5) / bands).astype(np.float32) * np.sqrt(2.0 / bands)


def _zip_stems(files: dict[str, Path]) -> Iterator[np.ndarray]:
    """(4, samples) chunks. The stems were written together, so they have the same length."""
    decoders = [waveform.decode_chunks(files[s], CHUNK_FRAMES * HOP, sample_rate=SAMPLE_RATE) for s in STEMS]
    try:
        for parts in zip(*decoders):
            m = min(p.size for p in parts)
            yield np.stack([p[:m] for p in parts])
    finally:
        for d in decoders:
            d.close()


@dataclass
class _Frames:
    power: np.ndarray  # (frames, 4)
    flux: np.ndarray  # (frames, 4)
    kick_flux: np.ndarray
    snare_flux: np.ndarray
    mix_power: np.ndarray
    high_power: np.ndarray
    chroma: np.ndarray  # (frames, 12)
    mfcc: np.ndarray  # (frames, 13)


def stem_frames(files: dict[str, Path], check: Callable[[int], None] | None = None) -> _Frames:
    """Frame features of the stems, streamed: at most a chunk of audio is held, and O(frames) small arrays."""
    mel, chroma_f, dct = _mel_filters(), _chroma_filters(), _dct()
    high, kick_band, snare_band = _band(5000, SAMPLE_RATE / 2), _band(20, 150), _band(1500, 8000)
    tail = np.zeros((4, NFFT - HOP), np.float32)
    last: np.ndarray | None = None
    out: dict[str, list[np.ndarray]] = {k: [] for k in _Frames.__dataclass_fields__}
    for chunk in _zip_stems(files):
        x = np.concatenate([tail, chunk], axis=1)
        tail = x[:, x.shape[1] - (NFFT - HOP):]
        frames = (x.shape[1] - NFFT) // HOP + 1
        if frames <= 0:
            continue
        idx = np.arange(NFFT)[None, :] + HOP * np.arange(frames)[:, None]
        spec = sfft.rfft(x[:, idx] * _WINDOW, axis=-1, workers=1)  # (4, frames, bins)
        mag = np.abs(spec) / (NFFT / 4)
        pw = (mag * mag).astype(np.float32)
        log_mag = np.log1p(10.0 * mag).astype(np.float32)
        prev = np.concatenate([(log_mag[:, :1] if last is None else last), log_mag[:, :-1]], axis=1)
        last = log_mag[:, -1:]
        rise = np.maximum(log_mag - prev, 0.0)
        out["power"].append(pw.mean(axis=2).T)
        out["flux"].append(rise.sum(axis=2).T)
        out["kick_flux"].append(rise[0, :, kick_band].sum(axis=1))
        out["snare_flux"].append(rise[0, :, snare_band].sum(axis=1))
        mix = spec.sum(axis=0)
        mix_pw = (np.abs(mix) / (NFFT / 4)) ** 2
        tonal = spec[1:].sum(axis=0)  # bass, other, vocals: drums would only smear the chroma
        tonal_pw = (np.abs(tonal) / (NFFT / 4)) ** 2
        out["mix_power"].append(mix_pw.mean(axis=1).astype(np.float32))
        out["high_power"].append(mix_pw[:, high].mean(axis=1).astype(np.float32))
        out["chroma"].append((tonal_pw @ chroma_f.T).astype(np.float32))
        out["mfcc"].append((np.log10(mix_pw @ mel.T + 1e-8) @ dct.T).astype(np.float32))
        if check is not None:
            check(frames * HOP)
    if not out["power"]:
        raise SectionsError("the stems are empty")
    return _Frames(**{k: np.concatenate(v) for k, v in out.items()})


def _peaks(flux: np.ndarray, gate: np.ndarray) -> np.ndarray:
    """Onsets: local maxima (±2 frames) well above the signal's usual attack, where the stem is audible at all."""
    if flux.size == 0:
        return np.zeros(0, bool)
    thr = 0.3 * float(np.percentile(flux, 97)) + 1e-6
    return (flux >= ndimage.maximum_filter1d(flux, 5, mode="nearest")) & (flux > thr) & gate


def _db(power: np.ndarray) -> np.ndarray:
    return 10.0 * np.log10(power + 1e-10)


def bars_of(grid: Grid, duration_s: float) -> tuple[np.ndarray, int]:
    """Bar start times over the track, with the end of the last bar, and the beat number of bar 0."""
    first = int(np.ceil(float(grid.beat_at(0.0)) / BEATS_PER_BAR - 1e-9))
    last = int(np.ceil(float(grid.beat_at(duration_s)) / BEATS_PER_BAR - 1e-9)) - 1  # the last bar starting before the end
    numbers = np.arange(first, last + 1) * BEATS_PER_BAR
    starts = np.asarray(grid.time_at(numbers), np.float64).reshape(-1)
    return np.append(starts, duration_s), first * BEATS_PER_BAR


def features_from_frames(f: _Frames, grid: Grid, bpm: float) -> BarFeatures:
    """Aggregate frames on the grid."""
    frames = f.power.shape[0]
    duration = (frames - 1) * HOP / SAMPLE_RATE + NFFT / SAMPLE_RATE
    bar_s, first_beat = bars_of(grid, duration)
    n = bar_s.size - 1
    if n < 1:
        raise SectionsError("the track is shorter than a bar")
    t = (np.arange(frames) * HOP + NFFT / 2) / SAMPLE_RATE
    bar = np.searchsorted(bar_s, t, side="right") - 1
    keep = (bar >= 0) & (bar < n)
    idx = bar[keep]
    count = np.maximum(np.bincount(idx, minlength=n), 1)

    def mean(a: np.ndarray) -> np.ndarray:
        if a.ndim == 1:
            return np.bincount(idx, a[keep], minlength=n) / count
        return np.column_stack([np.bincount(idx, a[keep, i], minlength=n) / count for i in range(a.shape[1])])

    mix_db = _db(mean(f.mix_power))
    ref = float(np.percentile(mix_db, 95))
    gate = f.power >= 10.0 ** ((ref - LEVEL_RANGE_DB - 9.0) / 10.0)  # mean power is well under the peak of a hit

    def per_beat(fl: np.ndarray, g: np.ndarray) -> np.ndarray:
        return np.bincount(idx, _peaks(fl, g)[keep].astype(float), minlength=n) / BEATS_PER_BAR

    onsets = np.column_stack([per_beat(f.flux[:, i], gate[:, i]) for i in range(4)])
    chroma = mean(f.chroma)
    chroma = chroma / np.maximum(np.linalg.norm(chroma, axis=1, keepdims=True), 1e-9)
    # drums per sixteenth, for the fills
    beat_no = np.asarray(grid.beat_at(t), np.float64) - first_beat
    six = np.floor(beat_no * 4).astype(np.int64)
    total = (n * BEATS_PER_BAR) * 4
    ok = (six >= 0) & (six < total)
    scale = float(np.percentile(f.flux[:, 0], 97)) + 1e-6
    strength = np.zeros(total, np.float32)
    np.maximum.at(strength, six[ok], np.clip(f.flux[ok, 0] * gate[ok, 0] / scale, 0.0, 1.0).astype(np.float32))
    return BarFeatures(
        bar_s=bar_s, first_bar_beat=first_beat, bpm=bpm, db=_db(mean(f.power)) - ref, onsets=onsets, mix_db=mix_db - ref,
        high_db=_db(mean(f.high_power)) - ref, kick=per_beat(f.kick_flux, gate[:, 0]),
        snare=per_beat(f.snare_flux, gate[:, 0]), chroma=chroma, mfcc=mean(f.mfcc),
        drum_grid=strength.reshape(-1, 4),
    )  # fmt: skip


def bar_features(files: dict[str, Path], beats_result: dict, check: Callable[[int], None] | None = None) -> BarFeatures:
    grid = Grid(beats_result["segments"])
    return features_from_frames(stem_frames(files, check), grid, float(beats_result["bpm"]))


# --- boundaries ------------------------------------------------------------------------------------------------------


def _matrix(b: BarFeatures) -> np.ndarray:
    """The features the segments are constant in: levels (6 dB = 1), drum density, chroma, whitened MFCC."""
    ref = np.percentile(b.db, 95, axis=0)
    levels = np.maximum(b.db, ref - LEVEL_RANGE_DB) / 6.0
    sd = np.maximum(b.mfcc.std(axis=0), 1e-6)
    mfcc = (b.mfcc - b.mfcc.mean(axis=0)) / sd
    return np.column_stack([
        levels * np.array([1.0, 1.0, 0.7, 1.0]), b.kick[:, None] / 0.5, b.snare[:, None] / 1.0,
        CHROMA_WEIGHT * 2.0 * b.chroma, MFCC_WEIGHT * mfcc[:, :8] / np.sqrt(8.0) * 2.0,
    ])  # fmt: skip


def foote(x: np.ndarray, width: int) -> np.ndarray:
    """Checkerboard novelty on the band of the self-similarity matrix within `width` bars of the diagonal."""
    n = x.shape[0]
    u = x - x.mean(axis=0)
    u = u / np.maximum(np.linalg.norm(u, axis=1, keepdims=True), 1e-9)
    out = np.zeros(n + 1)
    for b in range(1, n):
        lo, hi = max(0, b - width), min(n, b + width)
        before, after = u[lo:b], u[b:hi]
        cross = float((before @ after.T).mean())
        within = 0.5 * (float((before @ before.T).mean()) + float((after @ after.T).mean()))
        out[b] = max(0.0, within - cross)
    return out


def stem_change(b: BarFeatures, width: int = 4) -> np.ndarray:
    """Per bar line, how much the stems changed: a stem coming in or going out counts most, in units of about 6 dB."""
    ref = np.percentile(b.db, 95, axis=0)
    lv = np.maximum(b.db, ref - LEVEL_RANGE_DB)
    n = lv.shape[0]
    c = np.concatenate([np.zeros((1, 4)), np.cumsum(lv, axis=0)])
    out = np.zeros(n + 1)
    idx = np.arange(1, n)
    for w in (width // 2, width):
        lo, hi = np.maximum(idx - w, 0), np.minimum(idx + w, n)
        before = (c[idx] - c[lo]) / (idx - lo)[:, None]
        after = (c[hi] - c[idx]) / (hi - idx)[:, None]
        change = np.abs(after - before) / 6.0
        out[idx] = np.maximum(out[idx], (np.minimum(change, 3.0) * np.array([1.0, 1.0, 0.6, 1.0])).sum(axis=1))
    return out


def novelty(b: BarFeatures) -> np.ndarray:
    """Per bar line (n + 1), 0 to about 2: 1 is a typical strong change for this track."""
    sd = np.maximum(b.mfcc.std(axis=0), 1e-6)
    timbre = (b.mfcc - b.mfcc.mean(axis=0)) / sd / np.sqrt(b.mfcc.shape[1])
    x = np.column_stack([b.chroma, timbre])
    ssm = 0.5 * (foote(x, 4) + foote(x, 8))
    def unit(a: np.ndarray) -> np.ndarray:
        return a / max(float(np.percentile(a, 90)), 1e-6)
    return np.minimum(0.5 * unit(ssm) + 0.5 * unit(stem_change(b)), 2.0)


def phrase_offset(strength: np.ndarray, period: int = 8) -> int:
    """The bar (mod `period`) where the track's 8-bar phrases start: where its strong boundaries pile up, mostly early
    on. `strength` is per bar line, 0 for bars that aren't a boundary."""
    bars = np.arange(strength.size)
    w = strength * (1.0 + 3.0 * np.exp(-bars / EARLY_BARS))
    score = np.bincount(bars % period, w, minlength=period)
    score += 0.3 * np.roll(score, period // 2)  # a half-phrase hit also says something
    score[0] *= 1.05  # ties: the grid's own bar 0
    return int(np.argmax(score))


def boundary_strength(g: np.ndarray, bounds: list[int]) -> np.ndarray:
    """How far the features jump at each boundary, comparing the 4 bars on each side."""
    n = g.shape[0]
    c = np.concatenate([np.zeros((1, g.shape[1])), np.cumsum(g, axis=0)])
    out = np.zeros(n + 1)
    for b in bounds[1:-1]:
        lo, hi = max(0, b - 4), min(n, b + 4)
        out[b] = float(np.linalg.norm((c[hi] - c[b]) / (hi - b) - (c[b] - c[lo]) / (b - lo)))
    return out


def grid_cost(bars: np.ndarray) -> np.ndarray:
    """0 for a multiple of 32 up to 1.5 for an odd number (`GRID_COST`)."""
    out = np.full(bars.shape, GRID_COST[-1][1], float)
    for multiple, cost in GRID_COST[::-1]:
        out[bars % multiple == 0] = cost
    return out


def segment(g: np.ndarray, nov: np.ndarray, offset: int | None) -> list[int]:
    """Boundaries [0, ..., n] minimising within-segment squared error + a cost per boundary, which is lower on the
    phrase grid (global, or a whole number of phrases from the last boundary) and where novelty is high. Without an
    `offset` there is no global grid yet, only the distance from the last boundary.

    A banded DP: O(n x MAX_SEGMENT) time, O(n) memory.
    """
    n = g.shape[0]
    if n <= MIN_SEGMENT:
        return [0, n]
    c1 = np.concatenate([np.zeros((1, g.shape[1])), np.cumsum(g, axis=0)])
    c2 = np.concatenate([[0.0], np.cumsum(np.sum(g * g, axis=1))])
    bars = np.arange(n + 1)
    early = 1.0 + 1.5 * np.exp(-bars / EARLY_BARS)
    glob = np.full(n + 1, np.inf) if offset is None else grid_cost(bars - offset) * early
    local = LOCAL_DISCOUNT * grid_cost(np.arange(MAX_SEGMENT + 1))
    local[0] = np.inf
    best = np.full(n + 1, np.inf)
    best[0] = 0.0
    prev = np.zeros(n + 1, np.int32)
    reward = NOVELTY_GAIN * nov
    for j in range(1, n + 1):
        i = np.arange(max(0, j - MAX_SEGMENT), j)
        length = j - i
        sse = (c2[j] - c2[i]) - np.sum((c1[j] - c1[i]) ** 2, axis=1) / length
        pen = 0.0 if j == n else LAMBDA + GRID_WEIGHT * np.minimum(glob[j], local[length]) - reward[j]
        short = (length < MIN_SEGMENT) & (i > 0) & (j < n)  # pickups and tails may be short, sections may not
        tot = best[i] + sse + pen + np.where(short, np.inf, 0.0)
        k = int(np.argmin(tot))
        best[j], prev[j] = tot[k], i[k]
    out = [n]
    while out[-1] > 0:
        out.append(int(prev[out[-1]]))
    return _fold_slivers(out[::-1], n)


def _fold_slivers(bounds: list[int], n: int) -> list[int]:
    """A short pickup or tail joins its neighbour."""
    out = list(bounds)
    if len(out) > 2 and out[1] < MIN_SEGMENT:
        del out[1]
    if len(out) > 2 and n - out[-2] < MIN_SEGMENT:
        del out[-2]
    return out


def phrases(bounds: list[int], offset: int, n: int, nov: np.ndarray) -> list[dict]:
    """The 8-bar phrase starts, counted from each section start: `length` is 32, 16 or 8 from where that count last
    restarted, so a track whose phrasing shifts keeps its own 32s."""
    out: list[dict] = []
    grid_from = list(bounds[:-1])
    if offset % 8 and (not grid_from or grid_from[0] == 0):
        lead = offset % 8
        if lead >= MIN_SEGMENT and (len(grid_from) == 1 or grid_from[1] > lead):
            grid_from = [0, lead, *grid_from[1:]]
    for a, end in zip(grid_from, [*grid_from[1:], n]):
        for count, p in enumerate(range(a, end, 8)):
            if end - p < 4 and count > 0:
                break
            out.append({"bar": int(p), "length": 32 if count % 4 == 0 else 16 if count % 2 == 0 else 8,
                        "novelty": round(float(nov[p]), 3)})  # fmt: skip
    return out


# --- labels ----------------------------------------------------------------------------------------------------------


@dataclass
class _Seg:
    a: int
    b: int
    kind: str = "body"


def _rising(b: BarFeatures, a: int, e: int) -> bool:
    """Highs or snare density climbing over bars a..e: a riser or a snare roll."""
    if e - a < 3:
        return False
    x = np.arange(e - a)
    high = b.high_db[a:e]
    slope = float(np.polyfit(x, high, 1)[0])
    q = max(1, (e - a) // 4)
    climbed = float(high[-q:].mean() - high[:q].mean())
    snare_up = float(b.snare[e - q:e].mean() - b.snare[a:a + q].mean())
    return (slope >= RISE_DB_PER_BAR and climbed >= RISE_TOTAL_DB) or snare_up >= SNARE_RISE


def label(b: BarFeatures, bounds: list[int]) -> list[dict]:
    """Sections [(kind, start, end)] from the stems.

    - intro / outro: leading / trailing parts without the bass, or without the vocals of a track that sings, or
      clearly thinner than the main sections; a track that stays full to the edge has none.
    - breakdown: drums or bass out, inside the track.
    - build: bass out, highs or snare density rising into full drums and bass (split off the end of a longer part).
    - drop: drums and bass back at high energy after a build or a breakdown.
    - body: the rest.
    """
    on = b.on()
    drums, bass, other, voc = (on[:, i].astype(float) for i in range(4))
    fullness = drums + bass + 0.5 * other + voc
    n = b.n
    sings = float(voc.mean()) >= 0.1
    segs = [_Seg(int(x), int(y)) for x, y in zip(bounds[:-1], bounds[1:])]

    # a rising tail of a long bass-less part is its own segment
    split: list[_Seg] = []
    for s in segs:
        tail = next((k for k in (16, 8, 4) if s.b - s.a >= k + 4 and bass[s.a:s.b].mean() < 0.5
                     and _rising(b, s.b - k, s.b) and not _rising(b, s.a, s.b - k)), 0)  # fmt: skip
        if tail and s.b < n:
            split += [_Seg(s.a, s.b - tail), _Seg(s.b - tail, s.b)]
        else:
            split.append(s)
    segs = split

    def m(a: np.ndarray, s: _Seg) -> float:
        return float(a[s.a:s.b].mean())

    weights = np.array([s.b - s.a for s in segs], float)
    level = np.array([m(fullness, s) for s in segs])
    order = np.argsort(level)
    cum = np.cumsum(weights[order])
    main_level = float(level[order][np.searchsorted(cum, 0.65 * cum[-1])])  # the fullness two thirds of the bars reach
    energy = np.array([m(b.mix_db, s) for s in segs])
    full = [m(drums, s) >= 0.6 and m(bass, s) >= 0.6 and energy[i] >= -6.0 for i, s in enumerate(segs)]

    def thin(i: int) -> bool:
        s = segs[i]
        if m(bass, s) < 0.5 or level[i] <= main_level - MAIN_MARGIN or energy[i] <= -8.0:
            return True
        return sings and m(voc, s) < 0.25 and s.b - s.a <= VOCAL_EDGE_MAX_BARS  # longer: an instrumental section

    i_end = 0
    while i_end < len(segs) - 1 and thin(i_end) and segs[i_end].b <= EDGE_MAX_BARS:
        i_end += 1
    o_start = len(segs)
    while o_start - 1 > i_end and thin(o_start - 1) and n - segs[o_start - 1].a <= EDGE_MAX_BARS:
        o_start -= 1
    for i, s in enumerate(segs):
        if i < i_end:
            s.kind = "intro"
        elif i >= o_start:
            s.kind = "outro"
        elif m(drums, s) < 0.4 or (m(bass, s) < 0.4 and energy[i] < main_energy(energy, weights) - 2.0):
            s.kind = "breakdown"
    for i, s in enumerate(segs[:-1]):
        if i < o_start and s.b - s.a <= BUILD_MAX_BARS and not full[i] and m(bass, s) < 0.5 and full[i + 1] \
                and _rising(b, s.a, s.b) and (s.a > 0 or i_end > 1):
            s.kind = "build"
    for i in range(1, o_start):
        if full[i] and segs[i - 1].kind in ("build", "breakdown") and segs[i].kind == "body":
            segs[i].kind = "drop"
    # neighbours of one kind stay apart (a verse and a chorus are both body), but one edge is one edge
    out: list[dict] = []
    for s in segs:
        if out and out[-1]["kind"] == s.kind and s.kind in MERGED_KINDS:
            out[-1]["end_bar"] = s.b
        else:
            out.append({"kind": s.kind, "start_bar": s.a, "end_bar": s.b})
    return out


def main_energy(energy: np.ndarray, weights: np.ndarray) -> float:
    """The level the longest-lasting loud parts sit at."""
    order = np.argsort(energy)
    cum = np.cumsum(weights[order])
    return float(energy[order][np.searchsorted(cum, 0.6 * cum[-1])])


def mood(b: BarFeatures) -> tuple[str, float]:
    """High, mid or low for the track, from its tempo, how much of it drives (drums and bass together) and how busy
    the drums are. There is no ground truth for this: it is Rekordbox's idea, not its numbers."""
    on = b.on()
    driving = on[:, 0] & on[:, 1]
    tempo = float(np.clip((b.bpm - 80.0) / 50.0, 0.0, 1.0))
    share = float(driving.mean())
    busy = float(np.clip(b.onsets[on[:, 0], 0].mean() / 3.0, 0.0, 1.0)) if on[:, 0].any() else 0.0
    score = 0.45 * tempo + 0.35 * share + 0.2 * busy
    return ("high" if score >= 0.72 else "low" if score < 0.45 else "mid"), round(score, 3)


# --- fill-ins --------------------------------------------------------------------------------------------------------


def fills(b: BarFeatures, phrase_bars: list[int]) -> list[dict]:
    """Drum fills in the last 1-4 beats before each phrase start: each beat's sixteenths compared with the same beat
    of the groove (the median of the preceding bars). Counted back from the phrase end, until a beat is plain groove."""
    out: list[dict] = []
    grid = b.drum_grid
    for e in sorted(set(phrase_bars)):
        if e < 2 or e > b.n:
            continue
        end_beat = e * BEATS_PER_BAR
        last = grid[end_beat - BEATS_PER_BAR:end_beat]
        ref_bars = np.arange(max(0, e - 5), e - 1)
        if ref_bars.size < 1 or last.shape[0] < BEATS_PER_BAR:
            continue
        ref = np.median(grid[ref_bars[:, None] * BEATS_PER_BAR + np.arange(BEATS_PER_BAR)], axis=0)  # (4, 4)
        silent = float(ref.sum()) < 1.0
        beats = 0
        total = 0.0
        for k in range(BEATS_PER_BAR - 1, -1, -1):
            excess = float(np.maximum(last[k] - ref[k], 0.0).sum())
            plays = float(last[k].sum())
            if (silent and plays >= FILL_MIN_ONSETS - 1.0 * (k < 3)) or (not silent and excess >= FILL_EXCESS):
                beats += 1
                total += excess
            else:
                break
        if beats:
            strength = float(np.clip(total / beats / (2 * FILL_EXCESS), 0.0, 1.0))
            start = end_beat - beats
            out.append({"bar": int(e), "start_beat": int(start + b.first_bar_beat), "beats": beats,
                        "start_s": round(float(np.interp(start / BEATS_PER_BAR, np.arange(b.bar_s.size), b.bar_s)), 3),
                        "strength": round(strength, 3)})  # fmt: skip
    return out


# --- where to mix ----------------------------------------------------------------------------------------------------


def _candidate(b: BarFeatures, on: np.ndarray, bar: int, length: int, section: str, score: float) -> dict:
    span = on[bar:bar + max(length, 1)]
    return {"bar": int(bar), "start_s": round(float(b.bar_s[bar]), 3), "bars": int(length), "section": section,
            "score": round(float(np.clip(score, 0.0, 1.0)), 3), "vocals": bool(span[:, 3].mean() >= 0.25),
            "bass": bool(span[:, 1].mean() >= 0.5), "drums": bool(span[:, 0].mean() >= 0.5)}  # fmt: skip


def _kind_at(sections: list[dict], bar: int) -> str:
    return next((s["kind"] for s in sections if s["start_bar"] <= bar < s["end_bar"]), "body")


def mix_candidates(b: BarFeatures, sections: list[dict], phrase_list: list[dict], limit: int = 5) -> tuple[list[dict],
                                                                                                         list[dict]]:
    """Scored mix-in and mix-out points, best first; at least one each for any track with bars.

    A **mix-in** is a phrase start where the next track can come in under the current one: `bars` is the runway until
    the vocals start or the full arrangement lands. The score favours a long, vocal-free runway with drums to beatmatch
    and no bass to clash, on a strong phrase, early in the track. A **mix-out** is a phrase start where this track can
    begin to leave: `bars` is what is left until it ends (or until a thin part ends). The score favours vocal-free,
    thinning arrangements late in the track with a long enough tail. Vocals are allowed on both, at a lower score: a
    radio edit with no outro still gets a place to leave.
    """
    n = b.n
    on = b.on()
    voc = on[:, 3]
    strengths = {p["bar"]: p["length"] for p in phrase_list}
    pts = sorted({p["bar"] for p in phrase_list if p["bar"] < n} | {s["start_bar"] for s in sections})
    mains = [s["start_bar"] for s in sections if s["kind"] in ("drop", "body")]

    def run_length(flags: np.ndarray, start: int) -> int:
        stop = np.flatnonzero(flags[start:])
        return int(stop[0]) if stop.size else n - start

    def phrase_score(bar: int) -> float:
        return {32: 1.0, 16: 0.8, 8: 0.55}.get(strengths.get(bar, 0), 0.3)

    ins: list[dict] = []
    outs: list[dict] = []
    for bar in pts:
        kind = _kind_at(sections, bar)
        pos = bar / max(n, 1)
        # mix-in: runway until the vocals (in two consecutive bars) or the first main section
        sung = voc & np.roll(voc, -1)
        sung[-1] = voc[-1]
        next_main = next((m for m in mains if m > bar), n)
        runway = min(run_length(sung, bar), next_main - bar) if kind in ("intro", "breakdown", "build") else \
            run_length(sung, bar)
        runway = min(runway, 64)
        span = on[bar:bar + max(min(runway, 32), 1)]
        clean = 1.0 - float(span[:, 3].mean())
        score = (0.3 * min(runway / 32.0, 1.0) + 0.2 * clean + 0.15 * float(span[:, 0].mean())
                 + 0.15 * (1.0 - float(span[:, 1].mean())) + 0.1 * phrase_score(bar) + 0.1 * max(0.0, 1.0 - pos / 0.4))  # fmt: skip
        score *= {"intro": 1.0, "breakdown": 0.75, "build": 0.6}.get(kind, 0.55)
        if pos <= 0.75 and n - bar >= 8:
            ins.append(_candidate(b, on, bar, max(runway, 1), kind, score))
        # mix-out: how much is left, and is it thin?
        left = n - bar
        if pos >= 0.25 and left >= 4:
            tail = on[bar:]
            clean = 1.0 - float(tail[:, 3].mean())
            thinner = float(np.clip((b.mix_db[max(0, bar - 8):bar].mean() - b.mix_db[bar:bar + 8].mean()) / 6.0, 0.0,
                                    1.0)) if bar > 0 else 0.0  # fmt: skip
            tail_fit = float(np.clip(left / 16.0, 0.0, 1.0)) * float(np.clip((96.0 - left) / 64.0, 0.0, 1.0))
            score = (0.3 * tail_fit + 0.25 * clean + 0.15 * thinner + 0.1 * float(tail[:, 0].mean())
                     + 0.1 * phrase_score(bar) + 0.1 * pos)  # fmt: skip
            score *= {"outro": 1.0, "breakdown": 0.8}.get(kind, 0.7)
            outs.append(_candidate(b, on, bar, left, kind, score))
    if not ins and n:
        ins.append(_candidate(b, on, 0, min(n, 8), _kind_at(sections, 0), 0.05))
    if not outs and n:
        s = max(0, n - 8)
        outs.append(_candidate(b, on, s, n - s, _kind_at(sections, s), 0.05))
    return (sorted(ins, key=lambda c: -c["score"])[:limit], sorted(outs, key=lambda c: -c["score"])[:limit])


# --- the analysis ----------------------------------------------------------------------------------------------------


def decide(b: BarFeatures) -> dict:
    """Every decision from the bar features: pure, so it can be tuned on cached features and tested on synthetic ones."""
    n = b.n
    nov = novelty(b)
    g = _matrix(b)
    offset = phrase_offset(boundary_strength(g, segment(g, nov, None)))
    bounds = segment(g, nov, offset)
    secs = label(b, bounds)
    phrase_list = phrases([s["start_bar"] for s in secs] + [n], offset, n, nov)

    def t(bar: int) -> float:
        return round(float(b.bar_s[bar]), 3)

    sections = [{**s, "start_s": 0.0 if s["start_bar"] == 0 else t(s["start_bar"]), "end_s": t(s["end_bar"]),
                 "novelty": round(float(nov[s["start_bar"]]), 3)} for s in secs]  # fmt: skip
    phrase_out = [{**p, "start_s": t(p["bar"])} for p in phrase_list]
    fill = fills(b, [p["bar"] for p in phrase_list if p["bar"] > 0] + [s["start_bar"] for s in secs if s["start_bar"]])
    mix_in, mix_out = mix_candidates(b, secs, phrase_list)
    label_mood, mood_score = mood(b)
    ref = np.percentile(b.db, 95, axis=0)
    activity = np.clip((b.db - ref + LEVEL_RANGE_DB) / LEVEL_RANGE_DB, 0.0, 1.0)
    return {
        "version": VERSION, "source": "stems", "duration_s": round(float(b.bar_s[-1]), 3),
        "first_bar_beat": int(b.first_bar_beat), "bar_count": int(n), "phrase_offset": int(offset), "mood": label_mood,
        "mood_score": mood_score, "sections": sections, "phrases": phrase_out, "fills": fill,
        "mix_in": mix_in, "mix_out": mix_out,
        "activity": {s: np.round(activity[:, i], 2).tolist() for i, s in enumerate(STEMS)},
    }  # fmt: skip


def analyse(files: dict[str, Path], beats_result: dict, check: Callable[[int], None] | None = None) -> dict:
    return decide(bar_features(files, beats_result, check))


def seedbox1_fallback(path: Path | str) -> dict:
    """Seedbox 1's sections and edges in the same shape, for a track without stems."""
    from musicreco import structure  # noqa: PLC0415

    a = structure.analyze(Path(path))
    sections = [{"kind": s["kind"], "start_bar": int(s["start_bar"]), "end_bar": int(s["end_bar"]),
                 "start_s": round(max(0.0, float(s["start_s"])), 3), "end_s": round(float(s["end_s"]), 3),
                 "novelty": None} for s in a["sections"]]  # fmt: skip

    def edge(bar: int, bars: int, e: dict) -> dict:
        return {"bar": bar, "start_s": round(max(0.0, a["first_downbeat_s"] + bar * a["bar_s"]), 3), "bars": bars,
                "section": _kind_at(sections, bar), "score": 0.5, "vocals": bool(e.get("has_vocals")),
                "bass": None, "drums": bool(e.get("drums_only")) or None}  # fmt: skip

    intro, outro = a.get("intro") or {}, a.get("outro") or {}
    count = int(a["bar_count"])
    i_bars, o_bars = int(intro.get("bars") or 8), int(outro.get("bars") or 8)
    return {
        "version": VERSION, "source": "seedbox1", "duration_s": round(float(a["duration_s"]), 3), "first_bar_beat": 0,
        "bar_count": count, "phrase_offset": int(a.get("phrase_offset") or 0), "mood": None, "mood_score": None,
        "sections": sections, "phrases": [], "fills": [], "mix_in": [edge(0, i_bars, intro)],
        "mix_out": [edge(max(0, count - o_bars), o_bars, outro)], "activity": None,
    }  # fmt: skip


# --- the job ---------------------------------------------------------------------------------------------------------


@register("sections", pool="light")
def sections_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"hash": ...}`. From the stems and the beats when the track has them; otherwise Seedbox 1's."""
    hash_ = payload["hash"]
    lib = ctx.library
    if lib.track(hash_) is None:
        raise SectionsError(f"no track {hash_}")
    row = lib.analysis(hash_, beat_analysis.ANALYSER, beat_analysis.VERSION)
    folder = stem_cache.current(lib, hash_)
    files = waveform.stem_files(folder) if folder is not None else None
    if row is not None and files is not None:
        stem_cache.touch(folder)
        total = max(1.0, float(row["data"].get("duration_s") or 0.0)) * SAMPLE_RATE
        seen = [0]

        def check(samples: int) -> None:
            seen[0] += samples
            ctx.progress(min(0.95, seen[0] / total))

        data = analyse(files, row["data"], check)
    else:
        sources = [Path(p) for p in lib.paths(hash_) if Path(p).is_file()]
        if not sources:
            raise SectionsError("none of the track's files is there any more")
        data = seedbox1_fallback(sources[0])
    lib.put_analysis(hash_, ANALYSER, VERSION, data)
    return {"hash": hash_, "source": data["source"], "sections": len(data["sections"]), "mood": data["mood"]}


def submit(jobs: Any, hash_: str, *, priority: int | str = "library", front: bool = False) -> dict:
    return jobs.submit("sections", {"hash": hash_}, priority=priority, front=front)


def on_job_finished(jobs: Any, lib: Any, row: dict) -> None:
    """The supervisor's listener: sections run once a track has its beats and its stems, or when stems were skipped.
    Stems that arrive later replace an analysis made without them."""
    payload = row.get("payload")
    if row.get("kind") not in ("beats", "stems") or row.get("state") != "done" or not isinstance(payload, dict):
        return
    hash_ = payload.get("hash")
    if not hash_:
        return
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    have_beats = lib.analysis(hash_, beat_analysis.ANALYSER, beat_analysis.VERSION) is not None
    have_stems = stem_cache.current(lib, hash_) is not None
    if not have_beats:
        return
    existing = lib.analysis(hash_, ANALYSER, VERSION)
    stems_new = have_stems and (existing is None or existing["data"].get("source") != "stems")
    stems_skipped = row.get("kind") == "stems" and result.get("skipped") and existing is None
    if stems_new or stems_skipped:
        submit(jobs, hash_)
