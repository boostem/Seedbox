"""Piecewise beatgrids: segments `[{start_beat, start_s, bpm}]`, and fitting one to detected beat times.

A grid maps beat numbers (floats; beat 0 is the track's first downbeat, and downbeats are the multiples of
`beats_per_bar`) to file time and back. Each segment holds from its `start_beat` up to the next segment's; the first
one also extends back before its start, the last one on to the end. Segments meet: a segment's `start_s` is where
the one before it reaches `start_beat`, so the grid is continuous and monotonic. A quantised track (most electronic
music) has one segment.

Fitting (`fit`): the detected beats get integer indices (each beat is placed against a line through the last good
beats, so a stretch of off-beat or missing detections can't shift the count after it), then a robust (Huber) line
through index -> time. If most beats sit on that line the grid is constant. Otherwise the track is split top-down
where a straight line stops holding (a tempo change, or a live drummer drifting), and a continuous piecewise-linear
fit with knots at the splits gives the segments.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

TOL_S = 0.025  # a beat within 25 ms of the grid is on it (beat_this's frames are 20 ms)
INLIERS = 0.85  # one segment when at least this share of the beats are on a single line ...
MIN_SEG = 16  # ... else segments of at least 16 beats (4 bars)
MAX_SEGMENTS = 64
# (beats, relative slope change): that many beats in a row off the line that make a line of their own, with a tempo
# that much different, are a tempo change to follow. A jump is caught within 4 beats (before the count can slip), a
# slow drift over 16 (where the slope is measured well enough).
REANCHOR = ((4, 0.01), (16, 0.002))


@dataclass(frozen=True)
class Segment:
    start_beat: float
    start_s: float
    bpm: float

    def to_json(self) -> dict:
        return {"start_beat": round(float(self.start_beat), 4), "start_s": round(float(self.start_s), 5),
                "bpm": round(float(self.bpm), 4)}  # fmt: skip


class Grid:
    """Beat <-> time over sorted, continuous segments."""

    def __init__(self, segments: Iterable[Segment | dict]) -> None:
        segs = [s if isinstance(s, Segment) else Segment(float(s["start_beat"]), float(s["start_s"]), float(s["bpm"]))
                for s in segments]  # fmt: skip
        if not segs:
            raise ValueError("a grid needs at least one segment")
        segs.sort(key=lambda s: s.start_beat)
        for s in segs:
            if not (1.0 <= s.bpm <= 1000.0) or not np.isfinite(s.start_s):
                raise ValueError(f"bad grid segment {s}")
        for a, b in zip(segs, segs[1:]):
            if b.start_beat <= a.start_beat or b.start_s <= a.start_s:
                raise ValueError("grid segments must increase in beat and time")
        self.segments = segs
        self._beat = np.array([s.start_beat for s in segs])
        self._sec = np.array([s.start_s for s in segs])
        self._spb = np.array([60.0 / s.bpm for s in segs])  # seconds per beat

    @classmethod
    def constant(cls, beat0_s: float, bpm: float) -> Grid:
        return cls([Segment(0.0, float(beat0_s), float(bpm))])

    @classmethod
    def from_json(cls, raw: list[dict]) -> Grid:
        return cls(raw)

    def to_json(self) -> list[dict]:
        return [s.to_json() for s in self.segments]

    @property
    def is_constant(self) -> bool:
        return len(self.segments) == 1

    def time_at(self, beat: float | np.ndarray) -> float | np.ndarray:
        b = np.asarray(beat, dtype=np.float64)
        i = np.clip(np.searchsorted(self._beat, b, side="right") - 1, 0, len(self.segments) - 1)
        out = self._sec[i] + (b - self._beat[i]) * self._spb[i]
        return float(out) if np.ndim(beat) == 0 else out

    def beat_at(self, t: float | np.ndarray) -> float | np.ndarray:
        x = np.asarray(t, dtype=np.float64)
        i = np.clip(np.searchsorted(self._sec, x, side="right") - 1, 0, len(self.segments) - 1)
        out = self._beat[i] + (x - self._sec[i]) / self._spb[i]
        return float(out) if np.ndim(t) == 0 else out

    def bpm_at(self, beat: float | np.ndarray) -> float | np.ndarray:
        b = np.asarray(beat, dtype=np.float64)
        i = np.clip(np.searchsorted(self._beat, b, side="right") - 1, 0, len(self.segments) - 1)
        out = 60.0 / self._spb[i]
        return float(out) if np.ndim(beat) == 0 else out

    def beats(self, t0: float, t1: float, step: float = 1.0) -> np.ndarray:
        """Times of the grid's beats (multiples of `step`) in [t0, t1]."""
        b0, b1 = np.ceil(self.beat_at(t0) / step - 1e-9), np.floor(self.beat_at(t1) / step + 1e-9)
        return np.asarray(self.time_at(np.arange(b0, b1 + 1) * step), dtype=np.float64).reshape(-1)

    def main_bpm(self, t0: float | None = None, t1: float | None = None) -> float:
        """The tempo that covers the most time between t0 and t1 (default: the segments' own span)."""
        if self.is_constant:
            return self.segments[0].bpm
        lo = self._sec[0] if t0 is None else t0
        hi = self._sec[-1] + 1.0 if t1 is None else t1
        ends = np.append(self._sec[1:], np.inf)
        span = np.clip(np.minimum(ends, hi) - np.maximum(self._sec, lo), 0, None)
        return float(60.0 / self._spb[int(np.argmax(span))])

    def shifted(self, beats: float) -> Grid:
        """The same grid renumbered: beat b becomes b - beats."""
        return Grid([Segment(s.start_beat - beats, s.start_s, s.bpm) for s in self.segments])

    def scaled(self, factor: float, offset: float = 0.0) -> Grid:
        """Another beat level: beat b becomes (b - offset) * factor (factor 2: twice the tempo)."""
        return Grid([Segment((s.start_beat - offset) * factor, s.start_s, s.bpm * factor) for s in self.segments])


