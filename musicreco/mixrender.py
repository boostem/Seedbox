"""Render a MixPlan (frontend/src/api/types.ts, APP_SPEC.md "v4") to audio: two key-locked decks on one master clock
with DJ-mixer EQ, filter, echo and reverb, summed, levelled, limited and encoded to AAC.

Signal flow per deck: decode (ffmpeg, stereo) -> source audio for the window in master-beat order (loops spliced at
beat-accurate seams with 5 ms equal-power crossfades) -> Rubber Band time stretch (key lock) driven by the master
tempo curve, then a sub-sample timing correction (a pilot click track stretched with the same ratios measures where
every source sample actually landed; a 32-tap windowed-sinc warp moves it to the exact master time) -> auto gain to a
common loudness -> deck FX (turntable brake / spinback: varispeed reads of the deck) -> bipolar 24 dB/oct resonant
filter (per 64-sample block, smoothed) -> echo / reverb send taps (pre-EQ, pre-fader: a throw catches the whole
vocal even while the mids are being cut) -> 3-band isolator (Linkwitz-Riley 4th order at 200 Hz / 2.5 kHz,
phase-compensated, -30 dB = kill) -> fader.
Bus: both decks + echo returns (beat-synced, darkening repeats, tails ring past the deck) + a shared reverb return
-> slow zero-phase leveller (tames blend bumps) -> look-ahead true-peak limiter (-1 dBTP) -> AAC m4a.

Lanes follow types.ts: values between keyframes come from the left keyframe's curve (linear, ease = smoothstep,
hold = step at the next keyframe), clamped to LANE_RANGES; steps are softened to ~10 ms ramps (filter: ~30 ms).
beat_times_s comes from integrating the tempo curve, and the timing correction makes both decks' beats land on it.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

from .paths import ffmpeg_path

RENDER_VERSION = 2  # bump whenever the output for the same plan + files changes (invalidates cached renders)
SR = 44100

LANES = ("volume", "eq_low", "eq_mid", "eq_high", "filter", "echo_send", "echo_feedback", "reverb_send")
LANE_RANGES: dict[str, tuple[float, float]] = {
    "volume": (0.0, 1.0), "eq_low": (-30.0, 6.0), "eq_mid": (-30.0, 6.0), "eq_high": (-30.0, 6.0),
    "filter": (-1.0, 1.0), "echo_send": (0.0, 1.0), "echo_feedback": (0.0, 0.95), "reverb_send": (0.0, 1.0),
}
LANE_DEFAULTS = {"volume": 1.0, "eq_low": 0.0, "eq_mid": 0.0, "eq_high": 0.0, "filter": 0.0, "echo_send": 0.0,
                 "echo_feedback": 0.5, "reverb_send": 0.0}
CURVES = ("linear", "hold", "ease")

DECODE_TIMEOUT_S = 300
ENCODE_TIMEOUT_S = 300
DECK_LUFS = -12.0  # every deck is trimmed to this integrated loudness (whole track) before the mixer
TRIM_LIMIT_DB = 12.0
CEILING_DBTP = -1.6  # limiter ceiling; leaves room for AAC overshoot under -1 dBTP
LEVEL_THRESHOLD_DB = 1.0  # leveller acts this far above the deck reference
LEVEL_RATIO = 3.0
MAKEUP_DB = 1.0
XOVER_LOW_HZ, XOVER_HIGH_HZ = 200.0, 2500.0
FILTER_LP_MIN_HZ, FILTER_LP_MAX_HZ = 80.0, 20000.0
FILTER_HP_MIN_HZ, FILTER_HP_MAX_HZ = 20.0, 10000.0
ECHO_RETURN = 0.85
REVERB_RETURN = 0.9
REVERB_PREDELAY_S = 0.022
FX_KINDS = ("brake", "spinback")
SEAM_S = 0.005
STEP_RAMP_S = 0.010
FILTER_SMOOTH_S = 0.030
CTL = 16  # control-rate decimation (samples)
FILTER_BLOCK = 64
PILOT_SPACING = 2048
SINC_TAPS = 32
SINC_PHASES = 1024
BITRATE = "256k"

_log_lock = threading.Lock()
_loudness_cache: dict[tuple[str, int, int], float] = {}
_encoder: list[str] = []


# ---------------------------------------------------------------------------------------------------------------
# plan semantics (same as mixplan.lane_value / deck_time)


def lane_value(keyframes: list[dict], beat: float | np.ndarray) -> float | np.ndarray:
    """Value of a lane at master beat(s): constant before the first / after the last keyframe, else the left
    keyframe's curve towards the next one (linear, ease = smoothstep, hold = step at the next keyframe)."""
    scalar = np.ndim(beat) == 0
    b = np.atleast_1d(np.asarray(beat, dtype=float))
    if not keyframes:
        return 0.0 if scalar else np.zeros_like(b)
    kb = np.array([float(k["beat"]) for k in keyframes])
    kv = np.array([float(k["value"]) for k in keyframes])
    order = np.argsort(kb, kind="stable")
    kb, kv = kb[order], kv[order]
    kc = np.array([CURVES.index(c) if (c := keyframes[i].get("curve")) in CURVES else 0 for i in order])
    i = np.clip(np.searchsorted(kb, b, side="right") - 1, 0, kb.size - 1)
    j = np.minimum(i + 1, kb.size - 1)
    span = kb[j] - kb[i]
    u = np.clip(np.where(span > 0, (b - kb[i]) / np.where(span > 0, span, 1.0), 0.0), 0.0, 1.0)
    c = kc[i]
    u = np.where(c == 1, 0.0, np.where(c == 2, u * u * (3 - 2 * u), u))
    out = kv[i] + (kv[j] - kv[i]) * u
    out = np.where(b < kb[0], kv[0], out)
    return float(out[0]) if scalar else out


def _loops(deck: dict) -> list[dict]:
    out = []
    for lp in deck.get("loops") or []:
        try:
            s, e, src, n = (float(lp[k]) for k in ("start_beat", "end_beat", "source_beat", "length_beats"))
        except (KeyError, TypeError, ValueError):
            continue
        if n > 0 and e > s and all(map(math.isfinite, (s, e, src, n))):
            out.append({"start_beat": s, "end_beat": e, "source_beat": src, "length_beats": n})
    return sorted(out, key=lambda lp: lp["start_beat"])


