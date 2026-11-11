"""`python -m seedbox2.eval curves`: fader and EQ curves recovered from real DJ mixes (#59, a research spike).

    python -m seedbox2.eval curves extract DATASET.json AUDIO_DIR out.jsonl --mix ID... [--limit 50]
    python -m seedbox2.eval curves synth [--n 12]      (recovers known curves from renders of our own templates)
    python -m seedbox2.eval curves shapes curves.jsonl [--k 4] [--json report.json]
    python -m seedbox2.eval curves model curves.jsonl [--json report.json]

The method is Kim, Yang & Nam's (NIME 2021, DAFx 2022): with both tracks aligned to the mix, the mix's spectrum over a
transition is explained as A's and B's spectra through a DJ mixer, each deck's fader times its 3-band EQ, and the gains
are found by convex optimisation. Here it is a linear program (scipy's HiGHS; the reference code uses cvxpy):

- Spectra are powers in sub-bands well inside each isolator band (low 40-140 Hz, mid 400-1.6 kHz, high 3.5-10 kHz:
  away from the 200 Hz / 2.5 kHz crossovers, so a bin hears one band's gain), on 93 ms frames.
- Unknowns per frame are power gains: each deck's fader and, per band, fader x EQ. A's fader only goes down and B's only
  up, A's band gains only fall faster than its fader and B's only rise faster (an EQ is cut on the way out and opened
  on the way in), and no band is above its fader (an EQ doesn't boost). The loss is the L1 error of the predicted
  mix power, relative to each sub-band's mean (as in the paper: squared errors would ignore the quiet bands).
- Powers add because two tracks are uncorrelated, which magnitudes don't, so the gains are solved in power and
  reported as amplitudes, each deck's fader scaled to 1 where it plays alone.

Alignment (the `djmix` package's job, re-done here without librosa or madmom): 1/3-octave log spectra at 4 frames a
second, a subsequence DTW of each track into the mix (near its tracklist timestamp), a robust line through the path
(the DJ's tempo for that track and where it sits), and a fine offset from the onset envelopes. Pitch follows the tempo
here, as on a deck without key lock; at a few percent that moves no energy between the coarse bands.

The dataset is `mir-aidj/djmix-dataset`'s metadata (mixesdb tracklists with audio links); the audio is downloaded by
the user (see seedbox2/README.md, "Learnt fader and EQ curves", for the licence situation: nothing ships).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import optimize, sparse, stats

SR = 22050
FFT, HOP = 4096, 2048  # 186 ms windows, 93 ms frames (the reference code's 8192/4096 at 44.1 kHz)
BANDS = {"low": (40.0, 140.0), "mid": (400.0, 1600.0), "high": (3500.0, 10000.0)}
SUB_PER_BAND = 6
GAIN_MAX = 4.0  # power: a fader may sit 6 dB over the level the track was cut at
PAD_BEATS = 16  # either side of the overlap, so each deck is heard alone too
FADER_COST = 1e-3
AUDIBLE = 0.1  # amplitude (-20 dB): a deck counts as in the mix above this in any band

ALIGN_HOP_S = 0.25
ALIGN_SEARCH_S = 900.0  # around the tracklist timestamp
MIN_MATCH_S = 30.0
RATE_RANGE = (0.88, 1.12)
MAX_WINDOW_S = 420.0
WINDOW_BEATS = 128  # either side of where A stops and B starts to lead: DJs here often blend for 64 bars  # longer means the two tracks' matches are far apart: an alignment or version problem


# --- spectra ----------------------------------------------------------------------------------------------------------


def _sub_edges() -> list[tuple[str, float, float]]:
    out = []
    for name, (lo, hi) in BANDS.items():
        e = np.geomspace(lo, hi, SUB_PER_BAND + 1)
        out += [(name, float(a), float(b)) for a, b in zip(e[:-1], e[1:])]
    return out


SUBS = _sub_edges()
BAND_OF_SUB = np.array([list(BANDS).index(s[0]) for s in SUBS])


def _bin_matrix(sr: int = SR, fft: int = FFT) -> np.ndarray:
    f = np.fft.rfftfreq(fft, 1.0 / sr)
    return np.stack([(f >= a) & (f < b) for _, a, b in SUBS]).astype(np.float64)


_WIN = np.hanning(FFT)


def frames_at(y: np.ndarray, centres: np.ndarray, sr: int = SR) -> np.ndarray:
    """Sub-band powers (len(SUBS), len(centres)) of Hann windows centred at the sample positions `centres` (outside
    the signal reads as silence)."""
    centres = np.round(np.asarray(centres)).astype(np.int64)
    pad = FFT
    yp = np.concatenate([np.zeros(pad, np.float32), np.asarray(y, np.float32), np.zeros(pad, np.float32)])
    idx = centres[:, None] - FFT // 2 + pad + np.arange(FFT)[None, :]
    idx = np.clip(idx, 0, len(yp) - 1)
    spec = np.abs(np.fft.rfft(yp[idx] * _WIN, axis=1)) ** 2
    return (_bin_matrix(sr) @ spec.T)


# --- the linear program -----------------------------------------------------------------------------------------------


@dataclass
class Curves:
    """Amplitude gains per frame: each deck's fader (1 = as loud as it plays alone) and, per band, fader x EQ."""
    fader_a: np.ndarray
    fader_b: np.ndarray
    band_a: np.ndarray  # (3, n): low, mid, high
    band_b: np.ndarray
    error_db: float  # mean |dB| between the mix and the fit, over the frames and sub-bands with energy

    def eq(self, deck: str) -> np.ndarray:
        f = self.fader_a if deck == "a" else self.fader_b
        g = self.band_a if deck == "a" else self.band_b
        return np.where(f > 1e-3, g / np.maximum(f, 1e-3), np.nan)


