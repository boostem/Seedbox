"""Audio in pieces: decoding only the part of a file a render needs, and the DSP stages as objects that keep their state
between chunks, so that no stage holds a whole track, or a float64 copy of one (#69).

Each stage takes float32 chunks and gives float32 chunks. The filters work in float64 inside a chunk, because recursive
filters at low cutoffs need the precision, but that scratch is at most `CHUNK` samples long. Cutting the audio into
chunks of another size doesn't change the output, which the tests hold them to.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

from musicreco.paths import ffmpeg_path

SR = 44100
CHUNK = 16384  # samples; a multiple of the leveller's hop
DECODE_TIMEOUT_S = 300
PREROLL_S = 0.5  # decoded before a window and thrown away: decoders and resamplers need a moment to settle after a seek
# ffmpeg lands exactly where it was asked to in these (tests/test_seedbox2_stream.py, bit for bit). In M4A, Ogg and WebM
# it lands up to 1000 samples off, so those are decoded from their start, with the part before the window thrown away.
EXACT_SEEK = frozenset({".mp3", ".flac", ".wav", ".aif", ".aiff"})


def _pcm(path: Path, sr: int, start_s: float, dur_s: float | None) -> tuple[subprocess.Popen, object, threading.Timer]:
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise ValueError("ffmpeg not found")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.6f}"]
    cmd += ["-i", str(path), "-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(sr)]
    if dur_s is not None:
        cmd += ["-t", f"{dur_s:.6f}"]
    cmd += ["-f", "f32le", "-"]
    err = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err)
    except OSError as e:
        err.close()
        raise ValueError(f"could not decode {Path(path).name}: {e}") from e
    timer = threading.Timer(DECODE_TIMEOUT_S, proc.kill)
    timer.daemon = True
    timer.start()
    return proc, err, timer


def _finish(path: Path, proc: subprocess.Popen, err, timer: threading.Timer, check: bool = True) -> None:
    timer.cancel()
    proc.stdout.close()
    code = proc.wait()
    err.seek(0)
    tail = err.read().decode(errors="replace").strip().splitlines()[-1:]
    err.close()
    if check and code != 0:
        raise ValueError(f"could not decode {Path(path).name}: {tail[0] if tail else 'no output'}")


def _fill(stream, frames: np.ndarray) -> int:
    """Reads into `frames` (n, 2) until it is full or the stream ends. How many whole frames it got."""
    view = memoryview(frames).cast("B")
    got = 0
    while got < view.nbytes:
        k = stream.readinto(view[got:])
        if not k:
            break
        got += k
    return got // 8


def _read_window(path: Path, first: int, n: int, sr: int) -> tuple[np.ndarray, int]:
    """((2, n) float32 from sample `first`, silence outside the file; how many samples up to the window's end the file
    has, counted from `first`)."""
    out = np.zeros((n, 2), np.float32)
    lead = max(0, -first)
    if lead >= n:
        return out.T.copy(), n
    begin = first + lead
    if Path(path).suffix.lower() in EXACT_SEEK:
        pre = min(begin, int(round(PREROLL_S * sr)))
    else:
        pre = begin
    scratch = np.empty((max(1, min(CHUNK, pre)), 2), np.float32)
    got = 0
    proc, err, timer = _pcm(path, sr, (begin - pre) / sr, (pre + n - lead) / sr)
    try:
        left = pre
        while left > 0:
            k = _fill(proc.stdout, scratch[: min(left, scratch.shape[0])])
            if not k:
                break
            left -= k
        if left <= 0:
            got = _fill(proc.stdout, out[lead:])
        while _fill(proc.stdout, scratch):  # -t can round up a sample: closing the pipe on it would fail ffmpeg
            pass
    finally:
        _finish(path, proc, err, timer)
    return out.T.copy(), lead + got


def decode_window(path: Path, start_s: float, dur_s: float, sr: int = SR) -> np.ndarray:
    """(2, round(dur_s * sr)) float32: the file's audio from `start_s`, lined up to the sample with a decode of the
    whole file. Silence where the window is outside the file. ValueError if the file can't be decoded."""
    return _read_window(path, int(round(start_s * sr)), int(round(dur_s * sr)), sr)[0]