def deck_time(deck: dict, beat: float | np.ndarray) -> float | np.ndarray:
    """Master beat(s) -> the deck's file time (s). Outside loops the deck runs on its own grid (beat0_s + beats at
    native_bpm); inside a loop it repeats [source_beat, source_beat + length_beats) of that un-looped timeline, and
    after the loop releases it continues where the un-looped timeline is (slip mode). A later loop wins."""
    scalar = np.ndim(beat) == 0
    b = np.atleast_1d(np.asarray(beat, dtype=float))
    src = b.copy()
    for lp in _loops(deck):
        inside = (b >= lp["start_beat"]) & (b < lp["end_beat"])
        src = np.where(inside, lp["source_beat"] + np.mod(b - lp["start_beat"], lp["length_beats"]), src)
    t = float(deck["beat0_s"]) + src * 60.0 / float(deck["native_bpm"])
    return float(t[0]) if scalar else t


# ---------------------------------------------------------------------------------------------------------------
# normalisation + hash


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _keyframes(raw: object, lo: float, hi: float, default: float) -> list[dict]:
    out = []
    for k in raw if isinstance(raw, list) else []:
        try:
            b, v = float(k["beat"]), float(k["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(b) and math.isfinite(v):
            c = k.get("curve")
            out.append({"beat": b, "value": _clamp(v, lo, hi), "curve": c if c in CURVES else "linear"})
    out.sort(key=lambda k: k["beat"])
    return out or [{"beat": 0.0, "value": default, "curve": "hold"}]


def _normalise(plan: dict) -> dict:
    """The audio-relevant part of a plan, clamped and sorted. ValueError on nonsense."""
    try:
        w0, w1 = float(plan["window"]["start_beat"]), float(plan["window"]["end_beat"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("plan has no usable window") from e
    if not (math.isfinite(w0) and math.isfinite(w1) and w1 > w0):
        raise ValueError("plan window is empty")
    tempo = _keyframes(plan.get("tempo"), 40.0, 300.0, 120.0)
    decks = {}
    for name in ("a", "b"):
        d = plan.get(name)
        if not isinstance(d, dict):
            raise ValueError(f"plan has no deck {name}")
        try:
            nb, b0 = float(d["native_bpm"]), float(d["beat0_s"])
            pf, pt = float(d.get("play_from_beat", w0)), float(d.get("play_to_beat", w1))
            eb = float(d.get("echo_beats") or 0.75)
        except (TypeError, ValueError) as e:
            raise ValueError(f"deck {name} has non-numeric fields") from e
        if not (math.isfinite(nb) and 20 <= nb <= 400) or not math.isfinite(b0):
            raise ValueError(f"deck {name} has no usable native_bpm / beat0_s")
        lanes = d.get("lanes") if isinstance(d.get("lanes"), dict) else {}
        decks[name] = {
            "track_id": str(d.get("track_id", "")),
            "beat0_s": round(b0, 6), "native_bpm": round(nb, 6),
            "play_from_beat": pf, "play_to_beat": pt,
            "echo_beats": _clamp(eb if math.isfinite(eb) and eb > 0 else 0.75, 0.0625, 8.0),
            "loops": _loops(d),
            "fx": _fx_list(d),
            "lanes": {k: _keyframes(lanes.get(k), *LANE_RANGES[k], LANE_DEFAULTS[k]) for k in LANES},
        }
    return {"window": {"start_beat": w0, "end_beat": w1}, "tempo": tempo, "key_lock": bool(plan.get("key_lock", True)),
            "a": decks["a"], "b": decks["b"]}


def _fx_list(deck: dict) -> list[dict]:
    out = []
    for f in deck.get("fx") or []:
        try:
            kind, beat, beats = f["kind"], float(f["beat"]), float(f["beats"])
        except (KeyError, TypeError, ValueError):
            continue
        if kind in FX_KINDS and math.isfinite(beat) and math.isfinite(beats) and beats > 0:
            out.append({"kind": kind, "beat": beat, "beats": _clamp(beats, 0.25, 16.0)})
    return sorted(out, key=lambda f: f["beat"])


def _round_floats(x: object) -> object:
    if isinstance(x, float):
        return round(x, 6) + 0.0
    if isinstance(x, dict):
        return {k: _round_floats(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_round_floats(v) for v in x]
    return x


def _file_identity(path: Path) -> list:
    p = Path(path)
    try:
        st = p.stat()
        return [str(p.resolve()), st.st_size, st.st_mtime_ns]
    except OSError:
        return [str(p), None, None]


def plan_hash(plan: dict, a_path: Path, b_path: Path) -> str:
    """Stable cache key: the normalised audio-relevant plan (notes, markers, edited... ignored) + both files' path,
    size and mtime + RENDER_VERSION. 24 hex chars."""
    try:
        norm: object = _round_floats(_normalise(plan))
    except ValueError:
        norm = {"invalid": json.dumps(plan, sort_keys=True, default=str)}
    payload = {"v": RENDER_VERSION, "plan": norm, "a": _file_identity(a_path), "b": _file_identity(b_path)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------------------------------------------
# audio io + measurement


def decode_stereo(path: Path, sr: int = SR) -> np.ndarray:
    """Decode the first audio stream to (2, n) float32 at `sr` via ffmpeg (same decoder/timing as structure.py)."""
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise ValueError("ffmpeg not found")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-vn",
           "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=DECODE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"could not decode {Path(path).name}: {e}") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise ValueError(f"could not decode {Path(path).name}: {tail[0]}")
    x = np.frombuffer(proc.stdout, dtype="<f4")
    if x.size < 2:
        raise ValueError(f"no audio in {Path(path).name}")
    return np.ascontiguousarray(x[: x.size // 2 * 2].reshape(-1, 2).T)


def _aac_encoder(ffmpeg: str) -> str:
    if not _encoder:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20,
                                 stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _encoder.append("aac_at" if " aac_at " in out else "aac")
    return _encoder[0]


def encode_m4a(audio: np.ndarray, sr: int, out_path: Path) -> None:
    """(2, n) float -> AAC (AudioToolbox when ffmpeg has it, else ffmpeg's aac) in an m4a at `out_path`."""
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise ValueError("ffmpeg not found")
    pcm = np.ascontiguousarray(np.clip(audio, -1.0, 1.0).T.astype("<f4")).tobytes()
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(sr),
           "-ac", "2", "-i", "-", "-c:a", _aac_encoder(ffmpeg), "-b:a", BITRATE, "-movflags", "+faststart",
           "-f", "ipod", str(out_path)]
    try:
        proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=ENCODE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"could not encode the render: {e}") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise ValueError(f"could not encode the render: {tail[0]}")


def _k_weight(x: np.ndarray, sr: int) -> np.ndarray:
    """BS.1770 K-weighting (high shelf + RLB high-pass) along the last axis."""
    a_ = 10 ** (4.0 / 40)
    w0 = 2 * np.pi * 1500.0 / sr
    al = np.sin(w0) / (2 * (1 / np.sqrt(2)))
    c, sa = np.cos(w0), 2 * np.sqrt(a_) * al
    shelf = [a_ * ((a_ + 1) + (a_ - 1) * c + sa), -2 * a_ * ((a_ - 1) + (a_ + 1) * c), a_ * ((a_ + 1) + (a_ - 1) * c - sa),
             (a_ + 1) - (a_ - 1) * c + sa, 2 * ((a_ - 1) - (a_ + 1) * c), (a_ + 1) - (a_ - 1) * c - sa]
    w0 = 2 * np.pi * 38.0 / sr
    al, c = np.sin(w0) / (2 * 0.5), np.cos(w0)
    hp = [(1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + al, -2 * c, 1 - al]
    sos = np.array([np.r_[np.array(s[:3]) / s[3], 1.0, np.array(s[4:]) / s[3]] for s in (shelf, hp)])
    return signal.sosfilt(sos, x, axis=-1)


def _block_power(x: np.ndarray, sr: int, win_s: float, hop_s: float) -> np.ndarray:
    """Channel-summed mean square of K-weighted audio in windows of win_s every hop_s."""
    p = (_k_weight(np.asarray(x, dtype=np.float64), sr) ** 2).sum(axis=0)
    win, hop = int(round(win_s * sr)), int(round(hop_s * sr))
    if p.size < win:
        return np.array([p.mean()]) if p.size else np.zeros(1)
    c = np.concatenate([[0.0], np.cumsum(p)])
    starts = np.arange(0, p.size - win + 1, hop)
    return (c[starts + win] - c[starts]) / win


def integrated_lufs(x: np.ndarray, sr: int) -> float:
    """BS.1770-4 integrated loudness (LUFS) of (channels, n) audio; -70 for silence."""
    z = _block_power(x, sr, 0.4, 0.1)
    lk = -0.691 + 10 * np.log10(z + 1e-12)
    z = z[lk > -70]
    if not z.size:
        return -70.0
    rel = -0.691 + 10 * np.log10(z.mean()) - 10
    z = z[-0.691 + 10 * np.log10(z) > rel]
    return float(-0.691 + 10 * np.log10(z.mean())) if z.size else -70.0


def short_term_lufs(x: np.ndarray, sr: int, hop_s: float = 0.1) -> np.ndarray:
    """EBU short-term loudness (3 s windows) every hop_s; window k starts at k * hop_s."""
    return -0.691 + 10 * np.log10(_block_power(x, sr, 3.0, hop_s) + 1e-12)


def true_peak_db(x: np.ndarray) -> float:
    """4x-oversampled peak (dBTP) of (channels, n) audio."""
    up = signal.resample_poly(np.asarray(x, dtype=np.float64), 4, 1, axis=-1)
    return float(20 * np.log10(np.max(np.abs(up)) + 1e-12))


def _track_lufs(path: Path, audio: np.ndarray, sr: int) -> float:
    ident = _file_identity(path)
    key = (ident[0], ident[1] or 0, ident[2] or 0)
    with _log_lock:
        if key in _loudness_cache:
            return _loudness_cache[key]
    # every 4th 400 ms block is plenty for a gain trim
    lufs = integrated_lufs(audio[:, ::2], sr // 2) if sr % 2 == 0 else integrated_lufs(audio, sr)
    with _log_lock:
        if len(_loudness_cache) > 256:
            _loudness_cache.clear()
        _loudness_cache[key] = lufs
    return lufs


# ---------------------------------------------------------------------------------------------------------------
# master clock


class _Clock:
    """Master beat <-> time (s, 0 at window.start_beat) by integrating 60 / tempo(beat)."""

    STEP = 1.0 / 128

    def __init__(self, tempo: list[dict], b_lo: float, b_hi: float, w0: float) -> None:
        lo, hi = math.floor(min(b_lo, w0)) - 2, math.ceil(b_hi) + 2
        self.grid = np.arange(lo, hi + self.STEP / 2, self.STEP)
        self.bpm_grid = np.clip(np.asarray(lane_value(tempo, self.grid), dtype=float), 40.0, 300.0)
        spb = 60.0 / self.bpm_grid
        t = np.concatenate([[0.0], np.cumsum((spb[1:] + spb[:-1]) / 2 * self.STEP)])
        self.t_grid = t - np.interp(w0, self.grid, t)

    def time(self, beat: float | np.ndarray) -> np.ndarray:
        return np.interp(beat, self.grid, self.t_grid)

    def beat(self, t: float | np.ndarray) -> np.ndarray:
        return np.interp(t, self.t_grid, self.grid)

    def bpm(self, beat: float | np.ndarray) -> np.ndarray:
        return np.interp(beat, self.grid, self.bpm_grid)


# ---------------------------------------------------------------------------------------------------------------
# deck source: master-beat-ordered audio with loops spliced in


def _segments(deck: dict, b0: float, b1: float) -> list[tuple[float, float, float]]:
    """(start_beat, end_beat, file_time_at_start) pieces of [b0, b1) on which file time runs linearly."""
    cuts = {b0, b1}
    for lp in _loops(deck):
        s, e, n = lp["start_beat"], lp["end_beat"], lp["length_beats"]
        if e <= b0 or s >= b1:
            continue
        cuts.update((s, e))
        k0 = max(1, math.ceil((b0 - s) / n))
        k1 = math.floor((min(e, b1) - s) / n)
        if k1 - k0 > 100000:
            raise ValueError("loop too short for its length")
        cuts.update(s + k * n for k in range(k0, k1 + 1))
    pts = sorted(c for c in cuts if b0 <= c <= b1)
    out = []
    for s, e in zip(pts[:-1], pts[1:]):
        if e - s > 1e-9:
            out.append((s, e, float(deck_time(deck, s + 1e-9)) - 1e-9 * 60.0 / float(deck["native_bpm"])))
    return out


def _source(audio: np.ndarray, deck: dict, b0: float, b1: float, sr: int) -> tuple[np.ndarray, list[str]]:
    """(2, n) audio for master beats [b0, b1) at the deck's native tempo (sample j <-> beat b0 + j / (sr * spb))."""
    spb = 60.0 / float(deck["native_bpm"])
    per_beat = sr * spb
    n = int(round((b1 - b0) * per_beat))
    out = np.zeros((2, n), np.float32)
    nfile = audio.shape[1]
    seam = max(8, int(round(SEAM_S * sr)))
    warn: list[str] = []

    def take(start: int, length: int) -> np.ndarray:
        seg = np.zeros((2, length), np.float32)
        a, b = max(start, 0), min(start + length, nfile)
        if a < b:
            seg[:, a - start:b - start] = audio[:, a:b]
        return seg

    prev_end_file = None
    fade = np.linspace(0.0, 1.0, seam, endpoint=False) + 0.5 / seam
    f_in, f_out = np.sin(0.5 * np.pi * fade), np.cos(0.5 * np.pi * fade)
    for s, e, ft in _segments(deck, b0, b1):
        i0, i1 = int(round((s - b0) * per_beat)), min(n, int(round((e - b0) * per_beat)))
        if i1 <= i0:
            continue
        f0 = int(round(ft * sr))
        out[:, i0:i1] = take(f0, i1 - i0)
        if prev_end_file is not None and abs(f0 - prev_end_file) > 1 and i0 >= seam:
            # equal-power splice ending exactly on the seam: the incoming beat's attack stays intact
            out[:, i0 - seam:i0] = out[:, i0 - seam:i0] * f_out + take(f0 - seam, seam) * f_in
        prev_end_file = f0 + (i1 - i0)
    return out, warn


# ---------------------------------------------------------------------------------------------------------------
# time stretch with exact timing


def _sinc_table() -> np.ndarray:
    half = SINC_TAPS // 2
    ph = np.arange(SINC_PHASES)[:, None] / SINC_PHASES
    k = np.arange(-half + 1, half + 1)[None, :]
    d = k - ph
    w = np.kaiser(2 * SINC_TAPS + 1, 8.6)
    win = np.interp(d, np.linspace(-SINC_TAPS / 2 - 0.5, SINC_TAPS / 2 + 0.5, w.size), w)
    tab = np.sinc(d * 0.97) * 0.97 * win
    tab /= tab.sum(axis=1, keepdims=True)
    return tab.astype(np.float32)


_SINC = _sinc_table()


def _resample_at(y: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """y (2, n) read at fractional positions `pos` with a 32-tap Kaiser-windowed sinc (zeros outside y)."""
    half = SINC_TAPS // 2
    pad = np.pad(y, ((0, 0), (half + 1, half + 2)))
    out = np.empty((2, pos.size), np.float32)
    lim = pad.shape[1] - SINC_TAPS - 1
    taps = np.arange(SINC_TAPS)
    for c0 in range(0, pos.size, 8192):
        p = pos[c0:c0 + 8192]
        i = np.floor(p)
        ph = np.minimum(((p - i) * SINC_PHASES).round().astype(np.int64), SINC_PHASES - 1)
        carry = ((p - i) * SINC_PHASES).round().astype(np.int64) >= SINC_PHASES
        i = i.astype(np.int64) + carry
        base = np.clip(i + 2, 0, lim)  # pad offset (half + 1) minus (half - 1) taps before i
        idx = base[:, None] + taps[None, :]
        w = _SINC[ph]
        for ch in range(2):
            out[ch, c0:c0 + p.size] = np.einsum("ij,ij->i", pad[ch][idx], w)
        bad = (p < -half) | (p > y.shape[1] + half)
        if bad.any():
            out[:, c0:c0 + p.size][:, bad] = 0.0
    return out


def _pilot_map(speed: np.ndarray, sr: int, nominal: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Where Rubber Band puts source samples for this ratio curve: (source positions, output positions), measured
    by stretching a click track with the same ratios. None when the clicks cannot be matched one to one."""
    import pedalboard

    n = speed.size
    clicks = np.arange(PILOT_SPACING // 2, n - PILOT_SPACING // 2, PILOT_SPACING)
    if clicks.size < 4:
        return None
    pilot = np.zeros((1, n), np.float32)
    pilot[0, clicks] = 0.5
    yp = pedalboard.time_stretch(pilot, sr, stretch_factor=speed, high_quality=True)[0].astype(np.float64)
    env = ndimage.uniform_filter1d(yp * yp, 5)
    dist = max(4, int(0.5 * PILOT_SPACING / float(np.max(speed))))
    pk, _ = signal.find_peaks(env, height=0.15 * float(np.percentile(env[env > 0], 99.9)) if env.any() else 1,
                              distance=dist)
    if pk.size < clicks.size // 2:
        return None
    a, b, c = env[np.maximum(pk - 1, 0)], env[pk], env[np.minimum(pk + 1, env.size - 1)]
    den = a - 2 * b + c
    pos = pk + np.where(den < 0, 0.5 * (a - c) / np.where(den < 0, den, 1), 0.0)
    expect = np.interp(clicks, np.arange(n), nominal)
    # follow the drift click by click: each click's peak is the nearest one to (nominal + previous offset)
    keep, got, off = [], [], 0.0
    for k, e in enumerate(expect):
        i = int(np.searchsorted(pos, e + off))
        cand = [x for x in (i - 1, i) if 0 <= x < pos.size]
        if not cand:
            continue
        x = min(cand, key=lambda x: abs(pos[x] - e - off))
        if abs(pos[x] - e - off) < PILOT_SPACING / 4:
            keep.append(k)
            got.append(pos[x])
            off = pos[x] - e
    if len(keep) < max(4, clicks.size // 2):
        return None
    clicks, expect = clicks[keep], expect[keep]
    resid = np.asarray(got) - expect
    resid = ndimage.uniform_filter1d(resid, 5, mode="nearest")
    return clicks.astype(float), expect + resid


def _to_master(src: np.ndarray, speed: np.ndarray, jq: np.ndarray, sr: int, key_lock: bool) -> tuple[np.ndarray, str]:
    """Deck audio at master samples whose source positions are `jq` (fractional source indices)."""
    if np.max(np.abs(speed - 1.0)) < 1e-9:
        start = int(round(float(jq[0])))
        out = np.zeros((2, jq.size), np.float32)
        a, b = max(0, -start), min(jq.size, src.shape[1] - start)
        if a < b:
            out[:, a:b] = src[:, start + a:start + b]
        return out, ""
    if not key_lock:
        return _resample_at(src, jq), ""
    import pedalboard

    y = pedalboard.time_stretch(src, sr, stretch_factor=speed, high_quality=True, transient_mode="crisp")
    nominal = np.concatenate([[0.0], np.cumsum(1.0 / speed)])[:-1]
    m = _pilot_map(speed, sr, nominal)
    note = ""
    if m is None:
        m = (np.arange(speed.size, dtype=float), nominal)
        note = "time-stretch timing could not be verified (beats may drift by a few ms)"
    c, p = m
    slope_lo = (p[1] - p[0]) / (c[1] - c[0])
    slope_hi = (p[-1] - p[-2]) / (c[-1] - c[-2])
    u = np.interp(jq, c, p)
    u = np.where(jq < c[0], p[0] + (jq - c[0]) * slope_lo, u)
    u = np.where(jq > c[-1], p[-1] + (jq - c[-1]) * slope_hi, u)
    return _resample_at(y, u), note


# ---------------------------------------------------------------------------------------------------------------
# mixer stages


def _ctl(keyframes: list[dict], beats: np.ndarray, smooth_s: float, sr: int, n: int) -> np.ndarray:
    """Per-sample lane values: lane at control rate, steps softened by a centred moving average of smooth_s."""
    v = np.asarray(lane_value(keyframes, beats), dtype=float)
    w = max(1, int(round(smooth_s * sr / CTL)))
    if w > 1 and np.ptp(v) > 0:
        v = ndimage.uniform_filter1d(v, w, mode="nearest")
    return np.interp(np.arange(n), np.arange(v.size) * CTL, v).astype(np.float32)


def _iso_gain(db: np.ndarray) -> np.ndarray:
    """Isolator band gain: dB below -24 fades to a true kill at -30."""
    db = np.asarray(db, dtype=float)
    return (10 ** (db / 20) * np.clip((db + 30.0) / 6.0, 0.0, 1.0)).astype(np.float32)


def _allpass_sos(fc: float, sr: int) -> np.ndarray:
    w0 = 2 * np.pi * fc / sr
    al, c = np.sin(w0) / (2 * np.sqrt(0.5)), np.cos(w0)
    return np.array([[(1 - al) / (1 + al), -2 * c / (1 + al), 1.0, 1.0, -2 * c / (1 + al), (1 - al) / (1 + al)]])


def _isolator(x: np.ndarray, gl: np.ndarray, gm: np.ndarray, gh: np.ndarray, sr: int) -> np.ndarray:
    """3-band DJ isolator: LR4 split at 200 Hz / 2.5 kHz, low band all-pass compensated (bands sum flat)."""
    lp1 = signal.butter(2, XOVER_LOW_HZ, "lowpass", fs=sr, output="sos")
    hp1 = signal.butter(2, XOVER_LOW_HZ, "highpass", fs=sr, output="sos")
    lp2 = signal.butter(2, XOVER_HIGH_HZ, "lowpass", fs=sr, output="sos")
    hp2 = signal.butter(2, XOVER_HIGH_HZ, "highpass", fs=sr, output="sos")
    xd = x.astype(np.float64)
    low = signal.sosfilt(np.vstack([lp1, lp1, _allpass_sos(XOVER_HIGH_HZ, sr)]), xd, axis=-1)
    rest = signal.sosfilt(np.vstack([hp1, hp1]), xd, axis=-1)
    mid = signal.sosfilt(np.vstack([lp2, lp2]), rest, axis=-1)
    high = signal.sosfilt(np.vstack([hp2, hp2]), rest, axis=-1)
    return (low * gl + mid * gm + high * gh).astype(np.float32)


def _biquads(kind: str, fc: np.ndarray, q: np.ndarray, sr: int) -> np.ndarray:
    """RBJ low/high-pass biquads, vectorised: (len(fc), 6) normalised sos rows."""
    w0 = 2 * np.pi * np.clip(fc, 10.0, 0.45 * sr) / sr
    c, al = np.cos(w0), np.sin(w0) / (2 * q)
    a0 = 1 + al
    b = np.stack([(1 - c) / 2, 1 - c, (1 - c) / 2] if kind == "lp" else [(1 + c) / 2, -(1 + c), (1 + c) / 2], axis=1)
    a = np.stack([np.ones_like(c), -2 * c / a0, (1 - al) / a0], axis=1)
    return np.concatenate([b / a0[:, None], a], axis=1)


def _dj_filter(x: np.ndarray, f: np.ndarray, sr: int) -> np.ndarray:
    """Bipolar filter (f < 0 low-pass to 80 Hz, f > 0 high-pass to 10 kHz), 24 dB/oct with light resonance.
    Coefficients per 64-sample block from the smoothed lane; dry below |f| = 0.03 with a short blend."""
    n = x.shape[1]
    out = x.copy()
    nb = (n + FILTER_BLOCK - 1) // FILTER_BLOCK
    fb = f[np.minimum(np.arange(nb) * FILTER_BLOCK + FILTER_BLOCK // 2, n - 1)].astype(float)
    mag = np.abs(fb)
    wet = np.clip(np.abs(f) / 0.03, 0.0, 1.0)
    q2 = 1.3066 + 0.6 * np.clip(mag, 0, 1) ** 1.5
    for kind, sel in (("lp", fb < 0), ("hp", fb > 0)):
        active = ndimage.binary_dilation(sel & (mag >= 0.003), iterations=24)
        if not active.any():
            continue
        m = np.where(sel, mag, 0.0)
        if kind == "lp":
            fc = FILTER_LP_MAX_HZ * (FILTER_LP_MIN_HZ / FILTER_LP_MAX_HZ) ** m
        else:
            fc = FILTER_HP_MIN_HZ * (FILTER_HP_MAX_HZ / FILTER_HP_MIN_HZ) ** m
        s1 = _biquads(kind, fc, np.full_like(fc, 0.5412), sr)
        s2 = _biquads(kind, fc, q2, sr)
        y = np.zeros((2, n))
        zi = np.zeros((2, 2, 2))
        idx = np.nonzero(active)[0]
        runs = np.split(idx, np.nonzero(np.diff(idx) > 1)[0] + 1)
        xd = x.astype(np.float64)
        for run in runs:
            zi[:] = 0.0
            k = run[0]
            while k <= run[-1]:
                k2 = k + 1  # merge blocks with identical coefficients (holds) into one call
                while k2 <= run[-1] and fc[k2] == fc[k] and q2[k2] == q2[k]:
                    k2 += 1
                a, b = k * FILTER_BLOCK, min(n, k2 * FILTER_BLOCK)
                y[:, a:b], zi = signal.sosfilt(np.stack([s1[k], s2[k]]), xd[:, a:b], axis=-1, zi=zi)
                k = k2
        side = (f < 0) if kind == "lp" else (f > 0)
        w = np.where(side, wet, 0.0)
        out = np.where(side, out * (1 - w) + y * w, out)
    return out.astype(np.float32)


def _echo(x: np.ndarray, delay: np.ndarray, fb: np.ndarray, sr: int) -> np.ndarray:
    """Feedback delay (repeats only) with a band-limiting filter in the loop: every repeat gets thinner/darker."""
    n = x.shape[1]
    hp = signal.butter(2, 220.0, "highpass", fs=sr, output="sos")
    lp = signal.butter(2, 6000.0, "lowpass", fs=sr, output="sos")
    sos = np.vstack([hp, lp])
    zi = np.zeros((2, 2, 2))
    w = np.zeros((2, n))
    out = np.zeros((2, n))
    blk = int(max(32, min(1024, np.floor(delay.min()) - 2)))
    xd = x.astype(np.float64)
    for a in range(0, n, blk):
        b = min(n, a + blk)
        r = np.arange(a, b) - delay[a:b]
        i = np.floor(r).astype(np.int64)
        fr = r - i
        ok = i >= 0
        i0 = np.clip(i, 0, n - 1)
        i1 = np.clip(i + 1, 0, n - 1)
        d = (w[:, i0] * (1 - fr) + w[:, i1] * fr) * ok
        y, zi = signal.sosfilt(sos, d, axis=-1, zi=zi)
        out[:, a:b] = y
        w[:, a:b] = xd[:, a:b] + y * fb[a:b]
    return out.astype(np.float32)


def _reverb(x: np.ndarray, sr: int) -> np.ndarray:
    """Wash: band-limited send, ~22 ms pre-delay (keeps the attack of what's thrown in), into a long, open plate-ish
    reverb (wet only)."""
    import pedalboard

    pre = signal.sosfilt(np.vstack([signal.butter(2, 220.0, "highpass", fs=sr, output="sos"),
                                    signal.butter(2, 9000.0, "lowpass", fs=sr, output="sos")]), x, axis=-1)
    d = int(round(REVERB_PREDELAY_S * sr))
    pre = np.concatenate([np.zeros((pre.shape[0], d)), pre[:, : pre.shape[1] - d]], axis=1) if d else pre
    rv = pedalboard.Reverb(room_size=0.9, damping=0.38, wet_level=1.0, dry_level=0.0, width=1.0)
    return rv.process(pre.astype(np.float32), sr).astype(np.float32)


def _apply_fx(x: np.ndarray, fx: list[dict], clock: _Clock, qa: int, sr: int) -> np.ndarray:
    """Deck FX on the deck's master-time signal x (sample 0 = master sample qa): a turntable brake (the deck
    slows to a stop, pitch falling with it) or a spinback (a fast backwards whip that winds down). Both are
    varispeed reads of the deck itself; the deck is silent after the effect."""
    n_all = x.shape[1]
    for f in fx:
        i0 = int(math.ceil(float(clock.time(f["beat"])) * sr)) - qa
        i1 = int(math.floor(float(clock.time(f["beat"] + f["beats"])) * sr)) - qa
        if i1 - i0 < 64 or i0 >= n_all or i1 <= 0:
            continue
        n = i1 - i0
        u = (np.arange(n) + 0.5) / n
        if f["kind"] == "brake":
            rate = (1.0 - u) ** 1.6
            src = x
        else:
            rate = np.where(u < 0.12, 1.0 - u / 0.12 * 4.2, -3.2 * np.exp(-(u - 0.12) * 3.5))
            lp = signal.butter(4, 5000.0, "lowpass", fs=sr, output="sos")  # tames aliasing at 3-4x reverse speed
            src = signal.sosfiltfilt(lp, x.astype(np.float64), axis=-1).astype(np.float32)
        pos = i0 + np.concatenate([[0.0], np.cumsum(rate)[:-1]])
        y = _resample_at(src, pos)
        tail = max(1, int(0.18 * n))
        fade = np.clip((n - np.arange(n)) / tail, 0.0, 1.0).astype(np.float32)
        a, b = max(0, i0), min(n_all, i1)
        x[:, a:b] = (y * fade)[:, a - i0:b - i0]
        x[:, b:] = 0.0
    return x


def _level(mix: np.ndarray, ref_power: float, sr: int) -> tuple[np.ndarray, float]:
    """Slow zero-phase leveller: above (deck reference + threshold) the level is compressed LEVEL_RATIO:1, then
    make-up gain. Returns (gain per sample, max reduction dB)."""
    hop = 1024
    p = (_k_weight(mix.astype(np.float64), sr) ** 2).sum(axis=0)
    n = p.size
    pb = np.add.reduceat(p, np.arange(0, n, hop)) / hop
    pb = ndimage.uniform_filter1d(pb, max(1, int(1.5 * sr / hop)), mode="nearest")
    lvl = 10 * np.log10(pb + 1e-12) - 10 * np.log10(ref_power)
    red = -np.maximum(lvl - LEVEL_THRESHOLD_DB, 0.0) * (1 - 1 / LEVEL_RATIO)
    red = ndimage.uniform_filter1d(red, max(1, int(1.0 * sr / hop)), mode="nearest")
    g_db = red + MAKEUP_DB
    g = 10 ** (np.interp(np.arange(n), np.arange(pb.size) * hop + hop / 2, g_db) / 20)
    return g.astype(np.float32), float(-red.min()) if red.size else 0.0


def _limit(x: np.ndarray, ceiling_db: float) -> tuple[np.ndarray, float]:
    """Look-ahead true-peak limiter (offline, zero-phase gain envelope: min over 25 ms, then averaged)."""
    c = 10 ** (ceiling_db / 20)
    up = np.abs(signal.resample_poly(x.astype(np.float64), 4, 1, axis=-1)).max(axis=0)
    pk = up[: x.shape[1] * 4].reshape(-1, 4).max(axis=1)
    req = np.minimum(1.0, c / np.maximum(pk, 1e-9))
    if req.min() >= 1.0:
        return x, 0.0
    w = int(0.025 * SR) | 1
    g = ndimage.uniform_filter1d(ndimage.minimum_filter1d(req, w, mode="nearest"), w, mode="nearest")
    g = np.minimum(g, ndimage.minimum_filter1d(req, 3, mode="nearest"))  # float safety at isolated peaks
    y = (x * g).astype(np.float32)
    return np.clip(y, -c, c), float(-20 * np.log10(g.min()))


# ---------------------------------------------------------------------------------------------------------------
# render


def _deck_stage(name: str, deck: dict, audio: np.ndarray, trim: float, clock: _Clock, w0: float, w1: float,
                m_total: int, sr: int, key_lock: bool) -> dict | None:
    """One deck through stretch, EQ, filter; returns its post-filter signal, fader gain and send gains."""
    p0, p1 = max(deck["play_from_beat"], w0), min(deck["play_to_beat"], w1)
    if p1 <= p0:
        return None
    warn: list[str] = []
    spb = 60.0 / deck["native_bpm"]
    s0, s1 = p0 - 2.0, p1 + 1.0
    src, w = _source(audio, deck, s0, s1, sr)
    warn += w
    ft = np.asarray(deck_time(deck, np.linspace(p0, p1, 64)), dtype=float)
    if ft.min() < -0.05 or ft.max() > audio.shape[1] / sr + 0.05:
        warn.append("plays outside its audio file (silence there)")
    j = np.arange(src.shape[1])
    beats = s0 + j / (sr * spb)
    # the gated padding beats around the play range run at the edge speeds (no tempo step inside the stretcher; a
    # step right at an edge, e.g. a cut's tempo jump, is interpolated over one clock step)
    edge = 2 * _Clock.STEP
    speed = clock.bpm(np.clip(beats, p0 + edge, p1 - edge)) / deck["native_bpm"]
    audible = np.abs(speed[(beats >= p0) & (beats < p1)] - 1)
    if audible.size and audible.max() > 0.08:
        warn.append(f"tempo changed by up to {100 * audible.max():.0f}% (may sound stretched)")
    speed = np.clip(speed, 0.5, 2.0)
    fade_in = int(round(0.003 * sr)) if p0 > w0 else 0
    fade_out = int(round(0.004 * sr)) if p1 < w1 else 0
    t0, t1 = float(clock.time(p0)) * sr, float(clock.time(p1)) * sr
    qa = max(0, int(math.floor(t0)) - fade_in)
    tail = int(0.25 * sr)
    qb = min(m_total, int(math.ceil(t1)) + tail)
    q = np.arange(qa, qb)
    beats_q = clock.beat(q / sr)
    jq = (beats_q - s0) * sr * spb
    x, note = _to_master(src, speed.astype(np.float64), jq, sr, key_lock)
    if note:
        warn.append(note)
    # gate: the deck sounds on [p0, p1]
    g = np.ones(q.size, np.float32)
    if fade_in:
        g = np.minimum(g, np.clip((q - (t0 - fade_in)) / fade_in, 0, 1).astype(np.float32))
    g = np.minimum(g, np.clip((t1 - q) / max(fade_out, 1), 0, 1).astype(np.float32))
    g = (np.sin(0.5 * np.pi * g) ** 2).astype(np.float32)
    x *= g * trim
    if deck.get("fx"):
        x = _apply_fx(x, deck["fx"], clock, qa, sr)
    ctl_beats = clock.beat((qa + np.arange(0, q.size + CTL, CTL)) / sr)
    lanes = deck["lanes"]

    def lane(k: str, smooth: float = STEP_RAMP_S) -> np.ndarray:
        return _ctl(lanes[k], ctl_beats, smooth, sr, q.size)

    if any(kf["value"] != 0 for kf in lanes["filter"]):
        x = _dj_filter(x, lane("filter", FILTER_SMOOTH_S), sr)
    out = {"name": name, "qa": qa, "qb": qb, "volume": lane("volume"), "warnings": warn}
    if any(kf["value"] > 0 for kf in lanes["echo_send"]):  # send taps: post-filter, pre-EQ
        out["echo_send"] = lane("echo_send")
        out["echo_fb"] = lane("echo_feedback")
        out["send_x"] = x
    if any(kf["value"] > 0 for kf in lanes["reverb_send"]):
        out["reverb_send"] = lane("reverb_send")
        out["send_x"] = x
    if any(kf["value"] != 0 for k in ("eq_low", "eq_mid", "eq_high") for kf in lanes[k]):
        gl, gm, gh = (_iso_gain(lane(k)) for k in ("eq_low", "eq_mid", "eq_high"))
        x = _isolator(x, gl, gm, gh, sr)
    out["x"] = x
    return out


def _render_audio(plan: dict, a_path: Path, b_path: Path, sr: int = SR,
                  progress: Callable[[float], None] | None = None) -> dict:
    """Everything render() does except encoding; also returns the master mix and per-deck stems (for tests)."""
    def report(f: float) -> None:
        if progress is not None:
            try:
                progress(f)
            except Exception:  # noqa: BLE001 - a progress sink must never break a render
                pass

    norm = _normalise(plan)
    w0, w1 = norm["window"]["start_beat"], norm["window"]["end_beat"]
    decks = [norm["a"], norm["b"]]
    lo = min([w0] + [max(d["play_from_beat"], w0) - 3 for d in decks])
    clock = _Clock(norm["tempo"], lo, w1 + 2, w0)
    duration = float(clock.time(w1))
    m_total = int(round(duration * sr))
    if m_total < sr // 10:
        raise ValueError("plan window is too short to render")
    if duration > 600:
        raise ValueError("plan window is longer than 10 minutes")
    report(0.02)
    with ThreadPoolExecutor(2) as pool:
        audios = list(pool.map(lambda p: decode_stereo(Path(p), sr), (a_path, b_path)))
        trims = [10 ** (_clamp(DECK_LUFS - _track_lufs(Path(p), au, sr), -TRIM_LIMIT_DB, TRIM_LIMIT_DB) / 20)
                 for p, au in zip((a_path, b_path), audios)]
        report(0.1)
        futs = [pool.submit(_deck_stage, nm, d, au, tr, clock, w0, w1, m_total, sr, norm["key_lock"])
                for nm, d, au, tr in zip(("A", "B"), decks, audios, trims)]
        stages = [f.result() for f in futs]
    del audios
    report(0.7)
    mix = np.zeros((2, m_total), np.float32)
    stems: dict[str, np.ndarray] = {}
    rev_in = np.zeros((2, m_total), np.float32)
    warnings: list[str] = []
    for st, d in zip(stages, decks):
        if st is None:
            continue
        warnings += [f"deck {st['name']}: {w}" for w in st["warnings"]]
        qa, qb = st["qa"], st["qb"]
        dry = st["x"] * st["volume"]
        stem = np.zeros((2, m_total), np.float32)
        stem[:, qa:qb] = dry
        if "echo_send" in st:
            send = st["send_x"] * st["echo_send"]
            first = int(np.argmax(st["echo_send"] > 1e-4))
            e0 = qa + first
            ein = np.zeros((2, m_total - e0), np.float32)
            ein[:, : qb - e0] = send[:, first:]
            fb = np.full(m_total - e0, st["echo_fb"][-1], np.float32)
            fb[: qb - e0] = st["echo_fb"][first:]
            beats = clock.beat(np.arange(e0, m_total) / sr)
            delay = d["echo_beats"] * 60.0 / clock.bpm(beats) * sr
            ret = ECHO_RETURN * _echo(ein, delay, fb, sr)
            stems[f"{st['name']}_echo"] = np.zeros((2, m_total), np.float32)
            stems[f"{st['name']}_echo"][:, e0:] = ret
            stem[:, e0:] += ret
        if "reverb_send" in st:
            rev_in[:, qa:qb] += st["send_x"] * st["reverb_send"]
        stems[st["name"]] = stem
        mix += stem
    report(0.8)
    if rev_in.any():
        first = int(np.argmax(np.abs(rev_in).max(axis=0) > 0))
        rv = np.zeros_like(mix)
        rv[:, first:] = REVERB_RETURN * _reverb(rev_in[:, first:], sr)
        stems["reverb"] = rv
        mix += rv
    del rev_in
    ref_power = 10 ** ((DECK_LUFS + 0.691) / 10)
    gain, lev_red = _level(mix, ref_power, sr)
    mix *= gain
    mix, lim_red = _limit(mix, CEILING_DBTP)
    fi, fo = int(0.005 * sr), int(0.03 * sr)
    mix[:, :fi] *= np.linspace(0, 1, fi, dtype=np.float32)
    mix[:, -fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
    if lim_red > 8:
        warnings.append(f"limiter worked hard ({lim_red:.1f} dB): the overlap is very loud")
    beats = np.arange(math.ceil(w0 - 1e-9), math.floor(w1 + 1e-9) + 1)
    report(0.9)
    return {
        "mix": mix, "stems": stems, "gain": gain, "sr": sr, "clock": clock, "norm": norm,
        "duration_s": round(m_total / sr, 4),
        "start_beat": w0,
        "beat_times_s": [round(float(t), 4) for t in clock.time(beats)],
        "lufs": round(integrated_lufs(mix, sr), 2),
        "true_peak_db": round(true_peak_db(mix), 2),
        "warnings": warnings,
        "leveller_db": round(lev_red, 2), "limiter_db": round(lim_red, 2),
    }


def render(plan: dict, a_path: Path, b_path: Path, out_path: Path, sr: int = SR,
           progress: Callable[[float], None] | None = None) -> dict:
    """Render `plan` (deck a from a_path, deck b from b_path) to an AAC m4a at out_path. Returns the MixRender
    fields except audio_url / render_id. ValueError for an unusable plan or undecodable audio."""
    r = _render_audio(plan, a_path, b_path, sr, progress)
    encode_m4a(r["mix"], sr, Path(out_path))
    if progress is not None:
        try:
            progress(1.0)
        except Exception:  # noqa: BLE001
            pass
    return {k: r[k] for k in ("duration_s", "start_beat", "beat_times_s", "lufs", "true_peak_db", "warnings")}


try:  # one source of truth for lane / loop semantics once mixplan is importable
    from .mixplan import deck_time as _mp_deck_time
    from .mixplan import lane_value as _mp_lane_value
except Exception:  # noqa: BLE001 - keep the private equivalents above
    pass
else:
    lane_value = _mp_lane_value  # noqa: F811
    deck_time = _mp_deck_time  # noqa: F811
