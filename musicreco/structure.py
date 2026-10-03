"""Track structure for DJ use: beat grid, bars, 8-bar phrases, sections, intro/outro, cues, 3-band waveform.

numpy/scipy only, ~0.5 s per 3-minute track. Method:
- tempo: the tunebat BPM (octave-corrected into 90-180) refined ±1.2 % by a phase-coherent comb over the whole
  onset envelope, snapped to an integer when that fits as well; another tempo only on overwhelming evidence.
- beat phase: the kick's attack start (sharpest > 150 Hz / > 1.5 kHz transient just before the low-band rise,
  folded over every beat at 0.5 ms), not the low band itself, which lags the attack by up to ~60 ms.
- downbeat: clap/snare backbeat parity, then sustained low-band entries (drops), bar novelty, first kick.
- sections: optimal segmentation of per-bar features with an 8-bar phrase-grid penalty; the phrase offset whose
  grid gives the cheapest segmentation wins. Labels from kick/bass/energy/content rules.

Field semantics beyond types.ts: bar 0 starts at first_downbeat_s, the first downbeat at most 3/4 beat before the
audio starts, so it can be slightly negative when a rip cuts into the first beat. A rip that starts partway into
a phrase shows up as phrase_offset != 0 (e.g. 7: the first full bar is the 2nd bar of an 8-bar phrase). bar_count
ends where the music does (a quiet decay tail is excluded). An intro/outro of 0 bars means the track starts/ends
full-on; its drums_only/has_vocals then describe the first/last 8 bars. vocal_per_bar is a heuristic (harmonic,
pitch-fluctuating 300 Hz-3.4 kHz energy): synth leads/arps can read as vocals, spoken or heavily chopped vocals
can be missed (once a track's vocal stem is split, musicreco.stems replaces it with the stem's own activity).
kick_per_bar is the kick's presence (0-1, beat-rate low-band pulses on the track's kick phase); bass_per_bar is how
much the low end fills the gaps between the kicks (0 = kick-only lows, 1 = a rolling bassline or a long sub that
never lets go; 0 where the bar has no real low end).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
from scipy import ndimage, signal
from scipy.fft import irfft, rfft

from .paths import ffmpeg_path

log = logging.getLogger(__name__)

ANALYSIS_VERSION = 5  # bump whenever the algorithm or output changes: older cache entries are recomputed
SR = 22050
DECODE_TIMEOUT_S = 300
MIN_DURATION_S = 8.0
WAVEFORM_PPS = 50
WAVEFORM_MAX_POINTS = 20000

_NFFT_A, _HOP_A = 1024, 256  # onsets / band energies, 86 fps
_NFFT_B, _HOP_B = 2048, 1024  # harmonic analysis on log-frequency bands, 21.5 fps
_EDGES_A = (30, 45, 60, 80, 105, 140, 180, 230, 300, 400, 520, 680, 880, 1150, 1500, 1950, 2550, 3300, 4300,
            5600, 7300, 9500, 11025)
_LOG_FMIN, _LOG_FMAX, _LOG_PER_OCT = 100.0, 5000.0, 24
_STFT_CHUNK = 4096
_SEG_LAMBDA = 6.0  # DP cost of one section boundary (feature units: 6 dB = 1)
_KICK_ON = 0.35  # per-bar kick presence above which the kick counts as playing


# ---------------------------------------------------------------------------------------------------------------
# decoding


def decode_mono(path: Path, sr: int = SR) -> np.ndarray:
    """Decode the first audio stream to mono float32 at `sr` via an ffmpeg pipe. ValueError on failure."""
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise ValueError("ffmpeg not found")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0",
           "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=DECODE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"could not decode {Path(path).name}: {e}") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise ValueError(f"could not decode {Path(path).name}: {tail[0]}")
    samples = np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)
    if samples.size == 0:
        raise ValueError(f"no audio in {Path(path).name}")
    return samples


# ---------------------------------------------------------------------------------------------------------------
# spectral front end


def _band_stft(x: np.ndarray, n_fft: int, hop: int, weights: np.ndarray, magnitude: bool) -> np.ndarray:
    """Banded STFT (frames, bands): power (or magnitude) @ weights. Frame t is centred on sample t*hop."""
    pad = n_fft // 2
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(x, (pad, pad)), n_fft)[::hop]
    win = np.hanning(n_fft + 1)[:-1].astype(np.float32)
    nbins = weights.shape[0]
    out = np.empty((len(frames), weights.shape[1]), np.float32)
    for i in range(0, len(frames), _STFT_CHUNK):
        spec = rfft(frames[i:i + _STFT_CHUNK] * win, axis=1, workers=2)[:, :nbins]
        power = spec.real ** 2 + spec.imag ** 2
        out[i:i + _STFT_CHUNK] = (np.sqrt(power) if magnitude else power) @ weights
    return out


def _rect_bands(edges: tuple[float, ...], n_fft: int, sr: int) -> np.ndarray:
    freqs = np.arange(n_fft // 2 + 1) * sr / n_fft
    nbins = int(np.searchsorted(freqs, edges[-1], side="right"))
    w = np.zeros((nbins, len(edges) - 1), np.float32)
    for j, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        sel = (freqs[:nbins] >= lo) & (freqs[:nbins] < hi)
        if not sel.any():
            sel[int(np.argmin(np.abs(freqs[:nbins] - (lo + hi) / 2)))] = True
        w[sel, j] = 1.0
    return w


def _log_bands(n_fft: int, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Triangular log-frequency filterbank (bins, bands) normalised per band, plus band centre frequencies."""
    n = int(np.floor(np.log2(_LOG_FMAX / _LOG_FMIN) * _LOG_PER_OCT)) + 1
    centres = _LOG_FMIN * 2.0 ** (np.arange(n) / _LOG_PER_OCT)
    df = sr / n_fft
    nbins = int(np.ceil(_LOG_FMAX * 2 ** (1 / _LOG_PER_OCT) / df)) + 2
    freqs = np.arange(nbins) * df
    half = np.maximum(centres * (2 ** (1 / _LOG_PER_OCT) - 1), df)
    w = np.maximum(0.0, 1.0 - np.abs(freqs[:, None] - centres[None, :]) / half[None, :])
    w /= w.sum(axis=0, keepdims=True)
    return w.astype(np.float32), centres