def iter_decode(path: Path, sr: int = SR, chunk: int = CHUNK) -> Iterator[np.ndarray]:
    """The whole file as (2, k) float32 chunks of `chunk` samples, the last one shorter. Each chunk is a new array.
    ValueError if the file can't be decoded."""
    proc, err, timer = _pcm(path, sr, 0.0, None)
    try:
        while True:
            frames = np.empty((chunk, 2), np.float32)
            k = _fill(proc.stdout, frames)
            if not k:
                break
            yield frames[:k].T.copy()
    except GeneratorExit:
        _finish(path, proc, err, timer, check=False)
        raise
    _finish(path, proc, err, timer)


# --- loudness --------------------------------------------------------------------------------------------------------


def k_weight_sos(sr: int) -> np.ndarray:
    """BS.1770 K-weighting: a high shelf and the RLB high-pass."""
    a = 10 ** (4.0 / 40)
    w0 = 2 * np.pi * 1500.0 / sr
    al = np.sin(w0) / (2 * (1 / np.sqrt(2)))
    c, sa = np.cos(w0), 2 * np.sqrt(a) * al
    shelf = [a * ((a + 1) + (a - 1) * c + sa), -2 * a * ((a - 1) + (a + 1) * c), a * ((a + 1) + (a - 1) * c - sa),
             (a + 1) - (a - 1) * c + sa, 2 * ((a - 1) - (a + 1) * c), (a + 1) - (a - 1) * c - sa]  # fmt: skip
    w0 = 2 * np.pi * 38.0 / sr
    al, c = np.sin(w0) / (2 * 0.5), np.cos(w0)
    hp = [(1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + al, -2 * c, 1 - al]
    return np.array([np.r_[np.array(s[:3]) / s[3], 1.0, np.array(s[4:]) / s[3]] for s in (shelf, hp)])


class Sos:
    """A cascade of biquads that remembers its state between calls. Takes float32, gives float64 of the same length:
    the caller decides what to do with the extra precision."""

    def __init__(self, sos: np.ndarray, channels: int = 2) -> None:
        self.sos = np.asarray(sos, np.float64)
        self.zi = np.zeros((self.sos.shape[0], channels, 2))

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = signal.sosfilt(self.sos, x, axis=-1, zi=self.zi)
        return y


class HopPower:
    """The channel-summed power of K-weighted audio, added up per `hop` samples: all that loudness needs to keep of it.
    The total of the last hop, if it is partial, is kept too."""

    def __init__(self, sr: int, hop: int) -> None:
        self.k = Sos(k_weight_sos(sr))
        self.hop = hop
        self.done: list[np.ndarray] = []
        self.partial = 0.0
        self.fill = 0

    def feed(self, x: np.ndarray) -> None:
        p = (self.k(x) ** 2).sum(axis=0)
        i = 0
        if self.fill:
            i = min(p.size, self.hop - self.fill)
            self.partial += float(p[:i].sum())
            self.fill += i
            if self.fill < self.hop:
                return
            self.done.append(np.array([self.partial]))
            self.partial, self.fill = 0.0, 0
        whole = (p.size - i) // self.hop
        if whole:
            self.done.append(p[i : i + whole * self.hop].reshape(whole, self.hop).sum(axis=1))
        rest = p[i + whole * self.hop :]
        if rest.size:
            self.partial, self.fill = float(rest.sum()), rest.size

    def hops(self) -> np.ndarray:
        return np.concatenate(self.done) if self.done else np.zeros(0)


class Loudness:
    """BS.1770 loudness of audio fed in chunks: 400 ms blocks every 100 ms."""

    def __init__(self, sr: int) -> None:
        self.hp = HopPower(sr, int(round(0.1 * sr)))

    def feed(self, x: np.ndarray) -> None:
        self.hp.feed(x)

    def _blocks(self) -> np.ndarray:
        h, hop = self.hp.hops(), self.hp.hop
        if h.size < 4:
            count = h.size * hop + self.hp.fill
            return np.array([(h.sum() + self.hp.partial) / count]) if count else np.zeros(1)
        c = np.concatenate([[0.0], np.cumsum(h)])
        return (c[4:] - c[:-4]) / (4 * hop)

    def ungated(self) -> float:
        """Above the -70 LUFS gate only: a quiet intro next to a loud phrase still counts. -70 for silence."""
        z = self._blocks()
        z = z[-0.691 + 10 * np.log10(z + 1e-12) > -70]
        return float(-0.691 + 10 * np.log10(z.mean())) if z.size else -70.0

    def integrated(self) -> float:
        """BS.1770-4 integrated loudness. -70 for silence."""
        z = self._blocks()
        z = z[-0.691 + 10 * np.log10(z + 1e-12) > -70]
        if not z.size:
            return -70.0
        rel = -0.691 + 10 * np.log10(z.mean()) - 10
        z = z[-0.691 + 10 * np.log10(z) > rel]
        return float(-0.691 + 10 * np.log10(z.mean())) if z.size else -70.0


def feed_loudness(meter: Loudness, x: np.ndarray, every: int = 1) -> None:
    """Feeds (2, n) audio to `meter` a chunk at a time, keeping every `every`-th sample."""
    for a in range(0, x.shape[1], CHUNK):
        meter.feed(x[:, a : a + CHUNK : every])


def measure_track(path: Path, sr: int = SR) -> float:
    """Integrated loudness of a whole file, at half the sample rate (plenty for a gain trim), one chunk at a time."""
    half = sr % 2 == 0
    meter = Loudness(sr // 2 if half else sr)
    got = False
    for chunk in iter_decode(path, sr):
        got = True
        meter.feed(chunk[:, ::2] if half else chunk)
    if not got:
        raise ValueError(f"no audio in {Path(path).name}")
    return meter.integrated()


# --- true peak -------------------------------------------------------------------------------------------------------

_PEAK_MARGIN = 11  # the 4x interpolation filter reaches this many samples either way


def peaks_4x(x: np.ndarray, a: int, b: int) -> np.ndarray:
    """The 4x-oversampled peak of (2, n) audio at each sample in [a, b), across channels. float32."""
    lo, hi = max(0, a - _PEAK_MARGIN), min(x.shape[1], b + _PEAK_MARGIN)
    up = np.abs(signal.resample_poly(x[:, lo:hi], 4, 1, axis=-1)).max(axis=0)
    return up[(a - lo) * 4 : (b - lo) * 4].reshape(-1, 4).max(axis=1)


def true_peak_db(x: np.ndarray) -> float:
    """4x-oversampled peak (dBTP) of (2, n) audio."""
    peak = 0.0
    for a in range(0, x.shape[1], CHUNK):
        peak = max(peak, float(peaks_4x(x, a, min(x.shape[1], a + CHUNK)).max()))
    return float(20 * np.log10(peak + 1e-12))


# --- the mixer's stages ----------------------------------------------------------------------------------------------

XOVER_LOW_HZ, XOVER_HIGH_HZ = 200.0, 2500.0
FILTER_LP_MIN_HZ, FILTER_LP_MAX_HZ = 80.0, 20000.0
FILTER_HP_MIN_HZ, FILTER_HP_MAX_HZ = 20.0, 10000.0
FILTER_Q_BASE = 1.3066  # the resonant stage's Q at filter_res 0 (Butterworth), plus a touch as the filter closes
FILTER_Q_MAX = 5.0  # its Q at filter_res 1: a ~+14 dB peak before the level compensation (safe limit)
FILTER_RES_ONSET = 0.2  # resonance fades in over |filter| 0..this (no boost at 20 Hz / 20 kHz, where it's just mud)
FILTER_BLOCK = 64
REVERB_BAND_HZ = (150.0, 9000.0)  # the reverb send is band-limited to this (no sub rumble, no fizz)
LEVEL_HOP = 1024


class Lane:
    """A lane at control rate, read a chunk of samples at a time: what the mixer would otherwise keep per sample. Steps
    are softened by a centred moving average of `smooth_s`."""

    def __init__(self, values: np.ndarray, ctl: int, smooth_s: float, sr: int) -> None:
        v = np.asarray(values, dtype=np.float64)
        w = max(1, int(round(smooth_s * sr / ctl)))
        if w > 1 and np.ptp(v) > 0:
            v = ndimage.uniform_filter1d(v, w, mode="nearest")
        self.v, self.ctl = v, ctl
        self.flat = float(v[0]) if np.ptp(v) == 0 else None

    def at(self, a: int, b: int) -> np.ndarray:
        """float32 values at samples [a, b)."""
        if self.flat is not None:
            return np.full(b - a, self.flat, np.float32)
        i0 = a // self.ctl
        i1 = min(self.v.size, (b - 1) // self.ctl + 2)
        return np.interp(np.arange(a, b), np.arange(i0, i1) * self.ctl, self.v[i0:i1]).astype(np.float32)

    def samples(self, idx: np.ndarray) -> np.ndarray:
        """Values at sample numbers `idx`, as float64."""
        return np.interp(idx, np.arange(self.v.size) * self.ctl, self.v)


def _allpass_sos(fc: float, sr: int) -> np.ndarray:
    w0 = 2 * np.pi * fc / sr
    al, c = np.sin(w0) / (2 * np.sqrt(0.5)), np.cos(w0)
    return np.array([[(1 - al) / (1 + al), -2 * c / (1 + al), 1.0, 1.0, -2 * c / (1 + al), (1 - al) / (1 + al)]])


def iso_gain(db: np.ndarray) -> np.ndarray:
    """Isolator band gain: dB below -24 fades to a true kill at -30."""
    db = np.asarray(db, dtype=np.float64)
    return (10 ** (db / 20) * np.clip((db + 30.0) / 6.0, 0.0, 1.0)).astype(np.float32)


class Isolator:
    """3-band DJ isolator: LR4 split at 200 Hz / 2.5 kHz, low band all-pass compensated (bands sum flat)."""

    def __init__(self, sr: int) -> None:
        lp1 = signal.butter(2, XOVER_LOW_HZ, "lowpass", fs=sr, output="sos")
        hp1 = signal.butter(2, XOVER_LOW_HZ, "highpass", fs=sr, output="sos")
        lp2 = signal.butter(2, XOVER_HIGH_HZ, "lowpass", fs=sr, output="sos")
        hp2 = signal.butter(2, XOVER_HIGH_HZ, "highpass", fs=sr, output="sos")
        self.low = Sos(np.vstack([lp1, lp1, _allpass_sos(XOVER_HIGH_HZ, sr)]))
        self.rest = Sos(np.vstack([hp1, hp1]))
        self.mid = Sos(np.vstack([lp2, lp2]))
        self.high = Sos(np.vstack([hp2, hp2]))

    def __call__(self, x: np.ndarray, gl: np.ndarray | float, gm: np.ndarray | float,
                 gh: np.ndarray | float) -> np.ndarray:  # fmt: skip
        rest = self.rest(x)
        return (self.low(x) * gl + self.mid(rest) * gm + self.high(rest) * gh).astype(np.float32)


def _biquads(kind: str, fc: np.ndarray, q: np.ndarray, sr: int) -> np.ndarray:
    """RBJ low/high-pass biquads, vectorised: (len(fc), 6) normalised sos rows."""
    w0 = 2 * np.pi * np.clip(fc, 10.0, 0.45 * sr) / sr
    c, al = np.cos(w0), np.sin(w0) / (2 * q)
    a0 = 1 + al
    b = np.stack([(1 - c) / 2, 1 - c, (1 - c) / 2] if kind == "lp" else [(1 + c) / 2, -(1 + c), (1 + c) / 2], axis=1)
    a = np.stack([np.ones_like(c), -2 * c / a0, (1 - al) / a0], axis=1)
    return np.concatenate([b / a0[:, None], a], axis=1)


def _sos_in_place(sos: np.ndarray, x: np.ndarray, zi: np.ndarray) -> None:
    """Filters x (channels, k), float64, in place. zi is (channels, sections, 2). Slower, public way; see below."""
    y, z = signal.sosfilt(sos, x, axis=-1, zi=np.moveaxis(zi, 1, 0))
    x[:] = y
    zi[:] = np.moveaxis(z, 0, 1)


try:
    # What signal.sosfilt calls after checking its arguments. Those checks are 90% of the time of a call on 64 samples,
    # and a filter sweep makes 700 calls per second of audio per deck.
    from scipy.signal._sosfilt import _sosfilt as _sos_in_place  # noqa: F811
except ImportError:
    pass


class _FilterSide:
    """The low-pass or the high-pass half of `DjFilter`: coefficients per block, grouped into runs of equal ones."""

    def __init__(self, kind: str, sel: np.ndarray, mag: np.ndarray, q2: np.ndarray, comp: np.ndarray, sr: int) -> None:
        self.kind = kind
        self.active = ndimage.binary_dilation(sel & (mag >= 0.003), iterations=24)
        self.used = bool(self.active.any())
        if not self.used:
            return
        m = np.where(sel, mag, 0.0)
        if kind == "lp":
            fc = FILTER_LP_MAX_HZ * (FILTER_LP_MIN_HZ / FILTER_LP_MAX_HZ) ** m
        else:
            fc = FILTER_HP_MIN_HZ * (FILTER_HP_MAX_HZ / FILTER_HP_MIN_HZ) ** m
        s1 = _biquads(kind, fc, np.full_like(fc, 0.5412), sr)
        s2 = _biquads(kind, fc, q2, sr)
        s2[:, :3] *= comp[:, None]
        self.sos = np.stack([s1, s2], axis=1)  # (blocks, 2, 6)
        # a block starts a new stretch of coefficients where they change, and a silent start where a run of active
        # blocks begins
        self.changed = np.r_[True, (fc[1:] != fc[:-1]) | (q2[1:] != q2[:-1])]
        self.starts_run = self.active & ~np.r_[False, self.active[:-1]]
        self.zi = np.zeros((2, 2, 2))

    def __call__(self, x: np.ndarray, a: int, b: int) -> np.ndarray | None:
        """Its output for x, which is samples [a, b); None if no block in them is active."""
        k0, k1 = a // FILTER_BLOCK, (b - 1) // FILTER_BLOCK + 1
        if not self.active[k0:k1].any():
            return None
        y = np.zeros(x.shape, np.float64)
        k = k0
        while k < k1:
            if not self.active[k]:
                k += 1
                continue
            if self.starts_run[k] and k * FILTER_BLOCK >= a:
                self.zi[:] = 0.0
            k2 = k + 1
            while k2 < k1 and self.active[k2] and not self.changed[k2] and not self.starts_run[k2]:
                k2 += 1
            i, j = max(a, k * FILTER_BLOCK), min(b, k2 * FILTER_BLOCK)
            seg = x[:, i - a : j - a].astype(np.float64)
            _sos_in_place(self.sos[k], seg, self.zi)
            y[:, i - a : j - a] = seg
            k = k2
        return y


class DjFilter:
    """Bipolar filter (f < 0 low-pass to 80 Hz, f > 0 high-pass to 10 kHz), 24 dB/oct, resonance 0..1 (`res`, light by
    default): the second stage's Q goes up to FILTER_Q_MAX, faded in over the first FILTER_RES_ONSET of the sweep, with
    its level brought down by the 4th root of the Q gain: the peak stays within ~+8 dB of the dry signal. Coefficients
    per 64-sample block from the smoothed lanes; dry below |f| = 0.03 with a short blend."""

    def __init__(self, f: Lane, res: Lane | None, n: int, sr: int) -> None:
        self.f = f
        nb = (n + FILTER_BLOCK - 1) // FILTER_BLOCK
        mid = np.minimum(np.arange(nb) * FILTER_BLOCK + FILTER_BLOCK // 2, n - 1)
        # float32, as the per-sample values these blocks sit on
        fb = f.samples(mid).astype(np.float32).astype(np.float64)
        mag = np.abs(fb)
        q_light = FILTER_Q_BASE + 0.6 * np.clip(mag, 0, 1) ** 1.5
        r = np.zeros(nb) if res is None else np.clip(res.samples(mid).astype(np.float32).astype(np.float64), 0.0, 1.0)
        r = np.round(r * np.clip(mag / FILTER_RES_ONSET, 0.0, 1.0), 3)  # rounded: holds merge into one filter call
        q2 = q_light + r * np.maximum(FILTER_Q_MAX - q_light, 0.0)
        comp = (q_light / q2) ** 0.25
        sides = (_FilterSide("lp", fb < 0, mag, q2, comp, sr), _FilterSide("hp", fb > 0, mag, q2, comp, sr))
        self.sides = [s for s in sides if s.used]

    def __call__(self, x: np.ndarray, a: int) -> np.ndarray:
        """x (2, k) float32, samples [a, a + k) of the deck, filtered. Float32; x itself if nothing happens to it."""
        b = a + x.shape[1]
        if not self.sides:
            return x
        f = self.f.at(a, b)
        out = x
        for s in self.sides:
            side = (f < 0) if s.kind == "lp" else (f > 0)
            y = s(x, a, b)  # it has to run even where it isn't heard: it settles on the blocks before it is
            if side.any():
                # where the filter hasn't started yet its blend is already under way, against silence
                w = np.where(side, np.clip(np.abs(f) / 0.03, 0.0, 1.0), 0.0)
                out = np.where(side, out * (1 - w) + (0.0 if y is None else y * w), out).astype(np.float32)
        return out


class Echo:
    """Feedback delay (repeats only) with a band-limiting filter in the loop: every repeat gets thinner/darker. Keeps as
    much history as the longest delay."""

    MAX_BLOCK = 1024

    def __init__(self, max_delay: float, sr: int) -> None:
        hp = signal.butter(2, 220.0, "highpass", fs=sr, output="sos")
        lp = signal.butter(2, 6000.0, "lowpass", fs=sr, output="sos")
        self.filter = Sos(np.vstack([hp, lp]))
        self.size = int(max_delay) + self.MAX_BLOCK + 8
        self.w = np.zeros((2, self.size), np.float32)
        self.pos = 0

    def __call__(self, x: np.ndarray, delay: np.ndarray, fb: np.ndarray) -> np.ndarray:
        """x (2, k) float32 in, delay (k) in samples, fb (k): (2, k) float32 out."""
        k = x.shape[1]
        out = np.empty((2, k), np.float32)
        blk = int(max(32, min(self.MAX_BLOCK, np.floor(delay.min()) - 2)))
        for a in range(0, k, blk):
            b = min(k, a + blk)
            p = self.pos + a
            r = np.arange(p, p + b - a) - delay[a:b]
            i = np.floor(r).astype(np.int64)
            fr = r - i
            d = (self.w[:, i % self.size] * (1 - fr) + self.w[:, (i + 1) % self.size] * fr) * (i >= 0)
            y = self.filter(d)
            out[:, a:b] = y
            self.w[:, np.arange(p, p + b - a) % self.size] = x[:, a:b] + y * fb[a:b]
        self.pos += k
        return out


class Reverb:
    """Wash: band-limited send into a plate-ish reverb (wet only); size 0..1 goes from a short room to a long, brighter
    hall."""

    BLOCK = 8192

    def __init__(self, size: float, sr: int) -> None:
        import pedalboard

        lo, hi = REVERB_BAND_HZ
        self.band = Sos(np.vstack([signal.butter(2, lo, "highpass", fs=sr, output="sos"),
                                   signal.butter(2, hi, "lowpass", fs=sr, output="sos")]))  # fmt: skip
        self.rv = pedalboard.Reverb(room_size=0.6 + 0.37 * size, damping=0.6 - 0.3 * size, wet_level=1.0, dry_level=0.0,
                                    width=1.0)  # fmt: skip
        self.sr = sr
        # pedalboard wipes a plugin's state when a call brings a larger block than any before it, whatever `reset` says.
        # This call sets the largest block, and silence leaves no trace in the reverb.
        self.rv.process(np.zeros((2, self.BLOCK), np.float32), sr, buffer_size=self.BLOCK, reset=True)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        wet = self.band(x).astype(np.float32)
        return self.rv.process(wet, self.sr, buffer_size=self.BLOCK, reset=False).astype(np.float32)


# --- the bus ---------------------------------------------------------------------------------------------------------


class Leveller:
    """Slow zero-phase leveller: above (deck reference + threshold) the level is compressed `ratio`:1, then make-up
    gain. It reads the mix first and works out a gain per hop, which `gain` gives back per sample."""

    def __init__(self, ref_power: float, threshold_db: float, ratio: float, makeup_db: float, sr: int) -> None:
        self.ref_power, self.threshold_db, self.ratio = ref_power, threshold_db, ratio
        self.makeup_db, self.sr = makeup_db, sr
        self.power = HopPower(sr, LEVEL_HOP)

    def feed(self, x: np.ndarray) -> None:
        self.power.feed(x)

    def finish(self) -> float:
        """Works out the gains. The most it takes off, in dB."""
        hops = self.power.hops()
        if self.power.fill:
            hops = np.r_[hops, self.power.partial]
        hop, sr = LEVEL_HOP, self.sr
        pb = ndimage.uniform_filter1d(hops / hop, max(1, int(1.5 * sr / hop)), mode="nearest")
        lvl = 10 * np.log10(pb + 1e-12) - 10 * np.log10(self.ref_power)
        red = -np.maximum(lvl - self.threshold_db, 0.0) * (1 - 1 / self.ratio)
        red = ndimage.uniform_filter1d(red, max(1, int(1.0 * sr / hop)), mode="nearest")
        self.g_db = red + self.makeup_db
        self.centres = np.arange(pb.size) * hop + hop / 2
        return float(-red.min()) if red.size else 0.0

    def gain(self, a: int, b: int) -> np.ndarray:
        """float32 gains at samples [a, b)."""
        return (10 ** (np.interp(np.arange(a, b), self.centres, self.g_db) / 20)).astype(np.float32)


LIMIT_WINDOW_S = 0.025


def limit(x: np.ndarray, ceiling_db: float, sr: int) -> float:
    """Look-ahead true-peak limiter on (2, n) float32, in place (offline, zero-phase gain envelope: min over 25 ms, then
    averaged). How much it took off at most, in dB."""
    c = 10 ** (ceiling_db / 20)
    n = x.shape[1]
    req = np.empty(n, np.float32)
    for a in range(0, n, CHUNK):
        b = min(n, a + CHUNK)
        req[a:b] = np.minimum(1.0, c / np.maximum(peaks_4x(x, a, b), 1e-9))
    if req.min() >= 1.0:
        return 0.0
    w = int(LIMIT_WINDOW_S * sr) | 1
    reach = 2 * w  # the two filters' reach
    least = 1.0
    for a in range(0, n, CHUNK):
        b = min(n, a + CHUNK)
        lo, hi = max(0, a - reach), min(n, b + reach)
        r = req[lo:hi]
        if r.min() >= 1.0:  # no peak this chunk's gain can see
            np.clip(x[:, a:b], -c, c, out=x[:, a:b])
            continue
        g = ndimage.uniform_filter1d(ndimage.minimum_filter1d(r, w, mode="nearest"), w, mode="nearest")
        g = np.minimum(g, ndimage.minimum_filter1d(r, 3, mode="nearest"))[a - lo : b - lo]  # float safety at lone peaks
        x[:, a:b] = np.clip(x[:, a:b] * g, -c, c)
        least = min(least, float(g.min()))
    return float(-20 * np.log10(least))