def solve(mix: np.ndarray, a: np.ndarray, b: np.ndarray) -> Curves:
    """Fader and band gains of A and B that best explain `mix` (all (len(SUBS), n) sub-band powers)."""
    nb, n = mix.shape
    nv = 8 * n + nb * n  # fA, fB, gA[3], gB[3], residuals
    FA, FB = 0, n
    GA = [2 * n + k * n for k in range(3)]
    GB = [5 * n + k * n for k in range(3)]
    E = 8 * n
    rows, cols, vals, rhs = [], [], [], []
    r = 0

    def add(entries: list[tuple[np.ndarray, float]], bound: np.ndarray | float, count: int) -> None:
        nonlocal r
        ri = r + np.arange(count)
        for c, v in entries:
            rows.append(ri)
            cols.append(c)
            vals.append(np.broadcast_to(np.asarray(v, float), (count,)))
        rhs.append(np.broadcast_to(np.asarray(bound, float), (count,)))
        r += count

    i0, i1 = np.arange(n - 1), np.arange(1, n)
    add([(FA + i1, 1.0), (FA + i0, -1.0)], 0.0, n - 1)  # A's fader only down
    add([(FB + i0, 1.0), (FB + i1, -1.0)], 0.0, n - 1)  # B's only up
    for k in range(3):
        add([(GA[k] + np.arange(n), 1.0), (FA + np.arange(n), -1.0)], 0.0, n)  # no boost
        add([(GB[k] + np.arange(n), 1.0), (FB + np.arange(n), -1.0)], 0.0, n)
        # A's band falls at least as fast as its fader, B's rises at least as fast
        add([(GA[k] + i1, 1.0), (GA[k] + i0, -1.0), (FA + i1, -1.0), (FA + i0, 1.0)], 0.0, n - 1)
        add([(GB[k] + i0, 1.0), (GB[k] + i1, -1.0), (FB + i1, 1.0), (FB + i0, -1.0)], 0.0, n - 1)
    scale = np.maximum(mix.mean(axis=1, keepdims=True), 1e-12)
    m, pa, pb = mix / scale, a / scale, b / scale
    t = np.arange(n)
    for j in range(nb):
        k = BAND_OF_SUB[j]
        e = E + j * n + t
        # +-(gA*pa + gB*pb - m) <= e
        add([(GA[k] + t, pa[j]), (GB[k] + t, pb[j]), (e, -1.0)], m[j], n)
        add([(GA[k] + t, -pa[j]), (GB[k] + t, -pb[j]), (e, -1.0)], -m[j], n)
    A = sparse.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(r, nv))
    c = np.zeros(nv)
    c[E:] = 1.0 / (nb * n)
    c[FA:FB + n] = FADER_COST / n  # the fader is otherwise free above its bands: this makes it their envelope
    bounds = np.zeros((nv, 2))
    bounds[:E, 1] = GAIN_MAX
    bounds[E:, 1] = np.inf
    res = optimize.linprog(c, A_ub=A, b_ub=np.concatenate(rhs), bounds=bounds, method="highs")
    if res.status != 0:
        raise ValueError(f"linear program: {res.message}")
    x = res.x
    fa, fb = x[FA:FA + n], x[FB:FB + n]
    ga = np.stack([x[GA[k]:GA[k] + n] for k in range(3)])
    gb = np.stack([x[GB[k]:GB[k] + n] for k in range(3)])
    est = ga[BAND_OF_SUB] * a + gb[BAND_OF_SUB] * b
    live = mix > mix.mean(axis=1, keepdims=True) * 1e-3
    err = float(np.mean(np.abs(10 * np.log10((est[live] + 1e-12) / (mix[live] + 1e-12)))))
    # amplitudes; each fader and band at 1 where it is highest (A's at the start, B's at the end), since the tracks'
    # own levels and tone aren't the mix's
    def norm(x: np.ndarray) -> np.ndarray:
        x = np.clip(x, 0, None)
        return np.sqrt(x / np.maximum(x.max(axis=-1, keepdims=True), 1e-9))

    return Curves(norm(fa), norm(fb), norm(ga), norm(gb), err)


def span(c: Curves) -> tuple[int, int] | None:
    """(first, last) frame where both decks are audible: B in any band above AUDIBLE, A still above it."""
    a_on = c.band_a.max(axis=0) > AUDIBLE
    b_on = c.band_b.max(axis=0) > AUDIBLE
    both = np.flatnonzero(a_on & b_on)
    return (int(both[0]), int(both[-1])) if both.size else None


# --- our templates --------------------------------------------------------------------------------------------------

BAND_CENTRES = np.array([math.sqrt(lo * hi) for lo, hi in BANDS.values()])
TEMPLATE_TECHNIQUES = ("long blend", "phrase blend", "bass swap", "filter sweep", "echo out")


def _filter_amp(value: np.ndarray) -> np.ndarray:
    """(3, n) amplitude of the DJ filter at each band's centre (automation.filter_power's curve)."""
    from seedbox2.mix.automation import FILTER_HP_HZ, FILTER_LP_HZ

    v = np.asarray(value, float)
    m = np.abs(v)
    lp = FILTER_LP_HZ[1] * (FILTER_LP_HZ[0] / FILTER_LP_HZ[1]) ** m
    hp = FILTER_HP_HZ[0] * (FILTER_HP_HZ[1] / FILTER_HP_HZ[0]) ** m
    f = BAND_CENTRES[:, None]
    gain = np.where(v < 0, 1.0 / (1.0 + (f / lp) ** 8), 1.0 / (1.0 + (hp / f) ** 8))
    return np.sqrt(1.0 + (gain - 1.0) * np.clip(m / 0.03, 0.0, 1.0))


def template(technique: str, bars: int, beats: np.ndarray, swap: int | None = None) -> dict[str, np.ndarray]:
    """Our hand-made blend (`mixplan._blend_body`) as the curves `solve` recovers, at master `beats` (0 = B comes in):
    per deck the fader and, per band, fader x isolator x filter (amplitudes). `swap` is the bass swap's bar; by
    default halfway, as `_swap` puts it when both tracks have a phrase there."""
    from musicreco import mixplan, mixrender

    A, B = mixplan._lanes(), mixplan._lanes()
    swap = bars // 2 if swap is None else swap
    mixplan._blend_body(technique, bars, swap if swap > 0 else None, A, B)
    out: dict[str, np.ndarray] = {}
    for name, deck in (("a", A), ("b", B)):
        lane = {k: mixplan.lane_value(mixplan._kf(v, -1e6, k), beats) for k, v in deck.items()}
        vol = np.asarray(lane["volume"], float)
        iso = np.stack([mixrender._iso_gain(lane[k]) for k in ("eq_low", "eq_mid", "eq_high")]).astype(float)
        out[f"fader_{name}"] = vol
        out[f"band_{name}"] = vol * iso * _filter_amp(lane["filter"])
    return out


# --- a synthetic check: our own renders, known curves -------------------------------------------------------------