class _Front:
    """Everything derived from the spectrogram that later stages need."""

    def __init__(self, x: np.ndarray, sr: int) -> None:
        # A: band powers + onset envelopes
        self.fps_a = sr / _HOP_A
        self.pa = _band_stft(x, _NFFT_A, _HOP_A, _rect_bands(_EDGES_A, _NFFT_A, sr), magnitude=False)  # (frames, 22)
        ref = np.percentile(self.pa, 99, axis=0) + 1e-12
        la = 10 * np.log10(self.pa + ref * 1e-5)
        rise = np.maximum(np.diff(la, axis=0, prepend=la[:1]), 0.0)
        edges = np.array(_EDGES_A, float)
        centre = np.sqrt(edges[:-1] * edges[1:])
        self.o_full = rise.sum(axis=1)
        self.o_low = rise[:, centre < 150].sum(axis=1)
        self.band_centre_a = centre
        self.low_power = self.pa[:, centre < 150].sum(axis=1)
        # transient envelopes (> 150 Hz and > 1.5 kHz) at ~0.5 ms for a precise attack time
        dec = 11
        self.fs_t = sr / dec
        self.t0_t = (dec - 1) / 2 / sr
        self.attack = []
        for fc in (150, 1500):
            hp = signal.sosfilt(signal.butter(2, fc, "highpass", fs=sr, output="sos"), x)
            env = (hp[: hp.size // dec * dec] ** 2).reshape(-1, dec).mean(axis=1)
            env = ndimage.uniform_filter1d(env, 3)
            env_db = 10 * np.log10(env + np.percentile(env, 99) * 1e-6 + 1e-12)
            self.attack.append(np.maximum(env_db - np.concatenate([env_db[:2], env_db[:-2]]), 0.0))
        # B: log-frequency magnitudes for harmonic / vocal analysis
        self.fps_b = sr / _HOP_B
        wb, self.centres_b = _log_bands(_NFFT_B, sr)
        self.xb = _band_stft(x, _NFFT_B, _HOP_B, wb, magnitude=True)  # (frames, ~136)


# ---------------------------------------------------------------------------------------------------------------
# tempo + grid


def _comb(o: np.ndarray, fps: float, bpm0: float, rel: float, step: float = 5e-5) -> tuple[np.ndarray, np.ndarray]:
    """Phase-coherent beat-comb strength around bpm0 (±rel), harmonics 1/2/4, normalised by envelope mass."""
    blk = max(1, int(fps * 0.5))
    nblk = o.size // blk
    if nblk < 4:
        return np.array([bpm0]), np.zeros(1)
    n = nblk * blk
    t = np.arange(n) / fps
    tb = (np.arange(nblk) * blk + (blk - 1) / 2) / fps
    d = np.arange(-rel, rel + step / 2, step)
    f0 = bpm0 / 60
    score = np.zeros(d.size)
    for k, w in ((1, 1.0), (2, 0.5), (4, 0.25)):
        y = (o[:n] * np.exp(-2j * np.pi * k * f0 * t)).reshape(nblk, blk).sum(axis=1)
        score += w * np.abs(np.exp(-2j * np.pi * k * f0 * np.outer(d, tb)) @ y)
    return bpm0 * (1 + d), score / (o[:n].sum() + 1e-9)


def _refine(o: np.ndarray, fps: float, bpm0: float, rel: float) -> tuple[float, float]:
    bpms, sc = _comb(o, fps, bpm0, rel)
    i = int(np.argmax(sc))
    if 0 < i < sc.size - 1:
        a, b, c = sc[i - 1], sc[i], sc[i + 1]
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den < 0 else 0.0
        return float(bpms[i] + off * (bpms[1] - bpms[0])), float(b)
    return float(bpms[i]), float(sc[i])


def _acf_tempo(o: np.ndarray, fps: float) -> float:
    """Coarse tempo (90-180 BPM) from the onset autocorrelation with a log-normal prior around 125."""
    o = o - o.mean()
    nfft = 1 << int(np.ceil(np.log2(2 * o.size)))
    spec = rfft(o, nfft)
    ac = irfft(spec.real ** 2 + spec.imag ** 2, nfft)[: o.size]
    ac /= ac[0] + 1e-12
    bpms = np.arange(90.0, 180.0, 0.25)
    lags = 60 * fps / bpms
    idx = np.arange(ac.size)
    score = np.interp(lags, idx, ac) + 0.5 * np.interp(2 * lags, idx, ac) + 0.25 * np.interp(4 * lags, idx, ac)
    prior = np.exp(-0.5 * (np.log2(bpms / 125.0) / 0.5) ** 2)
    return float(bpms[int(np.argmax(score * prior))])


def _octave_into(bpm: float, lo: float = 90.0, hi: float = 180.0) -> float:
    while bpm < lo:
        bpm *= 2
    while bpm >= hi:
        bpm /= 2
    return bpm


def _tempo(front: _Front, bpm_hint: float | None, dbg: dict) -> tuple[float, str]:
    """Grid BPM and its source. The tunebat hint (octave-corrected into 90-180, refined ±1.2 %) wins unless the
    onsets overwhelmingly prefer another tempo: 2x the hint's comb strength for a musically related ratio (3:2,
    4:3), or for an unrelated one only when the hint fits nowhere (strength < 0.08) and the other is >= 3x and
    >= 0.15 (e.g. a YouTube version at another speed, or a wrong download)."""
    o, fps = front.o_full, front.fps_a
    det0 = _acf_tempo(o, fps)
    det, q_det = _refine(o, fps, det0, 0.02)
    dbg.update(acf_bpm=det0, det_bpm=det, q_det=q_det)
    if not bpm_hint or not (30 <= bpm_hint <= 400):
        return _snap(o, fps, det), "detected"
    hint = _octave_into(float(bpm_hint))
    bpm, q_hint = _refine(o, fps, hint, 0.012)
    at_edge = abs(bpm / hint - 1) > 0.0115  # the refinement ran into the window edge: the hint does not hold
    if at_edge:
        q_hint *= 0.5
    dbg.update(hint_bpm=bpm, q_hint=q_hint)
    ratio = det / hint
    if not at_edge and any(abs(ratio - r) < 0.012 * r for r in (0.5, 1.0, 2.0)):
        return _snap(o, fps, bpm), "tunebat"
    related = any(abs(ratio - r) < 0.01 * r for r in (1.5, 4 / 3, 2 / 3, 0.75))
    if (related and q_det >= 2 * q_hint) or (q_hint < 0.08 and q_det >= max(3 * q_hint, 0.15)):
        return _snap(o, fps, det), "detected"
    return _snap(o, fps, bpm), "tunebat"


def _snap(o: np.ndarray, fps: float, bpm: float) -> float:
    """Prefer an integer (or .5) BPM when it fits the onsets practically as well as the free estimate."""
    _, sc = _comb(o, fps, bpm, 0.0, 1.0)
    best = float(sc[0])
    for cand in (round(bpm), round(bpm * 2) / 2):
        if abs(cand - bpm) <= 0.06:
            _, sc_c = _comb(o, fps, float(cand), 0.0, 1.0)
            if sc_c[0] >= 0.985 * best:
                return float(cand)
    return round(bpm, 3)


def _fold(values: np.ndarray, times: np.ndarray, period: float, bins: int) -> np.ndarray:
    """Mean of `values` per phase bin of `period` (circular profile)."""
    b = ((times % period) / period * bins).astype(int) % bins
    cnt = np.bincount(b, minlength=bins)
    return np.bincount(b, weights=values, minlength=bins) / np.maximum(cnt, 1)


def _phase(front: _Front, period: float, dbg: dict) -> float:
    """Time of one beat (kick attack start) in seconds, modulo `period`.

    The low band locates the kick (it lags the attack by up to ~60 ms: pitch sweep, sidechain), then the
    sharpest broadband transient just before that rise is the attack.
    """
    bins = max(16, int(round(period / 0.0025)))
    t = np.arange(front.low_power.size) / front.fps_a
    prof = _fold(front.low_power, t, period, bins)
    prof_db = 10 * np.log10(prof + 1e-12)
    prof_db = ndimage.gaussian_filter1d(prof_db, 1.5, mode="wrap")
    k = max(1, int(round(0.01 / (period / bins))))
    slope = np.roll(prof_db, -k) - np.roll(prof_db, k)
    phi_low = (int(np.argmax(slope)) + 0.5) * period / bins
    # fine: transient onsets folded at 0.5 ms in [phi_low - 80 ms, phi_low + 25 ms]: the strongest peak, or an
    # almost as prominent one up to 30 ms before it (a kick's click can lead its 150-500 Hz body)
    fb = max(64, int(round(period / 0.0005)))
    ta = front.t0_t + np.arange(front.attack[0].size) / front.fs_t
    att = np.zeros(fb)
    for a in front.attack:
        prof_a = ndimage.gaussian_filter1d(_fold(a, ta, period, fb), 1.0, mode="wrap")
        att += prof_a / (np.median(prof_a) + 1e-9)
    att /= len(front.attack)
    centres = (np.arange(fb) + 0.5) * period / fb
    rel = (centres - phi_low + period / 2) % period - period / 2
    cand = np.nonzero((rel >= -0.08) & (rel <= 0.025))[0]
    cand = cand[np.argsort(rel[cand])]
    vals = att[cand] - 1.0  # prominence over the (normalised) baseline
    j = int(np.argmax(vals))
    peak = float(vals[j])
    is_max = (vals >= np.roll(vals, 1)) & (vals >= np.roll(vals, -1))
    early = np.nonzero(is_max & (vals >= 0.7 * peak) & (rel[cand] >= rel[cand[j]] - 0.03)
                       & (np.arange(vals.size) < j))[0]
    if early.size:
        j = int(early[0])
    i = int(cand[j])
    a, b, c = att[(i - 1) % fb], att[i], att[(i + 1) % fb]
    den = a - 2 * b + c
    off = 0.5 * (a - c) / den if den < 0 else 0.0
    phi = (i + 0.5 + off) * period / fb
    dbg.update(phase_low=phi_low, phase_fine=phi % period, phase_sharp=peak)
    if peak < 0.5:  # no clear transient: fall back to the low-band rise, nudged to its typical lead
        phi = phi_low - 0.02
    return phi % period


def _sample_max(env: np.ndarray, fps: float, times: np.ndarray, before: float, after: float,
                t0: float = 0.0) -> np.ndarray:
    """Max of `env` in [t - before, t + after] for each time."""
    lo = np.clip(np.round((times - before - t0) * fps).astype(int), 0, env.size - 1)
    hi = np.clip(np.round((times + after - t0) * fps).astype(int) + 1, 1, env.size)
    width = int(np.max(hi - lo)) if lo.size else 1
    idx = np.clip(lo[:, None] + np.arange(max(width, 1))[None, :], 0, env.size - 1)
    vals = env[idx]
    vals[np.arange(max(width, 1))[None, :] >= (hi - lo)[:, None]] = 0.0
    return vals.max(axis=1)


def _interval_mean(values: np.ndarray, fps: float, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """Mean of frame rows over [start, end) time intervals (rows: frames)."""
    csum = np.concatenate([np.zeros((1,) + values.shape[1:]), np.cumsum(values, axis=0, dtype=np.float64)])
    a = np.clip(np.round(starts * fps).astype(int), 0, values.shape[0])
    b = np.clip(np.round(ends * fps).astype(int), 0, values.shape[0])
    b = np.maximum(b, np.minimum(a + 1, values.shape[0]))
    a = np.minimum(a, b - 1).clip(0)
    cnt = (b - a).reshape((-1,) + (1,) * (values.ndim - 1))
    return (csum[b] - csum[a]) / np.maximum(cnt, 1)


def _downbeat(front: _Front, beats: np.ndarray, kick: np.ndarray, dbg: dict) -> int:
    """Index (0-3) of the first downbeat among `beats`.

    1. Backbeat parity: claps/snares land on beats 2 and 4, so when that pattern is clear the downbeat is one of
       the two non-clap positions (this also rules out one-beat pickups, which sit on beat 4).
    2. Sustained low-band entries (drops, bass/kick coming back) start bars: if one candidate clearly gets most
       of the entry energy it wins, else bar-level novelty plus the first kick (intros start on beat 1) decide.
    """
    if beats.size < 24:
        return 0
    n = beats.size - 1
    # clap/snare band (1.1-5.6 kHz) power rise across each beat: [-120, -20] ms -> [0, +100] ms
    ca = front.band_centre_a
    clap = front.pa[:, (ca > 1100) & (ca < 5600)].sum(axis=1)
    pre = _interval_mean(clap, front.fps_a, beats[:n] - 0.12, beats[:n] - 0.02)
    post = _interval_mean(clap, front.fps_a, beats[:n], beats[:n] + 0.10)
    sn = np.maximum(10 * np.log10((post + 1e-12) / (pre + 1e-12)), 0.0)
    sn_pos = np.array([np.mean(sn[j::4]) for j in range(4)])
    contrast = float(sn_pos[1] + sn_pos[3] - sn_pos[0] - sn_pos[2]) / 2  # dB; > 0: claps on odd -> even downbeat
    period = float(np.median(np.diff(beats)))
    inner = (beats[:-1] + 0.1 * period, beats[:-1] + 0.9 * period)  # no leakage from the neighbouring beats' kicks
    low = 10 * np.log10(_interval_mean(front.low_power, front.fps_a, *inner) + 1e-12)
    c = np.concatenate([[0.0], np.cumsum(low)])
    k = np.arange(4, n - 3)
    jump = np.zeros(n)
    jump[k] = (c[k + 4] - c[k]) / 4 - (c[k] - c[k - 4]) / 4
    entries = np.maximum(jump - 10, 0) ** 2
    ent = np.array([entries[j::4].sum() for j in range(4)])
    ent = ent / (ent.sum() + 1e-9)
    bp = _interval_mean(front.pa, front.fps_a, *inner)
    feats = 10 * np.log10(bp + np.percentile(front.pa, 99, axis=0) * 1e-4)
    feats = (feats - feats.mean(axis=0)) / (feats.std(axis=0) + 1e-6)
    cf = np.concatenate([np.zeros((1, feats.shape[1])), np.cumsum(feats, axis=0)])
    nov = np.zeros(n)
    nov[k] = np.linalg.norm((cf[k + 4] - cf[k]) - (cf[k] - cf[k - 4]), axis=1) / 4
    nov_s = np.array([np.sum(nov[j::4] ** 2) for j in range(4)])
    nov_s = nov_s / (nov_s.sum() + 1e-9)
    strong = np.nonzero(kick[:n] > 0.5 * np.percentile(kick, 90))[0]
    prior = np.zeros(4)
    if strong.size:
        prior[strong[0] % 4] = 1.0
    allowed = np.ones(4, bool)
    if abs(contrast) >= 3 and sn_pos.max() >= 5:  # clear backbeat: the clap positions cannot be the downbeat
        allowed[(np.arange(4) % 2) == (0 if contrast < 0 else 1)] = False
    cand = np.nonzero(allowed)[0]
    e = ent[cand] / (ent[cand].sum() + 1e-9)
    order = np.argsort(-e)
    if e[order[0]] >= 0.45 and e[order[0]] >= 1.5 * e[order[1]]:  # decisive entries
        score = np.where(allowed, ent, -np.inf)
    else:
        back = np.array([contrast, -contrast, contrast, -contrast])  # dB of clap rise on the off-beats of j
        score = np.where(allowed, 4 * (nov_s - 0.25) + 0.5 * ent + 0.1 * np.clip(back, -5, 5) + 0.15 * prior,
                         -np.inf)
    dbg.update(db_entries=ent.round(3).tolist(), db_nov=nov_s.round(3).tolist(), db_backbeat=round(contrast, 3),
               db_clap=sn_pos.round(2).tolist(),
               db_total=np.where(np.isfinite(score), score, -9).round(3).tolist())
    return int(np.argmax(score))


# ---------------------------------------------------------------------------------------------------------------
# harmonic / vocal features


def _harmonic_features(front: _Front) -> dict[str, np.ndarray]:
    """Per-frame (fps_b) harmonic mid-band level, fluctuating-harmonic ('vocal-like') level and harmonic flux."""
    xb = front.xb
    h = ndimage.median_filter(xb, size=(9, 1), mode="nearest")
    p = ndimage.median_filter(xb, size=(1, 13), mode="nearest")
    mask = h * h / (h * h + p * p + 1e-12)
    xh = xb * mask
    # sustained part of the harmonic layer (pads, held notes): ~1.4 s median on 2x-decimated frames
    nf = xh.shape[0] // 2 * 2
    half = 0.5 * (xh[:nf:2] + xh[1:nf:2]) if nf else xh
    sus = ndimage.median_filter(half, size=(15, 1), mode="nearest")
    sus = np.repeat(sus, 2, axis=0)
    if sus.shape[0] < xh.shape[0]:
        sus = np.concatenate([sus, sus[-1:]], axis=0)
    fluct = np.maximum(xh - sus[: xh.shape[0]], 0.0)
    mid = (front.centres_b >= 300) & (front.centres_b <= 3400)
    e_all = np.sum(xb[:, mid] ** 2, axis=1)
    e_h = np.sum(xh[:, mid] ** 2, axis=1)
    e_f = np.sum(fluct[:, mid] ** 2, axis=1)
    xh_m = xh[:, mid]
    diff = np.abs(np.diff(xh_m, axis=0, prepend=xh_m[:1]))
    flux = diff.sum(axis=1) / (xh_m.sum(axis=1) + np.roll(xh_m, 1, axis=0).sum(axis=1) + 1e-9)
    return {"e_mid": e_all, "e_harm": e_h, "e_fluct": e_f, "flux": flux}


# ---------------------------------------------------------------------------------------------------------------
# main analysis


def _db_to_unit(db: np.ndarray, ref: float, span: float) -> np.ndarray:
    return np.clip((db - (ref - span)) / span, 0.0, 1.0)


def _analyze_samples(x: np.ndarray, sr: int, bpm_hint: float | None, track_id: str = "",
                     dbg: dict | None = None) -> dict:
    dbg = {} if dbg is None else dbg
    duration = x.size / sr
    if duration < MIN_DURATION_S:
        raise ValueError(f"audio too short ({duration:.1f} s)")
    if not np.all(np.isfinite(x)) or float(np.max(np.abs(x))) < 1e-4:
        raise ValueError("audio is silent or corrupt")
    front = _Front(x, sr)

    bpm, source = _tempo(front, bpm_hint, dbg)
    bpm = round(bpm, 3)
    period = 60.0 / bpm
    phase = _phase(front, period, dbg)

    # audible span (frame RMS within 40 dB of the loud level)
    frame_db = 10 * np.log10(front.pa.sum(axis=1) + 1e-12)
    loud = np.percentile(frame_db, 95)
    audible = np.nonzero(frame_db > loud - 40)[0]
    t_start = audible[0] / front.fps_a if audible.size else 0.0
    t_end = (audible[-1] + 1) / front.fps_a if audible.size else duration

    b0 = phase % period - period
    beats = b0 + period * np.arange(int((duration - b0) / period) + 1)
    kick = _sample_max(front.o_low, front.fps_a, beats, 0.015, 0.08)
    d = _downbeat(front, beats, kick, dbg)
    downbeats = beats[d::4]
    # bar 0: first downbeat at most 3/4 beat before the audio starts (a bar opening on an off-beat hat counts)
    first_db = float(downbeats[np.searchsorted(downbeats, t_start - 0.75 * period)])
    bar_s = 4 * period
    bar_count = max(1, int(np.floor((t_end - first_db) / bar_s + 0.75)))

    feats = _bar_features(front, first_db, period, bar_count)
    # the music ends at the last bar within 20 dB of the loud level (a decay tail is not part of the outro)
    keep = np.nonzero(feats["e_db"] >= np.percentile(feats["e_db"], 90) - 20)[0]
    if keep.size and keep[-1] + 1 < bar_count:
        bar_count = int(keep[-1]) + 1
        feats = {k: v[:bar_count] for k, v in feats.items()}
    dbg.update(beats=beats, downbeat_index=d, bpm=bpm, source=source, front=front, t_start=t_start, t_end=t_end,
               bar_feats=feats, **feats)
    phrase_off, sections = _structure(feats, dbg)
    e_db, vocal_bar = feats["e_db"], feats["vocal"]
    energy_bar = _db_to_unit(e_db, float(np.percentile(e_db, 95)), 18.0)
    low_bar = _db_to_unit(feats["low_db"], float(np.percentile(feats["low_db"], 95)), 24.0)
    kick_bar = np.clip(feats["kick"], 0.0, 1.0)
    # between-kick low level vs the bar's whole low level: -15 dB (kick-only) .. -3 dB (the bass never lets go)
    bass_bar = np.clip((feats["bass_db"] - feats["low_db"] + 15.0) / 12.0, 0.0, 1.0) * (low_bar >= 0.2)

    def t(bar: int) -> float:
        return round(min(first_db + bar * bar_s, duration), 3)

    sec_out = [{"kind": k, "start_bar": a, "end_bar": b, "start_s": t(a), "end_s": t(b)} for k, a, b in sections]
    intro, outro, cues = _edges_and_cues(sec_out, feats, bar_count, phrase_off, t)

    pps = min(float(WAVEFORM_PPS), WAVEFORM_MAX_POINTS / duration)
    wave = waveform_peaks(x, sr, max(1, int(round(duration * pps))))

    return {
        "version": ANALYSIS_VERSION,
        "track_id": track_id,
        "duration_s": round(duration, 3),
        "bpm": bpm,
        "bpm_source": source,
        "first_downbeat_s": round(first_db, 4),
        "bar_s": round(bar_s, 6),
        "bar_count": int(bar_count),
        "phrase_offset": int(phrase_off),
        "energy_per_bar": _r(energy_bar),
        "low_per_bar": _r(low_bar),
        "vocal_per_bar": _r(vocal_bar),
        "kick_per_bar": _r(kick_bar),
        "bass_per_bar": _r(bass_bar),
        "sections": sec_out,
        "intro": intro,
        "outro": outro,
        "cues": cues,
        "waveform": wave,
    }


def _bar_features(front: _Front, first_db: float, period: float, bar_count: int) -> dict[str, np.ndarray]:
    """Per-bar descriptors (dB levels, kick presence, harmonic/vocal measures) for bars from `first_db`."""
    bar_s = 4 * period
    starts = first_db + bar_s * np.arange(bar_count)
    ends = starts + bar_s
    pa_bar = _interval_mean(front.pa, front.fps_a, starts, ends)
    ca = front.band_centre_a

    def level(sel: np.ndarray) -> np.ndarray:
        return 10 * np.log10(pa_bar[:, sel].sum(axis=1) + 1e-12)

    out = {"e_db": level(ca > 40), "low_db": level(ca < 150), "mid_db": level((ca >= 150) & (ca < 2500)),
           "high_db": level(ca >= 2500)}
    beat_times = first_db + period * np.arange(bar_count * 4)
    onset = _sample_max(front.o_low, front.fps_a, beat_times, 0.015, 0.08)
    out["kick_onset"] = np.clip(onset.reshape(bar_count, 4) / (np.percentile(onset, 90) + 1e-9), 0, 1.2).mean(axis=1)
    # beat-rate modulation of the low band, projected on the track's kick phase (off-beat bass counts negative)
    t = np.arange(front.low_power.size) / front.fps_a
    lp = front.low_power.astype(np.float64)
    rot = lp * np.exp(-2j * np.pi * (t - first_db) / period)
    ref = np.sum(rot)
    ref = ref / (abs(ref) + 1e-12)
    zc = np.concatenate([[0], np.cumsum(rot)])
    pc = np.concatenate([[0], np.cumsum(lp)])
    ia = np.clip(np.round(starts * front.fps_a).astype(int), 0, lp.size)
    ib = np.clip(np.round(ends * front.fps_a).astype(int), 0, lp.size)
    depth = np.real((zc[ib] - zc[ia]) * np.conj(ref)) / (pc[ib] - pc[ia] + 1e-12)
    out["kick_mod"] = depth
    loud = out["low_db"] > np.percentile(out["low_db"], 95) - 30
    ref_depth = np.percentile(depth[loud], 90) if loud.any() else 1.0
    out["kick"] = np.clip(np.maximum(depth / (ref_depth + 1e-9), out["kick_onset"]) * loud, 0, 1.2)
    # low-band level between the kicks (second half of each beat): bass presence without the kick
    off = _interval_mean(front.low_power, front.fps_a, beat_times + 0.45 * period, beat_times + 0.95 * period)
    out["bass_db"] = 10 * np.log10(off.reshape(bar_count, 4).mean(axis=1) + 1e-12)
    hf = _harmonic_features(front)
    hb = {k: _interval_mean(v, front.fps_b, starts, ends) for k, v in hf.items()}
    out["harm_db"] = 10 * np.log10(hb["e_harm"] + 1e-12)
    out["fluct_ratio"] = hb["e_fluct"] / (hb["e_mid"] + 1e-12)
    out["fluct_db"] = 10 * np.log10(hb["e_fluct"] + 1e-12)
    out["flux"] = hb["flux"]
    out["vocal"] = _vocal_score(out["fluct_ratio"], out["fluct_db"] - float(np.percentile(out["harm_db"], 95)),
                                out["flux"])
    return out


def _r(a: np.ndarray, nd: int = 3) -> list[float]:
    return [round(float(v), nd) for v in a]


def _vocal_score(fluct_ratio: np.ndarray, fluct_rel_db: np.ndarray, flux: np.ndarray) -> np.ndarray:
    """Heuristic vocal likelihood per bar from the fluctuating-harmonic share of the 300 Hz-3.4 kHz band."""
    a = 1 / (1 + np.exp(-(fluct_ratio - 0.10) / 0.03))
    b = 1 / (1 + np.exp(-(fluct_rel_db + 12) / 2.5))
    c = 1 / (1 + np.exp(-(flux - 0.25) / 0.05))
    return np.clip(a * b * (0.5 + 0.5 * c), 0, 1)


def _grid_cost(r: np.ndarray) -> np.ndarray:
    """Misalignment cost of bar offsets r (mod 8): phrase 0, half-phrase 0.25, even 1, odd 2."""
    r = r % 8
    return np.where(r == 0, 0.0, np.where(r == 4, 0.25, np.where(r % 2 == 0, 1.0, 2.0)))


def _segment(g: np.ndarray, lam: float) -> tuple[int, list[int]]:
    """Optimal piecewise-constant segmentation of bar features `g` (bars, feats) on an 8-bar phrase grid.

    A boundary costs `lam` x (1 + misalignment), the misalignment being the smaller of its offset from the global
    phrase grid (weighted up near the intro) and its segment's length off a phrase multiple (tracks may shift their
    phrasing later on). Segments under 4 bars (fills, drop-outs, pickups) cost extra and are folded into the
    section they end. Pass 1 segments without a global grid; the phrase offset is where its strongest (and earliest)
    boundaries land; pass 2 re-segments on that grid. Returns (phrase_offset, boundaries [0, ..., n]).
    """
    n = g.shape[0]
    if n < 8:
        return 0, [0, n]
    c1 = np.concatenate([np.zeros((1, g.shape[1])), np.cumsum(g, axis=0)])
    c2 = np.concatenate([[0.0], np.cumsum(np.sum(g * g, axis=1))])
    i = np.arange(n + 1)
    length = np.maximum(i[None, :] - i[:, None], 1)
    sse = (c2[None, :] - c2[:, None]) - np.sum((c1[None, :, :] - c1[:, None, :]) ** 2, axis=2) / length
    sse += np.where(length < 4, 0.5 * lam, 0.0)
    sse[np.tril_indices(n + 1)] = np.inf  # segments need start < end
    early = 1 + 3 * np.exp(-i / 32)  # the intro's phrasing matters most for mixing
    local = np.where(length % 4 == 0, 0.7 * _grid_cost(length), np.inf)  # only whole (half-)phrases count locally

    def solve(off: int | None) -> list[int]:
        glob = np.ones(n + 1) if off is None else _grid_cost(i - off) * early  # None: no global grid yet
        pen = lam * (1 + np.minimum(glob[None, :], local))
        pen[:, n] = 0.0
        best = np.full(n + 1, np.inf)
        best[0] = 0.0
        prev = np.zeros(n + 1, int)
        for j in range(1, n + 1):
            tot = best[:j] + sse[:j, j] + pen[:j, j]
            k = int(np.argmin(tot))
            best[j], prev[j] = tot[k], k
        bounds = [n]
        while bounds[-1] > 0:
            bounds.append(int(prev[bounds[-1]]))
        merged = [0]
        for b in bounds[::-1][1:]:
            if b - merged[-1] < 4 and b != n and len(merged) > 1:
                merged[-1] = b  # short segment: absorbed by the section it ends
            elif b - merged[-1] < 4 and b != n:
                continue  # short first segment (pickup): part of the next one
            else:
                merged.append(b)
        if len(merged) > 2 and n - merged[-2] < 4:
            merged.pop(-2)  # short tail joins the last section
        return merged

    bounds = solve(None)
    score = np.zeros(8)
    score[0] = 0.5
    for b in bounds[1:-1]:
        lo, hi = max(0, b - 4), min(n, b + 4)
        jump = float(np.linalg.norm((c1[hi] - c1[b]) / (hi - b) - (c1[b] - c1[lo]) / (b - lo)))
        w = jump * (1 + 3 * np.exp(-b / 32))
        score[b % 8] += w
        score[(b + 4) % 8] += 0.3 * w
    off = int(np.argmax(score))
    return off, solve(off)


def _structure(f: dict[str, np.ndarray], dbg: dict) -> tuple[int, list[tuple[str, int, int]]]:
    """Phrase offset and labelled sections [(kind, start_bar, end_bar)] from per-bar features."""
    # fixed physical scales (6 dB, 0.4 kick, 0.5 vocal = 1 unit) so a change means the same in every track
    db = [np.maximum(f[k], np.percentile(f[k], 95) - 30) / 6 for k in ("e_db", "low_db", "mid_db", "high_db",
                                                                       "harm_db")]
    g = np.column_stack(db + [f["kick"] / 0.4, f["vocal"] / 0.5]) * np.array([1.0, 1.0, 0.7, 0.7, 0.7, 1.0, 0.6])
    phrase_off, bounds = _segment(g, lam=_SEG_LAMBDA)
    dbg.update(bounds=bounds, phrase_off=phrase_off)
    return phrase_off, _refine_edges(_label(bounds, f), f, phrase_off)


def _fullness(f: dict[str, np.ndarray]) -> np.ndarray:
    """Per-bar arrangement fullness relative to the main groove (0 = groove level, negative = layers missing):
    harmonic/melodic mid, top end, bass between the kicks and vocals, ~1 unit per 6 dB / 8 dB / 0.3 vocal."""
    kick_on = f["kick"] >= _KICK_ON
    groove = kick_on & (f["low_db"] >= np.percentile(f["low_db"], 90) - 6)
    groove &= f["e_db"] >= np.percentile(f["e_db"], 90) - 4
    ref = groove if groove.sum() >= 8 else np.ones(groove.size, bool)
    h = np.clip(f["harm_db"] - np.percentile(f["harm_db"][ref], 75), -15, 3) / 6
    hi = np.clip(f["high_db"] - np.percentile(f["high_db"][ref], 75), -15, 3) / 6
    bass = np.clip(f["bass_db"] - np.median(f["bass_db"][ref]), -20, 3) / 8
    voc = np.clip(f["vocal"] - np.percentile(f["vocal"][ref], 75), -0.6, 0.2) / 0.3
    return ndimage.median_filter(h + hi + bass + 0.5 * voc, 3, mode="nearest")


def _stripped_edge(full: np.ndarray, kick_on: np.ndarray, vocal: np.ndarray, sections: list[tuple[str, int, int]],
                   phrase_off: int, tail: bool) -> int:
    """Bars of the trailing (leading) stretch, starting on the phrase grid, whose arrangement is clearly thinner
    than the groove next to it and has no vocals - leads, vocals or bass gone while the beat may well go on (an
    extended mix's DJ outro/intro). Searched only inside the last (first) run of sections without a real breakdown,
    8-64 bars; 0 if none."""
    n = full.size
    lim = n if tail else 0
    for kind, a, b in (sections[::-1] if tail else sections):
        if kind in ("breakdown", "build") and b - a >= 8:
            break
        lim = a if tail else b
    best, best_len = 0.0, 0
    for s in range(phrase_off % 4, n + 1, 4):  # split bars on the (half-)phrase grid
        length = n - s if tail else s
        if not 8 <= length <= 64 or (tail and s < lim) or (not tail and s > lim):
            continue
        idx = np.arange(max(0, s - 16), s) if tail else np.arange(s, min(n, s + 16))
        idx = idx[kick_on[idx]]
        if idx.size < 4:
            continue
        groove = float(full[idx].mean())
        edge = full[s:] if tail else full[:s]
        drop = groove - float(edge.mean())
        fullest = float(edge[: edge.size // 4 * 4].reshape(-1, 4).mean(axis=1).max())
        if drop < 0.8 or fullest > groove - 0.5:
            continue  # not thinner, or the full arrangement comes back inside the edge
        if np.mean((vocal[s:] if tail else vocal[:s]) >= 0.5) >= 0.25:
            continue  # vocals still running: not an outro/intro to mix over
        score = drop + 0.5 * (groove - fullest) + 0.2 * ((s - phrase_off) % 8 == 0) + 0.004 * length
        if score > best:
            best, best_len = score, length
    return best_len


def _refine_edges(sections: list[tuple[str, int, int]], f: dict[str, np.ndarray],
                  phrase_off: int) -> list[tuple[str, int, int]]:
    """Extend the outro/intro over a stripped-down tail/head (see _stripped_edge). A radio edit that stays full to
    the end keeps no outro."""
    full, kick_on, n = _fullness(f), f["kick"] >= _KICK_ON, f["e_db"].size
    out_len = _stripped_edge(full, kick_on, f["vocal"], sections, phrase_off, tail=True)
    cur_out = sections[-1][1] if sections[-1][0] == "outro" else n
    if out_len and n - out_len < cur_out:
        s = n - out_len
        keep = [(k, a, min(b, s)) for k, a, b in sections if a < s]
        if len(keep) > 1 and keep[-1][2] - keep[-1][1] < 4:  # a sliver before the outro joins its predecessor
            keep = keep[:-2] + [(keep[-2][0], keep[-2][1], s)]
        sections = keep + [("outro", s, n)]
    in_len = _stripped_edge(full, kick_on, f["vocal"], sections, phrase_off, tail=False)
    cur_in = sections[0][2] if sections[0][0] == "intro" else 0
    out_start = sections[-1][1] if sections[-1][0] == "outro" else n
    if in_len > cur_in and in_len <= out_start - 4:
        e = in_len
        keep = [(k, max(a, e), b) for k, a, b in sections if b > e]
        if len(keep) > 1 and keep[0][2] - keep[0][1] < 4:
            keep = [(keep[1][0], e, keep[1][2])] + keep[2:]
        sections = [("intro", 0, e)] + keep
    merged: list[tuple[str, int, int]] = []
    for kind, a, b in sections:
        if merged and merged[-1][0] == kind:
            merged[-1] = (kind, merged[-1][1], b)
        else:
            merged.append((kind, a, b))
    return merged


def _label(bounds: list[int], f: dict[str, np.ndarray]) -> list[tuple[str, int, int]]:
    """intro/outro = leading/trailing segments without the full arrangement; breakdown = kick out, or bass out at
    lower energy; build = rising run-up into a full section (also split off the end of a breakdown); drop = full
    section after a breakdown/build; everything else body."""
    e_db, low_db, kick, high_db, harm_db, vocal = (f[k] for k in ("e_db", "low_db", "kick", "high_db", "harm_db",
                                                                 "vocal"))
    e_ref, low_ref = float(np.percentile(e_db, 90)), float(np.percentile(low_db, 90))
    kick_on = kick >= _KICK_ON
    groove = kick_on & (low_db >= low_ref - 6) & (e_db >= e_ref - 4)
    harm_main = float(np.median(harm_db[groove])) if groove.sum() >= 4 else float(np.percentile(harm_db, 75))
    rise = np.maximum(e_db, high_db - (np.median(high_db) - np.median(e_db)))  # energy or (offset) high band

    def mean(a: np.ndarray, s: tuple[int, int]) -> float:
        return float(np.mean(a[s[0]:s[1]]))

    def rising(s: tuple[int, int]) -> bool:
        a, b = s
        if b - a < 3:
            return False
        sl = float(np.polyfit(np.arange(b - a), rise[a:b], 1)[0])
        return sl > 0.35 and float(np.mean(rise[b - 2:b]) - np.mean(rise[a:a + 2])) > 3.0

    segs = list(zip(bounds[:-1], bounds[1:]))
    # split a rising 4/8-bar tail off long kick-less segments (riser / snare roll before the drop)
    split: list[tuple[int, int]] = []
    for a, b in segs:
        tail = next((k for k in (8, 4) if b - a >= k + 4 and mean(kick_on, (a, b)) < 0.4 and rising((b - k, b))
                     and not rising((a, b - k))), 0)
        split += [(a, b - tail), (b - tail, b)] if tail else [(a, b)]
    segs = split
    full = [mean(kick_on, s) >= 0.6 and mean(low_db, s) >= low_ref - 6 and mean(e_db, s) >= e_ref - 5 for s in segs]

    def main(i: int, edge: tuple[int, int]) -> bool:
        """The track proper: kick on and either the full drums+bass level or clearly more content than the edge
        segment (which itself needs musical content too: a drums+bass opening at full level is still an intro)."""
        s = segs[i]
        if mean(kick_on, s) < 0.6:
            return False
        h, v = mean(harm_db, s), mean(vocal, s)
        if s == edge:
            return full[i] and (h >= harm_main - 6 or v >= 0.3)
        return full[i] or (v >= 0.3 and v >= mean(vocal, edge) + 0.15) or h >= mean(harm_db, edge) + 6

    kinds = ["body"] * len(segs)
    # intro / outro: leading / trailing segments before / after the track proper (<= 64 bars unless one segment)
    i_end = 0
    while i_end < len(segs) - 1 and not main(i_end, segs[0]) and (i_end == 0 or segs[i_end][1] <= 64):
        i_end += 1
    o_start = len(segs)
    while o_start - 1 > i_end and not main(o_start - 1, segs[-1]) and \
            (o_start == len(segs) or segs[-1][1] - segs[o_start - 1][0] <= 64):
        o_start -= 1
    for i in range(i_end):
        kinds[i] = "intro"
    for i in range(o_start, len(segs)):
        kinds[i] = "outro"
    for i in range(i_end, o_start):
        s = segs[i]
        if mean(kick_on, s) < 0.4 or (mean(low_db, s) < low_ref - 8 and mean(e_db, s) < e_ref - 3 and not full[i]):
            kinds[i] = "breakdown"
    for i in range(i_end, o_start):
        if not full[i] and i + 1 < len(segs) and full[i + 1] and segs[i][1] - segs[i][0] <= 16 and rising(segs[i]):
            kinds[i] = "build"
    for i in range(max(1, i_end), o_start):
        if full[i] and kinds[i - 1] in ("breakdown", "build"):
            kinds[i] = "drop"
    out: list[tuple[str, int, int]] = []
    for kind, (a, b) in zip(kinds, segs):
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1], b)
        else:
            out.append((kind, a, b))
    return out


def _edges_and_cues(sections: list[dict], f: dict[str, np.ndarray], bar_count: int, phrase_off: int,
                    t: Callable[[int], float]) -> tuple[dict, dict, list[dict]]:
    """Intro/outro Edges and cue points. A 0-bar edge (track starts/ends full-on) reports drums_only/has_vocals
    for the 8 bars a DJ would mix over instead."""
    intro_end = sections[0]["end_bar"] if sections[0]["kind"] == "intro" else 0
    outro_start = sections[-1]["start_bar"] if sections[-1]["kind"] == "outro" else bar_count
    kick_on = f["kick"] >= _KICK_ON
    groove = kick_on & (f["low_db"] >= np.percentile(f["low_db"], 90) - 6)
    ref = groove if groove.sum() >= 4 else np.ones(bar_count, bool)
    harm_main, bass_main = float(np.median(f["harm_db"][ref])), float(np.median(f["bass_db"][ref]))

    def edge(a: int, b: int) -> dict:
        lo, hi = (a, b) if b > a else ((0, min(8, bar_count)) if a == 0 else (max(0, bar_count - 8), bar_count))
        has_vocals = bool(np.mean(f["vocal"][lo:hi] >= 0.5) >= 0.25)
        drums_only = bool(not has_vocals and np.median(f["harm_db"][lo:hi]) <= harm_main - 8
                          and np.median(f["bass_db"][lo:hi]) <= bass_main - 8)
        return {"bars": b - a, "start_s": t(a), "end_s": t(b), "drums_only": drums_only, "has_vocals": has_vocals}

    intro, outro = edge(0, intro_end), edge(outro_start, bar_count)
    cues = [{"bar": 0, "time_s": t(0), "label": "first downbeat"}]
    mix_in = 0
    if intro_end and not kick_on[0]:
        on = np.nonzero(kick_on[:intro_end])[0]
        mix_in = int(on[0]) // 4 * 4 if on.size else 0
    cues.append({"bar": mix_in, "time_s": t(mix_in),
                 "label": f"mix in ({intro_end - mix_in}-bar intro)" if intro_end > mix_in else "mix in (no intro)"})
    count: dict[str, int] = {}
    for s in sections:
        if s["kind"] in ("drop", "breakdown"):
            count[s["kind"]] = count.get(s["kind"], 0) + 1
            n = count[s["kind"]]
            label = s["kind"] + (f" {n}" if n > 1 else "")
            cues.append({"bar": s["start_bar"], "time_s": s["start_s"], "label": label})
    if outro_start < bar_count:
        label = f"mix out ({bar_count - outro_start}-bar outro)"
        cues.append({"bar": outro_start, "time_s": t(outro_start), "label": label})
    else:  # cold ending: last phrase start at least 16 bars before the end
        b = max(0, (bar_count - 16 - phrase_off) // 8 * 8 + phrase_off)
        cues.append({"bar": b, "time_s": t(b), "label": f"mix out (ends cold, {bar_count - b} bars left)"})
    cues.sort(key=lambda c: (c["time_s"], c["label"] != "first downbeat"))
    return intro, outro, cues


# ---------------------------------------------------------------------------------------------------------------
# waveform


def waveform_peaks(samples: np.ndarray, sr: int, points: int) -> dict:
    """3-band (low < 200 Hz < mid < 2.5 kHz < high) peak waveform, `points` values per band in 0-1 on one scale."""
    n = samples.size
    points = int(max(1, min(points, n)))
    x = samples.astype(np.float32, copy=False)
    low = signal.sosfilt(signal.butter(4, 200, "lowpass", fs=sr, output="sos"), x)
    high = signal.sosfilt(signal.butter(4, 2500, "highpass", fs=sr, output="sos"), x)
    mid = x - low - high
    starts = (np.arange(points) * n) // points
    peaks = [np.maximum.reduceat(np.abs(b), starts) for b in (low, mid, high)]
    ref = max(float(np.percentile(p, 99.5)) for p in peaks) + 1e-9  # one scale: band levels stay comparable
    low_p, mid_p, high_p = (np.clip(p / ref, 0, 1) for p in peaks)
    return {"points_per_second": round(points / (n / sr), 6), "low": _r(low_p, 2), "mid": _r(mid_p, 2),
            "high": _r(high_p, 2)}


# ---------------------------------------------------------------------------------------------------------------
# public entry points + cache


def analyze(path: Path, bpm_hint: float | None = None, track_id: str = "") -> dict:
    """Full TrackAnalysis dict (frontend/src/api/types.ts) for an audio file. ValueError on unusable audio."""
    samples = decode_mono(Path(path))
    return _analyze_samples(samples, SR, bpm_hint, track_id)


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _audio_fingerprint(path: Path) -> str:
    """Hash of the audio payload, ignoring ID3v2/ID3v1 tags (so retagging, e.g. TRCK, keeps the cache valid)."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(10)
        start = 0
        if len(head) == 10 and head[:3] == b"ID3":
            start = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
            if head[5] & 0x10:
                start += 10
        end = size
        if size >= 128:
            f.seek(size - 128)
            if f.read(3) == b"TAG":
                end = size - 128
        h = hashlib.sha1(f"{end - start}".encode())
        for pos in (start, max(start, end - 262144)):
            f.seek(pos)
            h.update(f.read(min(262144, max(0, end - pos))))
    return h.hexdigest()


def load_or_analyze(seed_dir: Path, track_id: str, path: Path, bpm_hint: float | None) -> dict:
    """Cached analyze(): seed_dir/.seedbox/analysis/<id>.json keyed by (size, mtime, ANALYSIS_VERSION, bpm_hint).

    A size/mtime miss falls back to an audio fingerprint that ignores ID3 tags, so retagged files (TRCK, POPM...)
    are not reanalysed. Other versions are recomputed. Thread-safe (one lock per cache file), atomic writes.
    ValueError from analyze() propagates and nothing is cached.
    """
    path = Path(path)
    cache = Path(seed_dir) / ".seedbox" / "analysis" / f"{track_id}.json"
    with _lock_for(str(cache)):
        st = path.stat()
        hint = round(float(bpm_hint), 3) if bpm_hint else None
        key = {"size": st.st_size, "mtime": st.st_mtime_ns, "version": ANALYSIS_VERSION, "bpm_hint": hint}
        cached = None
        try:
            cached = json.loads(cache.read_text())
        except (OSError, ValueError):
            pass
        if isinstance(cached, dict) and isinstance(cached.get("analysis"), dict):
            old = cached.get("key") or {}
            if all(old.get(k) == key[k] for k in key):
                return cached["analysis"]
            if old.get("version") == ANALYSIS_VERSION and old.get("bpm_hint") == hint \
                    and old.get("fingerprint") and old.get("fingerprint") == _audio_fingerprint(path):
                key["fingerprint"] = old["fingerprint"]
                _write_json(cache, {"key": key, "analysis": cached["analysis"]})
                return cached["analysis"]
        result = analyze(path, bpm_hint, track_id)
        log.debug("structure %s: %s bpm (%s), %d bars, %d sections", track_id, result["bpm"], result["bpm_source"],
                  result["bar_count"], len(result["sections"]))
        key["fingerprint"] = _audio_fingerprint(path)
        _write_json(cache, {"key": key, "analysis": result})
        return result


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