# --- fitting ---------------------------------------------------------------------------------------------------


def global_period(times: np.ndarray, period: float, span: float = 0.04, steps: int = 801) -> tuple[float, float]:
    """(seconds per beat, phase in s) of the constant grid that the most detections line up with, searched within
    ±`span` of `period`: the beat times' phase coherence, |sum exp(2 pi i t / P)|, as a function of P."""
    t = np.asarray(times, dtype=np.float64)
    ps = period * (1 + np.linspace(-span, span, steps))
    best_p, best = period, -1.0
    for block in np.array_split(ps, max(1, steps // 200)):
        z = np.exp(2j * np.pi * t[None, :] / block[:, None]).sum(axis=1)
        i = int(np.argmax(np.abs(z)))
        if abs(z[i]) > best:
            best, best_p = float(abs(z[i])), float(block[i])
    z = np.exp(2j * np.pi * t / best_p).sum()
    return best_p, float((np.angle(z) / (2 * np.pi)) % 1.0 * best_p)


def _dedupe(t: np.ndarray, k: np.ndarray, err: np.ndarray) -> np.ndarray:
    """Mask keeping, for each index, the detection closest to the grid."""
    order = np.lexsort((np.abs(err), k))
    first = np.ones(order.size, bool)
    first[1:] = k[order][1:] != k[order][:-1]
    keep = np.zeros(t.size, bool)
    keep[order[first]] = True
    return keep


def index_beats(times: np.ndarray, period: float | None = None,
                window: int = 16) -> tuple[np.ndarray, np.ndarray, np.ndarray]:  # fmt: skip
    """Integer beat indices for detected beat times: `(times, indices, on_grid)`, one detection per index.

    Each beat is placed against a line through the last `window` beats that were on it, so a run of off-beat
    detections (a breakdown tracked on the hats) is marked off-grid and doesn't move the count after it, while
    a few beats in a row that make a line of their own with another slope (a tempo change, REANCHOR) become the new
    line.
    Of two detections that land on the same index, the one closer to the line stays."""
    t = np.asarray(times, dtype=np.float64)
    if t.size < 2:
        return t, np.arange(t.size), np.ones(t.size, bool)
    if period is None:
        period = float(np.median(np.diff(t)))
    keep_t, keep_k, keep_e = [t[0]], [0], [0.0]
    since = 0  # the line is fitted to the good beats from here on (moved on at a tempo change)
    for x in t[1:]:
        kt, kk, ke = np.array(keep_t), np.array(keep_k), np.abs(np.array(keep_e))
        good = np.nonzero(ke < 2 * TOL_S)[0]
        good = good[good >= since][-window:]
        slope = period
        if good.size >= 4 and kk[good[-1]] > kk[good[0]]:
            sl, icpt = np.polyfit(kk[good], kt[good], 1)
            if 0.9 * period < sl < 1.1 * period:
                slope = sl
                pos = (x - icpt) / slope
            else:
                pos = kk[good[-1]] + (x - kt[good[-1]]) / period
        else:
            last = good[-1] if good.size else len(kt) - 1
            pos = kk[last] + (x - kt[last]) / period
        k = int(np.floor(pos + 0.5))
        err = (pos - k) * slope
        if k < keep_k[-1]:
            continue
        if k == keep_k[-1]:  # a second detection of the same beat: keep the closer one
            if abs(err) < abs(keep_e[-1]):
                keep_t[-1], keep_e[-1] = float(x), err
            continue
        keep_t.append(float(x))
        keep_k.append(k)
        keep_e.append(err)
        for run, change in REANCHOR:
            if len(keep_e) > run and all(abs(e) >= 2 * TOL_S for e in keep_e[-run:]):
                # off the line for a while: a tempo change if those beats make a line of their own with another
                # slope (a run of off-beats keeps the old slope and stays off)
                rk, rt = np.array(keep_k[-run:], float), np.array(keep_t[-run:])
                sl, ic = np.polyfit(rk, rt, 1)
                if np.abs(rt - (ic + sl * rk)).max() < TOL_S and abs(sl / slope - 1) > change:
                    keep_e[-run:] = [0.0] * run
                    since = len(keep_e) - run
                    break
    return np.array(keep_t), np.array(keep_k), np.abs(np.array(keep_e)) < 2 * TOL_S


def _huber_lstsq(a: np.ndarray, y: np.ndarray, delta: float = TOL_S, iters: int = 12) -> np.ndarray:
    w = np.ones_like(y)
    coef = np.linalg.lstsq(a, y, rcond=None)[0]
    for _ in range(iters):
        r = np.abs(y - a @ coef)
        w = np.where(r <= delta, 1.0, delta / np.maximum(r, 1e-12))
        sw = np.sqrt(w)
        coef = np.linalg.lstsq(a * sw[:, None], y * sw, rcond=None)[0]
    return coef


def robust_line(k: np.ndarray, t: np.ndarray) -> tuple[float, float]:
    """(time of index 0, seconds per index) of a Huber fit of t against k."""
    a = np.column_stack([np.ones_like(k, dtype=np.float64), k.astype(np.float64)])
    c = _huber_lstsq(a, t.astype(np.float64))
    return float(c[0]), float(c[1])


def _rolling_median(x: np.ndarray, n: int) -> np.ndarray:
    if x.size < n:
        return np.full(x.size, np.median(x) if x.size else 0.0)
    v = np.median(np.lib.stride_tricks.sliding_window_view(x, n), axis=1)
    return np.concatenate([np.full(n // 2, v[0]), v, np.full(x.size - v.size - n // 2, v[-1])])


def coverage(grid: Grid, times: np.ndarray, t0: float | None = None, t1: float | None = None) -> float:
    """Share of the grid's beats between t0 and t1 (default: the first and last detection) with a detection
    within TOL_S."""
    t = np.sort(np.asarray(times, dtype=np.float64))
    if t.size == 0:
        return 0.0
    g = grid.beats(t[0] if t0 is None else t0, t[-1] if t1 is None else t1)
    if g.size == 0:
        return 0.0
    i = np.clip(np.searchsorted(t, g), 1, t.size - 1) if t.size > 1 else np.zeros(g.size, int)
    d = np.minimum(np.abs(t[i] - g), np.abs(t[np.maximum(i - 1, 0)] - g))
    return float(np.mean(d < TOL_S))


def _fits(k: np.ndarray, t: np.ndarray) -> bool:
    t0, spb = robust_line(k, t)
    r = np.abs(t - (t0 + spb * k))
    cover = np.unique(k[r < TOL_S]).size / max(1, k[-1] - k[0] + 1)
    return bool(cover >= INLIERS and _rolling_median(r, 8).max() < TOL_S)


def _sse_prefix(k: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, ...]:
    z = np.zeros(1)
    k, t = k.astype(np.float64), t.astype(np.float64)
    return tuple(np.concatenate([z, np.cumsum(v)]) for v in (np.ones_like(k), k, t, k * k, k * t, t * t))


def _sse(p: tuple[np.ndarray, ...], a: int, b: int) -> float:
    n, sk, st, skk, skt, stt = (v[b] - v[a] for v in p)
    var = skk - sk * sk / n
    if n < 2 or var <= 0:
        return 0.0
    slope = (skt - sk * st / n) / var
    return float(max(0.0, (stt - st * st / n) - slope * (skt - sk * st / n)))


def _split(k: np.ndarray, t: np.ndarray, lo: int, hi: int, out: list[int], depth: int = 0) -> None:
    """Split points (indices into k/t) of [lo, hi), top-down, until every piece is a line."""
    if hi - lo < 2 * MIN_SEG or depth > 8 or len(out) >= MAX_SEGMENTS or _fits(k[lo:hi], t[lo:hi]):
        return
    p = _sse_prefix(k, t)
    cands = range(lo + MIN_SEG, hi - MIN_SEG + 1)
    costs = [_sse(p, lo, m) + _sse(p, m, hi) for m in cands]
    m = list(cands)[int(np.argmin(costs))]
    out.append(m)
    _split(k, t, lo, m, out, depth + 1)
    _split(k, t, m, hi, out, depth + 1)


def _continuous(k: np.ndarray, t: np.ndarray, knots: list[float]) -> tuple[float, list[float]]:
    """Huber fit of a continuous piecewise line with these knots: (time at index 0, seconds per index per piece)."""
    cols = [np.ones_like(k, dtype=np.float64), k.astype(np.float64)]
    cols += [np.maximum(0.0, k - kn) for kn in knots]
    c = _huber_lstsq(np.column_stack(cols), t.astype(np.float64))
    return float(c[0]), list(np.cumsum(c[1:]))


@dataclass
class Fit:
    grid: Grid  # in beat indices of the detections (beat 0 = the first detection's index)
    coverage: float  # share of the grid's beats (over the detected span) that have a detection within TOL_S
    residual_ms: float  # median distance of the on-grid detections


def _score(grid: Grid, t: np.ndarray) -> float:
    """Detections explained minus half the grid beats with none (over the detected span): the right beat level
    explains the most without inventing beats."""
    cov = coverage(grid, t)
    n = grid.beats(t[0], t[-1]).size
    return cov * n - 0.5 * (1 - cov) * n


def _constant(t: np.ndarray, period: float) -> Grid:
    period, phase = global_period(t, period)
    k = np.floor((t - phase) / period + 0.5)
    err = t - phase - k * period
    keep = _dedupe(t, k, err) & (np.abs(err) < 2 * TOL_S)
    k = k - k[keep][0] if keep.any() else k
    t0, spb = robust_line(k[keep], t[keep]) if keep.sum() >= 2 else (phase, period)
    return Grid([Segment(0.0, t0, 60.0 / spb)])


def _piecewise(t: np.ndarray, period: float) -> Grid | None:
    tk, k, good = index_beats(t, period)
    kg, tg = k[good], tk[good]
    if kg.size < 2 * MIN_SEG:
        return None
    cuts: list[int] = []
    _split(kg, tg, 0, kg.size, cuts)
    knots = sorted(float(kg[m]) for m in cuts)
    t0, spbs = _continuous(kg, tg, knots)
    return _merge(_segments(t0, knots, spbs), kg, tg)


def fit(times: np.ndarray, periods: Iterable[float] = ()) -> Fit | None:
    """The grid through detected beat times, or None for fewer than 8 beats.

    The beat period is the median interval; when that grid covers less than 60 % of the beats, the detections are
    erratic and the period among it and `periods` (e.g. from the activation's autocorrelation) that explains them
    best (`_score`) is used instead. One constant segment when it covers the
    detections about as well as a piecewise grid does (within 3 points), or covers INLIERS of the beats."""
    t = np.sort(np.asarray(times, dtype=np.float64))
    if t.size < 8:
        return None
    const = _constant(t, float(np.median(np.diff(t))))  # the level beat_this tracked, when it tracked steadily
    if coverage(const, t) < 0.6:  # erratic detections: the period that explains them best
        const = max([const, *(_constant(t, float(p)) for p in periods if p and p > 0)], key=lambda g: _score(g, t))
    grid, cov = const, coverage(const, t)
    if cov < INLIERS:
        piece = _piecewise(t, 60.0 / const.segments[0].bpm)
        cov_p = coverage(piece, t) if piece is not None else 0.0
        if piece is not None and cov_p > cov + 0.03:
            grid, cov = piece, cov_p
    g = np.asarray(grid.time_at(np.round(grid.beat_at(t))))
    r = np.abs(t - g)
    on = r < TOL_S
    return Fit(grid, cov, float(np.median(r[on]) * 1000) if on.any() else float("nan"))


def _segments(t0: float, knots: list[float], spbs: list[float]) -> Grid:
    segs = [Segment(0.0, t0, 60.0 / spbs[0])]
    for kn, spb in zip(knots, spbs[1:]):
        prev = segs[-1]
        start_s = prev.start_s + (kn - prev.start_beat) * 60.0 / prev.bpm
        if spb <= 0 or kn <= prev.start_beat:
            continue
        segs.append(Segment(kn, start_s, 60.0 / spb))
    return Grid(segs)


def _merge(grid: Grid, k: np.ndarray, t: np.ndarray, rel: float = 0.0015) -> Grid:
    """Join neighbouring segments whose tempos differ by less than 0.15 %, refitting the knots."""
    knots = [s.start_beat for s in grid.segments[1:]]
    while knots:
        t0, spbs = _continuous(k, t, knots)
        d = [abs(spbs[i + 1] / spbs[i] - 1) for i in range(len(knots))]
        i = int(np.argmin(d))
        if d[i] >= rel:
            return _segments(t0, knots, spbs)
        knots.pop(i)
    t0, spb = robust_line(k, t)
    return Grid([Segment(0.0, t0, 60.0 / spb)])