def _noise_track(rng: np.random.Generator, seconds: float, bpm: float) -> np.ndarray:
    """A crude track: a kick, a bass note, offbeat hats and a chord pad, each with its own random level."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    beat = 60.0 / bpm
    ph = (t % beat) / beat
    kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-ph * 20)) * ph * beat) * np.exp(-ph * 8)
    root = rng.uniform(45, 70)
    bass = np.sin(2 * np.pi * root * t) * (((t + beat / 2) % beat) / beat < 0.4)
    hat = rng.standard_normal(n) * np.exp(-((t + beat / 2) % beat) * 60)
    hat = np.diff(hat, prepend=0.0)
    pad = sum(np.sin(2 * np.pi * root * 2 ** (k + rng.integers(2, 4)) * t + rng.uniform(0, 6)) for k in range(3))
    lv = rng.uniform(0.5, 1.0, 4)
    return (lv[0] * kick + lv[1] * 0.5 * bass + lv[2] * 0.3 * hat + lv[3] * 0.15 * pad).astype(np.float32)


def synth_case(rng: np.random.Generator, technique: str, bars: int) -> dict[str, Any]:
    """Render A and B through our isolator with a template's curves, add a little noise, and recover the curves."""
    from musicreco import mixrender

    bpm = float(rng.uniform(118, 130))
    beat = 60.0 / bpm
    total = 4 * bars + 2 * PAD_BEATS
    n = int(total * beat * SR)
    beats_s = np.arange(n) / SR / beat - PAD_BEATS
    a = _noise_track(rng, n / SR + 1, bpm)[:n]
    b = _noise_track(rng, n / SR + 1, bpm)[:n] * rng.uniform(0.5, 1.5)
    tm = template(technique, bars, beats_s)
    x = np.zeros(n, np.float32)
    for name, y in (("a", a), ("b", b)):
        g = tm[f"band_{name}"] / np.maximum(tm[f"fader_{name}"], 1e-9)
        x += tm[f"fader_{name}"].astype(np.float32) * mixrender._isolator(y, *g.astype(np.float32), SR)
    x += rng.standard_normal(n).astype(np.float32) * 1e-3
    centres = np.arange(FFT // 2, n - FFT // 2, HOP)
    c = solve(frames_at(x, centres), frames_at(a, centres), frames_at(b, centres))
    tb = (centres / SR) / beat - PAD_BEATS
    tt = template(technique, bars, tb)
    err = {k: float(np.mean(np.abs(_db(getattr(c, k)) - _db(tt[k])))) for k in ("band_a", "band_b")}
    return {"technique": technique, "bars": bars, "fit_db": c.error_db, "band_error_db": err}


def _db(x: np.ndarray, floor: float = -30.0) -> np.ndarray:
    return np.maximum(20 * np.log10(np.maximum(x, 1e-9)), floor)


# --- alignment --------------------------------------------------------------------------------------------------------

_ALIGN_FFT = 8192
_THIRDS = np.geomspace(50.0, 10000.0, 24)
_ONSET_HOP = 220  # 10 ms


def align_features(y: np.ndarray, sr: int = SR, hop_s: float = ALIGN_HOP_S) -> np.ndarray:
    """(len(_THIRDS) - 1, n) log powers in 1/3-octave-ish bands, a frame every hop_s."""
    hop = int(round(hop_s * sr))
    f = np.fft.rfftfreq(_ALIGN_FFT, 1.0 / sr)
    m = np.stack([(f >= a) & (f < b) for a, b in zip(_THIRDS[:-1], _THIRDS[1:])]).astype(np.float32)
    win = np.hanning(_ALIGN_FFT).astype(np.float32)
    out = []
    starts = np.arange(0, max(1, len(y) - _ALIGN_FFT), hop)
    for i in range(0, len(starts), 2048):
        s = starts[i:i + 2048]
        idx = s[:, None] + np.arange(_ALIGN_FFT)[None, :]
        seg = np.asarray(y, np.float32)[np.clip(idx, 0, len(y) - 1)] * win
        out.append(m @ (np.abs(np.fft.rfft(seg, axis=1)) ** 2).T)
    return np.log10(np.concatenate(out, axis=1) + 1e-9)


def onset_envelope(y: np.ndarray, sr: int = SR) -> np.ndarray:
    """Half-wave rectified spectral flux at 10 ms, for the fine offset."""
    n = 1024
    starts = np.arange(0, max(1, len(y) - n), _ONSET_HOP)
    out = []
    win = np.hanning(n).astype(np.float32)
    prev = None
    for i in range(0, len(starts), 8192):
        s = starts[i:i + 8192]
        seg = np.asarray(y, np.float32)[np.clip(s[:, None] + np.arange(n)[None, :], 0, len(y) - 1)] * win
        mag = np.log1p(100 * np.abs(np.fft.rfft(seg, axis=1)))
        d = np.diff(mag, axis=0, prepend=mag[:1] if prev is None else prev[None, :])
        prev = mag[-1]
        out.append(np.maximum(d, 0).sum(axis=1))
    env = np.concatenate(out)
    env = env - np.convolve(env, np.ones(50) / 50, mode="same")  # local mean off: 0.5 s
    return np.maximum(env, 0).astype(np.float32)


def _xcorr(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Cross-correlation over every lag where y (shorter) starts inside x: out[k] = sum_t x[k + t] * y[t], summed over
    rows for 2-D inputs, for k in [-(len(y) - 1), len(x) - 1] (index k + len(y) - 1)."""
    n = x.shape[-1] + y.shape[-1] - 1
    nf = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(x, nf) * np.conj(np.fft.rfft(y, nf)), nf)
    if c.ndim > 1:
        c = c.sum(axis=0)
    return np.concatenate([c[-(y.shape[-1] - 1):] if y.shape[-1] > 1 else c[:0], c[:x.shape[-1]]])


def _unit(f: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Frames z-scored by the track's per-band statistics (as the reference code does), then unit length."""
    z = (f - ref.mean(axis=1, keepdims=True)) / (ref.std(axis=1, keepdims=True) + 1e-6)
    return z / (np.linalg.norm(z, axis=0, keepdims=True) + 1e-6)


@dataclass
class Placement:
    """A track in the mix: track time = offset + rate * mix time, heard (dominant) over [start, end] in mix time."""
    rate: float
    offset: float
    start: float
    end: float
    score: float  # mean cosine similarity over the matched span

    def track_time(self, t: np.ndarray | float) -> np.ndarray | float:
        return self.offset + self.rate * np.asarray(t)

    def mix_time(self, u: float) -> float:
        return (u - self.offset) / self.rate


def place(mix_feat: np.ndarray, track_feat: np.ndarray, lo_s: float = 0.0, hi_s: float | None = None,
          hop_s: float = ALIGN_HOP_S) -> Placement | None:
    """The rate and offset at which the track's spectra best match the mix's within [lo_s, hi_s] of mix time,
    and the span over which they do."""
    j0 = max(0, int(lo_s / hop_s))
    j1 = mix_feat.shape[1] if hi_s is None else min(mix_feat.shape[1], int(hi_s / hop_s))
    if j1 - j0 < 64:
        return None
    mx = _unit(mix_feat[:, j0:j1], track_feat)
    tu = np.arange(track_feat.shape[1])
    best = (-np.inf, 1.0, 0)
    for r in np.arange(RATE_RANGE[0], RATE_RANGE[1] + 1e-9, 0.0025):
        # the track as heard at rate r on the mix's frame grid: mix frame k plays track frame r * k
        k = np.arange(int(track_feat.shape[1] / r))
        src = np.interp(r * k, tu, np.arange(track_feat.shape[1]))
        tf = _unit(track_feat[:, np.round(src).astype(int)], track_feat)
        c = _xcorr(mx, tf)
        i = int(np.argmax(c))
        if c[i] > best[0]:
            best = (float(c[i]), float(r), i - (tf.shape[1] - 1))
    _, r, lag = best
    if not RATE_RANGE[0] + 0.005 < r < RATE_RANGE[1] - 0.005:
        return None  # the best is at the edge of the search: a false match
    # per-frame similarity along the best line, smoothed over 8 s: the matched span is where it stays high
    k = np.arange(mx.shape[1])
    u = np.round(r * (k - lag)).astype(int)
    ok = (u >= 0) & (u < track_feat.shape[1])
    tf_all = _unit(track_feat, track_feat)
    sim = np.full(mx.shape[1], -1.0)
    sim[ok] = np.einsum("ij,ij->j", mx[:, ok], tf_all[:, u[ok]])
    sm = np.convolve(sim, np.ones(32) / 32, mode="same")
    runs = _runs(sm > 0.25, gap=int(30 / hop_s))
    if not runs:
        return None
    a, b = max(runs, key=lambda ab: ab[1] - ab[0])
    if (b - a) * hop_s < MIN_MATCH_S:
        return None
    t0 = (j0 + lag) * hop_s
    return Placement(rate=r, offset=-r * t0, start=(j0 + a) * hop_s, end=(j0 + b) * hop_s,
                     score=float(sim[a:b].mean()))


def _runs(on: np.ndarray, gap: int = 16) -> list[tuple[int, int]]:
    """[start, end) runs of True, bridging gaps up to `gap` frames."""
    idx = np.flatnonzero(on)
    if not idx.size:
        return []
    breaks = np.flatnonzero(np.diff(idx) > gap)
    starts = np.concatenate([[idx[0]], idx[breaks + 1]])
    ends = np.concatenate([idx[breaks], [idx[-1]]]) + 1
    return list(zip(starts.tolist(), ends.tolist()))


def refine(mix_env: np.ndarray, track_env: np.ndarray, p: Placement, t0: float, t1: float) -> Placement:
    """Rate and offset to 10 ms from the onset envelopes over [t0, t1] of mix time (where the track is clearest)."""
    hop = _ONSET_HOP / SR
    j0, j1 = max(0, int(t0 / hop)), min(len(mix_env), int(t1 / hop))
    if j1 - j0 < 500:
        return p
    seg = mix_env[j0:j1]
    tm = np.arange(j0, j1) * hop
    best = (-np.inf, p.rate, p.offset)
    for r in p.rate + np.arange(-0.003, 0.0031, 0.0002):
        base = p.offset + p.rate * tm[0]  # pivot at the segment's start: r turns the line about it
        lags = np.arange(-60, 61)
        idx = np.round((base + r * (tm - tm[0])) / hop).astype(int)
        valid = (idx + lags.min() >= 0) & (idx + lags.max() < len(track_env))
        if valid.mean() < 0.8:
            continue
        s, i = seg[valid], idx[valid]
        scores = [float(np.dot(s, track_env[i + L])) for L in lags]
        L = int(np.argmax(scores))
        if scores[L] > best[0]:
            u0 = base + lags[L] * hop
            best = (scores[L], float(r), float(u0 - r * tm[0]))
    _, r, off = best
    return Placement(rate=r, offset=off, start=p.start, end=p.end, score=p.score)


# --- the dataset ------------------------------------------------------------------------------------------------------

STYLE_TAGS = (  # first match wins: mixesdb's category tags to a style
    ("dnb", ("Drum & Bass", "Jungle")),
    ("trance", ("Trance", "Progressive Trance", "Psytrance", "A State Of Trance")),
    ("progressive", ("Progressive House",)),
    ("tech house", ("Tech House", "Deep Tech House")),
    ("techno", ("Techno", "Minimal", "Hard Techno", "Acid Techno", "Dub Techno", "Detroit Techno")),
    ("house", ("House", "Deep House", "Vocal House", "Disco", "Acid House")),
)


def style_of(mix: dict) -> str:
    tags = {t["key"].removeprefix("Category:") for t in mix.get("tags", [])}
    return next((s for s, keys in STYLE_TAGS if tags & set(keys)), "other")


def _timestamp_s(title: str) -> float | None:
    m = re.match(r"\s*\[(\d+)(?::(\d+))?\]", title)
    if not m:
        return None
    return 60.0 * int(m.group(1)) + (int(m.group(2)) if m.group(2) else 0) * (1.0 if ":" in m.group(0) else 0.0)


def _audio(folder: Path, stem: str) -> Path | None:
    return next((p for p in sorted(folder.glob(f"{stem}.*")) if p.suffix not in (".part", ".ytdl", ".json")), None)


def _brief(an: dict) -> dict:
    """What the shape model may read of a Seedbox 1 analysis (musicreco.structure)."""
    keys = ("duration_s", "bpm", "first_downbeat_s", "bar_s", "bar_count", "energy_per_bar", "low_per_bar",
            "vocal_per_bar", "sections", "intro", "outro")
    return {k: an[k] for k in keys if k in an}


def _r(x: np.ndarray, nd: int = 3) -> list:
    return np.round(np.asarray(x, float), nd).tolist()


def extract_mix(mix: dict, audio: Path, log: Any = print, limit: int | None = None, cache: bool = True
                ) -> list[dict]:
    """Every transition of `mix` (djmix metadata) whose two tracks are found in it, as curve records. Where each
    track sits in the mix is kept in AUDIO/placements/, since finding it is the slow part."""
    from musicreco import structure

    path = _audio(audio / "mixes", mix["id"])
    if path is None:
        log(f"{mix['id']}: no audio")
        return []
    y = _decode(path)
    mf, menv = align_features(y), onset_envelope(y)
    cache_path = audio / "placements" / f"{mix['id']}.json" if cache else None
    cache = json.loads(cache_path.read_text()) if cache_path is not None and cache_path.exists() else {}
    if cache_path is not None:
        cache_path.parent.mkdir(exist_ok=True)
    placed: dict[int, tuple[Placement, np.ndarray, np.ndarray, dict]] = {}
    for i, t in enumerate(mix["tracklist"]):
        tp = _audio(audio / "tracks", t["id"]) if t.get("id") else None
        if tp is None:
            continue
        ty = _decode(tp)
        ts = _timestamp_s(t["title"])
        lo, hi = (0.0, None) if ts is None else (ts - ALIGN_SEARCH_S, ts + len(ty) / SR + ALIGN_SEARCH_S)
        key = f"{i}:{t['id']}"
        if key in cache:
            p = Placement(**cache[key]) if cache[key] else None
        else:
            p = place(mf, align_features(ty), lo, hi)
            cache[key] = None if p is None else p.__dict__
            if cache_path is not None:
                cache_path.write_text(json.dumps(cache))
        if p is None:
            log(f"{mix['id']} {i:02} {t['id']}: not found in the mix")
            continue
        an = structure.analyze(tp)
        placed[i] = (p, ty, onset_envelope(ty), _brief(an))
        log(f"{mix['id']} {i:02} {t['id']}: {p.start / 60:5.1f}-{p.end / 60:5.1f} min, rate {p.rate:.4f}, "
            f"score {p.score:.2f}")
    out = []
    for i in sorted(placed):
        if i + 1 not in placed or (limit is not None and len(out) >= limit):
            continue
        (pa, ya, ea, ana), (pb, yb, eb, anb) = placed[i], placed[i + 1]
        if not (pa.start < pb.start and pa.end < pb.end):
            log(f"{mix['id']} {i:02}: tracks out of order, skipped")
            continue
        pa = refine(menv, ea, pa, max(pa.start, pa.end - 90.0), pa.end)
        pb = refine(menv, eb, pb, pb.start, min(pb.end, pb.start + 90.0))
        beat_s = 60.0 / (ana["bpm"] * pa.rate)
        w0 = max(pa.start - 32 * beat_s, min(pa.end, pb.start) - WINDOW_BEATS * beat_s)
        w1 = min(pb.end + 32 * beat_s, max(pa.end, pb.start) + WINDOW_BEATS * beat_s)
        if w1 - w0 > MAX_WINDOW_S or w1 - w0 < 16 * beat_s:
            log(f"{mix['id']} {i:02}: window of {w1 - w0:.0f} s, skipped")
            continue
        t = np.arange(w0, w1, HOP / SR)
        try:
            c = solve(frames_at(y, t * SR), frames_at(ya, np.asarray(pa.track_time(t)) * SR),
                      frames_at(yb, np.asarray(pb.track_time(t)) * SR))
        except ValueError as e:
            log(f"{mix['id']} {i:02}: {e}")
            continue
        sp = span(c)
        rec = {
            "mix": mix["id"], "style": style_of(mix), "i": i,
            "a": mix["tracklist"][i]["id"], "b": mix["tracklist"][i + 1]["id"],
            "bpm": round(ana["bpm"] * pa.rate, 2), "t0": round(w0, 3), "hop_s": HOP / SR,
            "a_map": [pa.offset, pa.rate], "b_map": [pb.offset, pb.rate], "score": [pa.score, pb.score],
            "fit_db": round(c.error_db, 2), "span": sp,
            "fader_a": _r(c.fader_a), "fader_b": _r(c.fader_b), "band_a": _r(c.band_a), "band_b": _r(c.band_b),
            "an_a": ana, "an_b": anb,
        }
        out.append(rec)
        sb = f"{(sp[1] - sp[0]) * HOP / SR / beat_s / 4:.0f} bars" if sp else "no overlap"
        log(f"{mix['id']} {i:02}: fit {c.error_db:.2f} dB, {sb}")
    return out


def _decode(path: Path) -> np.ndarray:
    from musicreco import structure

    old = structure.DECODE_TIMEOUT_S
    structure.DECODE_TIMEOUT_S = 1800  # a two-hour mix
    try:
        return structure.decode_mono(path, SR)
    finally:
        structure.DECODE_TIMEOUT_S = old


# --- shapes -----------------------------------------------------------------------------------------------------------

SHAPE_POINTS = 16  # per curve, over the overlap
FLOOR_DB = -30.0  # the isolator's kill
MAX_FIT_DB = 4.0  # a transition explained worse than this is an alignment or version problem, not a DJ's move
CURVE_KEYS = ("a_low", "a_mid", "a_high", "b_low", "b_mid", "b_high")


def _level(x: np.ndarray) -> np.ndarray:
    """Amplitude to 0..1 on a dB scale: 0 is the kill (-30 dB), 1 full."""
    return np.clip(1.0 - _db(x, FLOOR_DB) / FLOOR_DB, 0.0, 1.0)


def shape(band_a: np.ndarray, band_b: np.ndarray, sp: tuple[int, int]) -> np.ndarray:
    """(6, SHAPE_POINTS) levels of A's and B's bands over the overlap `sp` (frames), on a common 0..1 time."""
    s0, s1 = sp
    t = np.linspace(s0, s1, SHAPE_POINTS)
    fr = np.arange(band_a.shape[1])
    return np.stack([_level(np.interp(t, fr, g)) for g in (*band_a, *band_b)])


def events(sh: np.ndarray) -> dict[str, float]:
    """Readable moments of a shape, as fractions of the overlap: when each of B's bands is half up (-15 dB on the
    level scale) and each of A's half down, and where the lows cross (the bass swap)."""
    t = np.linspace(0.0, 1.0, sh.shape[1])

    def first(mask: np.ndarray) -> float:
        i = np.flatnonzero(mask)
        return float(t[i[0]]) if i.size else 1.0

    out = {}
    for k, band in enumerate(("low", "mid", "high")):
        out[f"b_{band}_in"] = first(sh[3 + k] >= 0.5)
        out[f"a_{band}_out"] = first(sh[k] < 0.5)
    out["swap"] = first(sh[3] > sh[0])
    return out


def record_shape(rec: dict) -> tuple[np.ndarray, float] | None:
    """A curve record's shape and its overlap in bars, or None when it doesn't pass the fit gate."""
    if rec.get("fit_db", 99) > MAX_FIT_DB or not rec.get("span"):
        return None
    if any(not RATE_RANGE[0] + 0.005 < m[1] < RATE_RANGE[1] - 0.005 for m in (rec["a_map"], rec["b_map"])):
        return None  # a rate at the edge of the search is a false match
    s0, s1 = rec["span"]
    if s0 <= 2 or s1 >= len(rec["fader_a"]) - 3:
        return None  # the overlap runs into the window's edge: cut short, its shape would be wrong
    bars = (s1 - s0) * rec["hop_s"] * rec["bpm"] / 240.0
    if bars < 1:
        return None
    return shape(np.asarray(rec["band_a"]), np.asarray(rec["band_b"]), (s0, s1)), bars


def template_shape(technique: str, bars: int) -> np.ndarray:
    """A template's shape over its own overlap, measured as `span` would on a recovered curve."""
    beats = np.linspace(-PAD_BEATS, 4 * bars + PAD_BEATS, 4 * (4 * bars + 2 * PAD_BEATS) + 1)
    tm = template(technique, bars, beats)
    c = Curves(tm["fader_a"], tm["fader_b"], tm["band_a"], tm["band_b"], 0.0)
    sp = span(c)
    assert sp is not None
    return shape(tm["band_a"], tm["band_b"], sp)


def distance_db(x: np.ndarray, y: np.ndarray) -> float:
    """Mean distance between two shapes, in dB."""
    return float(np.mean(np.abs(x - y)) * -FLOOR_DB)


def kmeans(X: np.ndarray, k: int, seed: int = 59, restarts: int = 20) -> tuple[np.ndarray, np.ndarray]:
    """(labels, centres) of the best of `restarts` k-means++ runs."""
    rng = np.random.default_rng(seed)
    best = (np.inf, None, None)
    for _ in range(restarts):
        c = [X[rng.integers(len(X))]]
        for _ in range(1, k):
            d = np.min([((X - ci) ** 2).sum(1) for ci in c], axis=0)
            c.append(X[rng.choice(len(X), p=d / d.sum()) if d.sum() > 0 else rng.integers(len(X))])
        C = np.array(c)
        for _ in range(100):
            lab = np.argmin(((X[:, None, :] - C[None]) ** 2).sum(-1), axis=1)
            new = np.array([X[lab == j].mean(0) if (lab == j).any() else C[j] for j in range(k)])
            if np.allclose(new, C):
                break
            C = new
        inertia = float(((X - C[lab]) ** 2).sum())
        if inertia < best[0]:
            best = (inertia, lab, C)
    return best[1], best[2]


def silhouette(X: np.ndarray, lab: np.ndarray) -> float:
    D = np.sqrt(((X[:, None] - X[None]) ** 2).sum(-1))
    s = []
    for i in range(len(X)):
        same = (lab == lab[i])
        if same.sum() <= 1:
            s.append(0.0)
            continue
        a = D[i, same].sum() / (same.sum() - 1)
        b = min(D[i, lab == j].mean() for j in set(lab.tolist()) - {lab[i]})
        s.append((b - a) / max(a, b))
    return float(np.mean(s))


def nearest_bars(bars: float) -> int:
    from musicreco import mixplan

    return min((b for b in mixplan.BARS if b >= 8), key=lambda b: abs(math.log2(max(bars, 1) / b)))


def shapes_report(recs: list[dict], k: int | None = None) -> dict[str, Any]:
    """Cluster the recovered shapes and set them against our templates."""
    rows = [(r, *rs) for r in recs if (rs := record_shape(r)) is not None]
    if len(rows) < 4:
        raise ValueError(f"only {len(rows)} transitions pass the fit gate")
    X = np.stack([sh.ravel() for _, sh, _ in rows])
    tried = {kk: kmeans(X, kk) for kk in range(2, min(7, len(rows)))}
    sil = {kk: silhouette(X, lab) for kk, (lab, _) in tried.items()}
    k = k or max(sil, key=sil.get)
    lab, C = tried[k] if k in tried else kmeans(X, k)
    temps = {(t, b): template_shape(t, b) for t in TEMPLATE_TECHNIQUES for b in (8, 16, 32)}
    clusters = []
    for j in range(k):
        mem = [i for i in range(len(rows)) if lab[i] == j]
        cen = C[j].reshape(6, SHAPE_POINTS)
        bars = [rows[i][2] for i in mem]
        tb = nearest_bars(float(np.median(bars)))
        near = sorted(((distance_db(cen, temps[(t, tb)]), t) for t in TEMPLATE_TECHNIQUES))
        clusters.append({
            "cluster": j, "n": len(mem),
            "styles": dict(sorted(Counter(rows[i][0]["style"] for i in mem).items())),
            "bars_median": round(float(np.median(bars)), 1), "bars_iqr": _r(np.percentile(bars, [25, 75]), 1),
            "spread_db": round(float(np.mean([distance_db(rows[i][1], cen) for i in mem])), 2),
            "events": {e: round(v, 2) for e, v in events(cen).items()},
            "nearest_template": {"technique": near[0][1], "bars": tb, "distance_db": round(near[0][0], 2)},
            "centre": _r(cen, 3),
        })
    per = []
    for r, sh, bars in rows:  # each DJ transition against our nearest template at its own length
        tb = nearest_bars(bars)
        d = {t: distance_db(sh, temps[(t, tb)]) for t in TEMPLATE_TECHNIQUES}
        per.append((min(d.values()), d))
    styles: dict[str, Any] = {}
    for s in sorted({r["style"] for r, _, _ in rows}):
        idx = [i for i, (r, _, _) in enumerate(rows) if r["style"] == s]
        styles[s] = {"n": len(idx), "clusters": dict(sorted(Counter(int(lab[i]) for i in idx).items())),
                     "bars_median": round(float(np.median([rows[i][2] for i in idx])), 1),
                     "events": {e: round(float(np.median([events(rows[i][1])[e] for i in idx])), 2)
                                for e in events(rows[idx[0]][1])}}
    return {
        "transitions": len(rows), "gated_out": len(recs) - len(rows), "k": k,
        "silhouette": {kk: round(v, 3) for kk, v in sil.items()},
        "clusters": clusters, "styles": styles,
        "templates": {f"{t} {b}": {e: round(v, 2) for e, v in events(sh).items()} for (t, b), sh in temps.items()
                      if b == 16},
        "dj_to_nearest_template_db": round(float(np.mean([p[0] for p in per])), 2),
        "dj_to_own_cluster_db": round(float(np.mean([distance_db(rows[i][1], C[lab[i]].reshape(6, -1))
                                                     for i in range(len(rows))])), 2),
        "dj_to_mean_shape_db": round(float(np.mean([distance_db(sh, X.mean(0).reshape(6, -1))
                                                    for _, sh, _ in rows])), 2),
        "labels": [{"mix": r["mix"], "i": r["i"], "cluster": int(lab[i])} for i, (r, _, _) in enumerate(rows)],
    }



# --- picking a shape from our analysis -------------------------------------------------------------------------------

MODEL_FEATURES = ("log2_bars", "a_low", "b_low", "a_vocal", "b_vocal", "both_vocal", "a_energy", "b_energy",
                  "energy_delta", "b_in_intro", "a_in_outro", "tempo")


def _bars_of(an: dict, u0: float, u1: float) -> np.ndarray:
    b0 = int(math.floor((u0 - an["first_downbeat_s"]) / an["bar_s"]))
    b1 = int(math.ceil((u1 - an["first_downbeat_s"]) / an["bar_s"]))
    return np.arange(max(0, b0), min(an["bar_count"], max(b1, b0 + 1)))


def _mean(an: dict, key: str, bars: np.ndarray) -> float:
    v = np.asarray(an.get(key) or [], float)
    bars = bars[bars < len(v)]
    return float(v[bars].mean()) if bars.size else 0.0


def _in_section(an: dict, kind: str, bars: np.ndarray) -> float:
    sec = an.get(kind) or {}
    lo, hi = sec.get("start_bar"), sec.get("end_bar")
    if lo is None or hi is None or not bars.size:
        return 0.0
    return float(np.mean((bars >= lo) & (bars < hi)))


def features(rec: dict) -> np.ndarray | None:
    """What our analysis says about the overlap the DJ played (see `pair_features`)."""
    rs = record_shape(rec)
    if rs is None:
        return None
    _, bars = rs
    s0, s1 = rec["span"]
    t0, t1 = rec["t0"] + s0 * rec["hop_s"], rec["t0"] + s1 * rec["hop_s"]
    (oa, ra), (ob, rb) = rec["a_map"], rec["b_map"]
    return pair_features(rec["an_a"], rec["an_b"], (oa + ra * t0, oa + ra * t1), (ob + rb * t0, ob + rb * t1),
                         bars, rec["bpm"])


def pair_features(an_a: dict, an_b: dict, a_span: tuple[float, float], b_span: tuple[float, float], bars: float,
                  bpm: float) -> np.ndarray:
    """MODEL_FEATURES of an overlap: each track's span in its own time, from our analysis (musicreco.structure).
    The planner knows all of it before it writes lanes."""
    ba, bb = _bars_of(an_a, *a_span), _bars_of(an_b, *b_span)
    va, vb = _mean(an_a, "vocal_per_bar", ba), _mean(an_b, "vocal_per_bar", bb)
    ea, eb = _mean(an_a, "energy_per_bar", ba), _mean(an_b, "energy_per_bar", bb)
    return np.array([math.log2(max(bars, 1.0)), _mean(an_a, "low_per_bar", ba), _mean(an_b, "low_per_bar", bb),
                     va, vb, va * vb, ea, eb, eb - ea, _in_section(an_b, "intro", bb), _in_section(an_a, "outro", ba),
                     (bpm - 125.0) / 10.0])


def fit_softmax(X: np.ndarray, y: np.ndarray, k: int, l2: float = 1.0, steps: int = 2000, lr: float = 0.1
                ) -> np.ndarray:
    """Multinomial logit (weights (d + 1, k), the last row the bias) by gradient descent with an L2 penalty."""
    Xb = np.hstack([X, np.ones((len(X), 1))])
    W = np.zeros((Xb.shape[1], k))
    Y = np.eye(k)[y]
    for _ in range(steps):
        Z = Xb @ W
        P = np.exp(Z - Z.max(1, keepdims=True))
        P /= P.sum(1, keepdims=True)
        G = Xb.T @ (P - Y) / len(X)
        G[:-1] += l2 * W[:-1] / len(X)
        W -= lr * G
    return W


def predict(W: np.ndarray, X: np.ndarray) -> np.ndarray:
    return np.argmax(np.hstack([X, np.ones((len(X), 1))]) @ W, axis=1)


def model_report(recs: list[dict], k: int | None = None) -> dict[str, Any]:
    """Leave one mix out: does our analysis pick the DJ's shape better than always playing one shape?"""
    rows = [(r, *rs, f) for r in recs if (rs := record_shape(r)) is not None and (f := features(r)) is not None]
    rep = shapes_report([r for r, *_ in rows], k)
    k = rep["k"]
    lab = np.array([c["cluster"] for c in rep["labels"]])
    S = np.stack([sh for _, sh, _, _ in rows])
    X = np.stack([f for *_, f in rows])
    mixes = np.array([r["mix"] for r, *_ in rows])
    temps = {(t, b): template_shape(t, b) for t in TEMPLATE_TECHNIQUES for b in (8, 16, 32)}
    hits = base_hits = 0
    d_model, d_mean, d_temp, d_best_temp = [], [], [], []
    best_t = min(TEMPLATE_TECHNIQUES, key=lambda t: np.mean([distance_db(S[i], temps[(t, nearest_bars(rows[i][2]))])
                                                            for i in range(len(rows))]))
    for m in sorted(set(mixes)):
        tr, te = mixes != m, mixes == m
        mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-6
        # cluster centres from the training mixes only, with the labels the full clustering gave them
        cen = {j: S[tr & (lab == j)].mean(0) for j in range(k) if (tr & (lab == j)).any()}
        major = Counter(lab[tr].tolist()).most_common(1)[0][0]
        W = fit_softmax((X[tr] - mu) / sd, lab[tr], k)
        pred = predict(W, (X[te] - mu) / sd)
        for i, p in zip(np.flatnonzero(te), pred):
            p = int(p) if int(p) in cen else major
            hits += int(p == lab[i])
            base_hits += int(major == lab[i])
            d_model.append(distance_db(S[i], cen[p]))
            d_mean.append(distance_db(S[i], S[tr].mean(0)))
            d_temp.append(min(distance_db(S[i], temps[(t, nearest_bars(rows[i][2]))]) for t in TEMPLATE_TECHNIQUES))
            d_best_temp.append(distance_db(S[i], temps[(best_t, nearest_bars(rows[i][2]))]))
    n = len(rows)
    W = fit_softmax((X - X.mean(0)) / (X.std(0) + 1e-6), lab, k)
    return {
        "transitions": n, "mixes": len(set(mixes)), "k": k,
        "accuracy": round(hits / n, 3), "majority_accuracy": round(base_hits / n, 3),
        "distance_db": {"model_pick": round(float(np.mean(d_model)), 2),
                        "mean_shape": round(float(np.mean(d_mean)), 2),
                        f"template_{best_t}": round(float(np.mean(d_best_temp)), 2),
                        "nearest_template_oracle": round(float(np.mean(d_temp)), 2)},
        "weights": {f: _r(W[i], 2) for i, f in enumerate(MODEL_FEATURES)},
    }


def train(recs: list[dict], k: int | None = None) -> dict[str, Any]:
    """The prototype as plain JSON: the cluster centres (shapes) and the logit that picks one from MODEL_FEATURES."""
    rows = [(r, *rs, f) for r in recs if (rs := record_shape(r)) is not None and (f := features(r)) is not None]
    rep = shapes_report([r for r, *_ in rows], k)
    lab = np.array([c["cluster"] for c in rep["labels"]])
    X = np.stack([f for *_, f in rows])
    mu, sd = X.mean(0), X.std(0) + 1e-6
    W = fit_softmax((X - mu) / sd, lab, rep["k"])
    return {"version": 1, "features": list(MODEL_FEATURES), "mean": _r(mu, 4), "std": _r(sd, 4), "weights": _r(W, 4),
            "shapes": [c["centre"] for c in rep["clusters"]], "trained_on": len(rows)}


def pick(model: dict, x: np.ndarray) -> int:
    z = (np.asarray(x) - np.asarray(model["mean"])) / np.asarray(model["std"])
    return int(predict(np.asarray(model["weights"]), z[None])[0])


# --- the A/B: a template's plan with its fader and EQ lanes replaced by a learnt shape ---------------------------------


def shape_lanes(sh: np.ndarray, end_beat: float) -> dict[str, dict[str, list[dict]]]:
    """Fader and EQ keyframes for both decks that play shape `sh` over master beats [0, end_beat]: the fader is the
    loudest band, each EQ the band under it (an isolator only cuts). Before the shape A plays as it is and B is
    silent; after it A is out and B stays as the shape ends."""
    t = np.linspace(0.0, end_beat, sh.shape[1])
    amp = 10 ** (FLOOR_DB * (1.0 - np.asarray(sh)) / 20.0)  # level back to amplitude; 0 is the kill
    amp = np.where(np.asarray(sh) <= 0.0, 0.0, amp)
    out = {}
    for deck, rows in (("a", amp[:3]), ("b", amp[3:])):
        fader = rows.max(axis=0)
        eq = 20 * np.log10(np.maximum(rows, 1e-6) / np.maximum(fader, 1e-6))
        eq = np.clip(eq, FLOOR_DB, 0.0)
        lanes = {"volume": [{"beat": float(b), "value": round(float(v), 4), "curve": "linear"} for b, v in zip(t, fader)]}
        for k, name in enumerate(("eq_low", "eq_mid", "eq_high")):
            lanes[name] = [{"beat": float(b), "value": round(float(v), 2), "curve": "linear"} for b, v in zip(t, eq[k])]
        if deck == "a":
            lanes["volume"].append({"beat": float(end_beat) + 0.25, "value": 0.0, "curve": "hold"})
        else:
            lanes["volume"].insert(0, {"beat": -0.25, "value": 0.0, "curve": "linear"})
        out[deck] = lanes
    return out


def apply_shape(plan: dict, sh: np.ndarray) -> dict:
    """The plan with its blend's fader, EQ and filter lanes replaced by the shape, from B in (beat 0) to A out."""
    import copy

    p = copy.deepcopy(plan)
    end = next((m["beat"] for m in p.get("markers", []) if m.get("label") == "A out"), 4.0 * p["bars"])
    lanes = shape_lanes(sh, float(end))
    for deck in ("a", "b"):
        L = p[deck]["lanes"]
        L.update(lanes[deck])
        L["filter"] = [{"beat": float(p["window"]["start_beat"]), "value": 0.0, "curve": "hold"}]
        L["filter_res"] = [{"beat": float(p["window"]["start_beat"]), "value": 0.0, "curve": "hold"}]
    return p


def plan_features(plan: dict, an_a: dict, an_b: dict) -> np.ndarray:
    """MODEL_FEATURES of the overlap our planner chose (B in at beat 0, A out at the marker)."""
    end = next((m["beat"] for m in plan.get("markers", []) if m.get("label") == "A out"), 4.0 * plan["bars"])
    a, b = plan["a"], plan["b"]
    span_a = (float(a["beat0_s"]), float(a["beat0_s"]) + end * 60.0 / float(a["native_bpm"]))
    span_b = (float(b["beat0_s"]), float(b["beat0_s"]) + end * 60.0 / float(b["native_bpm"]))
    bpm = float(plan["tempo"][0]["value"])
    return pair_features(an_a, an_b, span_a, span_b, end / 4.0, bpm)


def listen(recs: list[dict], audio: Path, model: dict, folder: Path, n: int = 20, seed: int = 59,
           log: Any = print) -> list[dict]:
    """Render up to `n` of the DJs' own pairs twice into the #/listen folder: our engine's plan as it is
    ("template") and with the shape the model picks for it ("learnt"). Returns objective metrics of both."""
    from musicreco import mixrender

    from . import listening, mixes, mixmetrics

    pairs = sorted({(r["a"], r["b"]) for r in recs}, key=lambda ab: (hash_str(ab[0] + ab[1], seed), ab))
    version = listening.engine_versions()["seedbox1"]
    out = []
    for ida, idb in pairs:
        if len(out) >= n:
            break
        pa, pb = _audio(audio / "tracks", ida), _audio(audio / "tracks", idb)
        if pa is None or pb is None:
            continue
        try:
            plan, keys = mixes._seedbox1_plan(mixes.Pair(f"{ida}-{idb}", pa, pb))
        except (ValueError, OSError) as e:
            log(f"{ida} > {idb}: {e}")
            continue
        if plan["technique"] in ("quick cut", "drop swap", "slam") or plan["bars"] < 8:
            log(f"{ida} > {idb}: a {plan['technique']}, not a blend")
            continue
        an_a, an_b = mixes.analysis_of(mixes.Pair("", pa, pb))
        j = pick(model, plan_features(plan, an_a, an_b))
        learnt = apply_shape(plan, np.asarray(model["shapes"][j]))
        row = {"pair": f"{ida}-{idb}", "technique": plan["technique"], "bars": plan["bars"], "shape": j}
        name = f"curves {len(out) + 1:02}"
        for style, pl in (("template", plan), ("learnt", learnt)):
            r = mixrender.render_audio(pl, pa, pb, mixes.SR)
            listening.write_render(folder, name, version, style, r["mix"], r["sr"])
            m = mixmetrics.measure(mixmetrics.Render.from_mixrender(r, pl, keys))
            row[style] = {"masking": m["masking"], "loudness": m["loudness"], "bass": m["bass"],
                          "overlap_s": m["overlap"]["seconds"]}
        out.append(row)
        log(f"{name}: {ida} > {idb}, {plan['technique']} {plan['bars']} bars against shape {j}")
    return out


def hash_str(s: str, seed: int) -> int:
    import zlib

    return zlib.crc32(f"{seed}:{s}".encode())


# --- command line -----------------------------------------------------------------------------------------------------


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m seedbox2.eval curves", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract", help="recover the curves of a mix's transitions (audio downloaded already)")
    ex.add_argument("dataset", type=Path, help="djmix-dataset.json")
    ex.add_argument("audio", type=Path, help="folder with mixes/<mix id>.* and tracks/<youtube id>.*")
    ex.add_argument("out", type=Path, help="curves.jsonl (appended to)")
    ex.add_argument("--mix", nargs="+", required=True)
    sy = sub.add_parser("synth", help="recover our own templates' curves from renders of synthetic tracks")
    sy.add_argument("--seed", type=int, default=59)
    for name, what in (("shapes", "cluster the shapes and compare them with our templates"),
                       ("model", "leave-one-mix-out: pick the shape from our analysis")):
        sp = sub.add_parser(name, help=what)
        sp.add_argument("curves", type=Path)
        sp.add_argument("--k", type=int)
        sp.add_argument("--json", type=Path)
    ls = sub.add_parser("listen", help="render pairs, template against learnt shape, for the #/listen page")
    ls.add_argument("curves", type=Path)
    ls.add_argument("audio", type=Path)
    ls.add_argument("--folder", type=Path, help="default: the listening folder next to the library")
    ls.add_argument("--n", type=int, default=20)
    ls.add_argument("--k", type=int)
    ls.add_argument("--json", type=Path)
    a = p.parse_args(argv)
    if a.cmd == "extract":
        meta = {m["id"]: m for m in json.loads(a.dataset.read_text())}
        with a.out.open("a") as f:
            for mid in a.mix:
                for rec in extract_mix(meta[mid], a.audio, log=lambda s: print(s, file=sys.stderr)):
                    f.write(json.dumps(rec) + "\n")
        return 0
    if a.cmd == "synth":
        rng = np.random.default_rng(a.seed)
        rows = [synth_case(rng, t, b) for t in TEMPLATE_TECHNIQUES for b in (8, 16, 32)]
        for r in rows:
            e = r["band_error_db"]
            print(f"{r['technique']:13} {r['bars']:2} bars: fit {r['fit_db']:.2f} dB, "
                  f"A off by {e['band_a']:.2f} dB, B by {e['band_b']:.2f} dB")
        print(f"mean: A {np.mean([r['band_error_db']['band_a'] for r in rows]):.2f} dB, "
              f"B {np.mean([r['band_error_db']['band_b'] for r in rows]):.2f} dB")
        return 0
    recs = _read(a.curves)
    if a.cmd == "listen":
        from seedbox2.library import default_path

        from .listening import FOLDER

        rows = listen(recs, a.audio, train(recs, a.k), a.folder or default_path().parent / FOLDER, a.n,
                      log=lambda s: print(s, file=sys.stderr))
        rep: Any = rows
    else:
        rep = shapes_report(recs, a.k) if a.cmd == "shapes" else model_report(recs, a.k)
    text = json.dumps(rep, indent=1)
    if a.json:
        a.json.write_text(text)
    print(text if a.cmd != "shapes" else json.dumps({k: v for k, v in rep.items() if k != "labels"}, indent=1))
    return 0
