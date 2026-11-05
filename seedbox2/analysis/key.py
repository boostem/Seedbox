"""Key detection (#45): constant-Q chroma of the harmonic stems, matched against Faraldo's EDM key profiles.

1. **Signal**: the bass and other stems (no drums, no vocals) when the track has them, else the mix, mono at 11,025 Hz.
2. **Tuning**: the spectral peaks' position within a semitone (`estimate_tuning`), so a track at A=432 still lands on
   the right bins. Only a clear offset counts: a small or scattered estimate is left at A=440.
3. **Chroma**: a constant-Q transform, one bin per tuned semitone from C2 to C7 (`cq_frames`), log-compressed, averaged
   per beat of the track's `beats` analysis (every 0.5 s without one) and scaled to a peak of 1 per beat.
4. **Global key**: Pearson correlation of the mean chroma with the 24 rotations of a profile (`PROFILE`), plus
   MINOR_PRIOR for the minor keys (85 % of GiantSteps is minor, and with no prior same-tonic major is the commonest
   mistake).
5. **Sections**: the same match on 8-bar blocks, smoothed with Viterbi; a stretch of at least MIN_CHANGE_BARS bars
   that clearly fits another key is a key change. Sections (#46) aren't there yet, so these blocks stand in for them.
6. **Confidence**: `confidence` (0-1) from the best correlation and its margin over the runner-up, fitted on half of
   GiantSteps Key. Below MIN_CONFIDENCE the key is unknown: `key`, `camelot` and `open_key` are None (shown as "?", and
   `planning_key` gives the planner None); `best_guess` still says what it would have been.

The result (`analyse`, stored as the track's `key` analysis):

    {"version", "key": "A minor", "camelot": "8A", "open_key": "1m", "confidence", "best_guess": {key, camelot,
     open_key}, "source": "stems"|"mix", "beat_synchronous", "tuning_cents", "profile", "changes": [{start_s, end_s,
     key, camelot, open_key, confidence}], "duration_s"}

`changes` is empty for a track in one key. The user's own key is the `key_override` analysis (`set_override`): it
survives re-analysis and wins everywhere (`effective`, `planning_key`, the library view).

Profiles: Faraldo, Gómez, Jordà & Herrera, "Key Estimation in Electronic Dance Music" (ECIR 2016) and "A
Comparison of Profiles for EDM Key Estimation" (2017), with the values of Essentia's Key algorithm.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
from scipy import fft as sfft

from seedbox2.jobs import register

log = logging.getLogger(__name__)

ANALYSER = "key"
OVERRIDE = "key_override"
VERSION = "1"
OVERRIDE_VERSION = "1"

SAMPLE_RATE = 11_025
LOW_MIDI = 36  # C2: below it the kick's pitch outweighs the bass line
OCTAVES = 5  # up to C7
WIDTH_SEMITONES = 1.0  # each bin's Hann window in frequency, full width
MIN_RATE = 16.0  # Hz: the least time resolution of a bin's envelope
LOG_GAIN = 10_000.0  # chroma = log(1 + gain * power / the track's peak power)
FALLBACK_FRAME_S = 0.5  # the "beat" of a track without a beats analysis

TUNING_MIN_CENTS = 12.0  # a smaller estimate is treated as A=440
TUNING_MIN_FOCUS = 0.1  # the peaks must agree this much (0-1) for the estimate to count

# tonic first, major then minor (Essentia: src/algorithms/tonal/key.cpp)
PROFILES = {
    "edma": (np.array([1.00, 0.29, 0.50, 0.40, 0.60, 0.56, 0.32, 0.80, 0.31, 0.45, 0.42, 0.39]),
             np.array([1.00, 0.31, 0.44, 0.58, 0.33, 0.49, 0.29, 0.78, 0.43, 0.29, 0.53, 0.32])),
    "bgate": (np.array([1.00, 0.00, 0.42, 0.00, 0.53, 0.37, 0.00, 0.77, 0.00, 0.38, 0.21, 0.30]),
              np.array([1.00, 0.00, 0.36, 0.39, 0.00, 0.38, 0.00, 0.74, 0.27, 0.00, 0.42, 0.23])),
}  # fmt: skip
PROFILE = "edma"
MINOR_PRIOR = 0.10  # added to the minor keys' correlations

# confidence = sigmoid(A + B * correlation + C * margin), the chance of the right key or a fifth (fitted on half of GiantSteps)
CONFIDENCE_A, CONFIDENCE_B, CONFIDENCE_C = -4.43, 5.98, 2.06
MIN_CONFIDENCE = 0.4

BLOCK_BEATS = 32  # 8 bars
CHANGE_COST = 0.6  # Viterbi: what leaving a key costs, in summed correlation
MIN_CHANGE_BARS = 16
MIN_CHANGE_GAIN = 0.15  # how much better the section's key must fit it than the global key does

NAMES = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
_MINOR_CAMELOT = {9: 8, 4: 9, 11: 10, 6: 11, 1: 12, 8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7}  # pitch class -> number
_CAMELOT = re.compile(r"^(\d{1,2})([AB])$")

# --- notation ------------------------------------------------------------------------------------------------------
# A key's index: 0-11 are the major keys by tonic pitch class, 12-23 the minor keys.


def camelot_of(index: int) -> str:
    tonic, minor = index % 12, index >= 12
    return f"{_MINOR_CAMELOT[tonic]}A" if minor else f"{_MINOR_CAMELOT[(tonic + 9) % 12]}B"


def open_key_of(camelot: str) -> str | None:
    """Traktor's Open Key for a Camelot code: 8A is 1m, 8B is 1d."""
    m = _CAMELOT.match(camelot or "")
    if not m or not 1 <= int(m[1]) <= 12:
        return None
    return f"{(int(m[1]) + 4) % 12 + 1}{'m' if m[2] == 'A' else 'd'}"


def index_of_camelot(camelot: str) -> int | None:
    m = _CAMELOT.match(camelot or "")
    if not m or not 1 <= int(m[1]) <= 12:
        return None
    minor_pc = next(pc for pc, n in _MINOR_CAMELOT.items() if n == int(m[1]))
    return 12 + minor_pc if m[2] == "A" else (minor_pc + 3) % 12


def notations(index: int) -> dict:
    """{key: "A minor", camelot: "8A", open_key: "1m"}."""
    camelot = camelot_of(index)
    return {"key": f"{NAMES[index % 12]} {'minor' if index >= 12 else 'major'}", "camelot": camelot,
            "open_key": open_key_of(camelot)}  # fmt: skip


# --- chroma --------------------------------------------------------------------------------------------------------


def estimate_tuning(x: np.ndarray, sr: int = SAMPLE_RATE, fmin: float = 80.0, fmax: float = 2000.0) -> float:
    """The offset from A=440 in cents (-50 to 50), 0 unless it is clear: the circular mean of the strongest spectral
    peaks' positions within a semitone, weighted by level, over up to a minute from the middle of the track."""
    n = min(x.size, sr * 60)
    if n < sr:
        return 0.0
    start = (x.size - n) // 2
    p = np.abs(sfft.rfft(x[start:start + n] * np.hanning(n)))
    f = np.arange(p.size) * sr / n
    lo, hi = np.searchsorted(f, [fmin, min(fmax, sr / 2 - 1)])
    if hi - lo < 3:
        return 0.0
    peaks = lo + 1 + np.nonzero((p[lo + 1:hi - 1] > p[lo:hi - 2]) & (p[lo + 1:hi - 1] >= p[lo + 2:hi]))[0]
    if peaks.size == 0:
        return 0.0
    keep = peaks[np.argsort(p[peaks])[-400:]]
    a, b, c = (np.log(p[keep + o] + 1e-12) for o in (-1, 0, 1))
    den = a - 2 * b + c
    off = np.divide(0.5 * (a - c), den, out=np.zeros_like(den), where=np.abs(den) > 1e-12)  # parabolic interpolation
    semitones = 12 * np.log2((keep + off) * sr / n / 440.0)
    weight = np.sqrt(p[keep])
    z = np.sum(weight * np.exp(2j * np.pi * semitones)) / (np.sum(weight) + 1e-12)
    cents = float(np.angle(z) / (2 * np.pi) * 100)
    return cents if abs(z) >= TUNING_MIN_FOCUS and abs(cents) >= TUNING_MIN_CENTS else 0.0


def cq_frames(x: np.ndarray, edges_s: np.ndarray, *, sr: int = SAMPLE_RATE, tuning_cents: float = 0.0,
              low_midi: int = LOW_MIDI, octaves: int = OCTAVES, width: float = WIDTH_SEMITONES) -> np.ndarray:  # fmt: skip
    """Constant-Q power, one bin per tuned semitone from `low_midi`, averaged over each span between consecutive
    `edges_s`: [spans, octaves * 12].

    Done in the frequency domain over the whole signal: each bin is a Hann window cut from the signal's FFT, inverse
    transformed at a rate of about its bandwidth, one batch per octave. A 4-minute track takes about 0.1 s."""
    edges_s = np.asarray(edges_s, np.float64)
    spans = max(0, edges_s.size - 1)
    out = np.zeros((spans, octaves * 12), np.float32)
    n = x.size
    if n == 0 or spans == 0:
        return out
    spectrum = sfft.rfft(np.asarray(x, np.float32))
    df = sr / n
    relative = 2 ** (width / 12) - 1
    base = 440.0 * 2 ** ((low_midi - 69 + tuning_cents / 100) / 12)
    for o in range(octaves):
        f = base * 2.0 ** (o + np.arange(12) / 12)
        bw = f * relative  # the window's full width
        if f[-1] + bw[-1] / 2 >= sr / 2:
            break
        m = int(sfft.next_fast_len(int(np.ceil(max(bw[-1] / df + 2, MIN_RATE / df))), real=False))
        batch = np.zeros((12, m), np.complex64)
        for k in range(12):
            half = max(1, int(np.ceil(bw[k] / 2 / df)))
            centre = int(round(f[k] / df))
            idx = np.arange(max(0, centre - half), min(spectrum.size, centre + half + 1))
            d = (idx * df - f[k]) / (bw[k] / 2)
            window = np.where(np.abs(d) <= 1, 0.5 + 0.5 * np.cos(np.pi * d), 0.0)
            batch[k, (idx - centre) % m] = spectrum[idx] * window
        power = np.abs(sfft.ifft(batch, axis=1)) ** 2  # the band's envelope, at m * df samples per second
        i = np.clip(np.round(edges_s * m * df).astype(np.int64), 0, m)
        cum = np.concatenate([np.zeros((12, 1)), np.cumsum(power, axis=1, dtype=np.float64)], axis=1)
        out[:, o * 12:(o + 1) * 12] = ((cum[:, i[1:]] - cum[:, i[:-1]]) / np.maximum(i[1:] - i[:-1], 1)).T / m
    return out


def chroma_of(frames: np.ndarray, low_midi: int = LOW_MIDI) -> np.ndarray:
    """[spans, 12] by pitch class (0 = C) from `cq_frames`: log-compressed, each span scaled to a peak of 1."""
    if frames.size == 0:
        return np.zeros((frames.shape[0], 12), np.float32)
    s = np.log1p(LOG_GAIN * frames / (float(frames.max()) + 1e-20))
    c = np.zeros((s.shape[0], 12), np.float64)
    for i in range(s.shape[1]):
        c[:, (low_midi + i) % 12] += s[:, i]
    return c / np.maximum(c.max(axis=1, keepdims=True), 1e-12)


# --- matching ------------------------------------------------------------------------------------------------------


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, np.float64)
    v = v - v.mean(axis=-1, keepdims=True)
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)


def templates(profile: str = PROFILE) -> np.ndarray:
    """[24, 12]: the zero-mean, unit-length profile of each key."""
    major, minor = PROFILES[profile]
    return _unit(np.array([np.roll(major, t) for t in range(12)] + [np.roll(minor, t) for t in range(12)]))


def correlations(chroma: np.ndarray, profile: str = PROFILE) -> np.ndarray:
    """Each key's Pearson correlation with a 12-vector, or a [n, 12] array's rows."""
    return _unit(chroma) @ templates(profile).T


def confidence_from(scores: np.ndarray) -> tuple[int, float]:
    """(best key, confidence) from 24 scores (correlation plus prior)."""
    best = int(np.argmax(scores))
    second = float(np.partition(scores, -2)[-2])
    z = CONFIDENCE_A + CONFIDENCE_B * float(scores[best]) + CONFIDENCE_C * (float(scores[best]) - second)
    return best, 1.0 / (1.0 + math.exp(-z))


def with_prior(r: np.ndarray) -> np.ndarray:
    out = np.array(r, np.float64)
    out[..., 12:] += MINOR_PRIOR
    return out


def viterbi(scores: np.ndarray, cost: float = CHANGE_COST) -> np.ndarray:
    """The key of each block ([blocks, 24] scores) maximising the summed score, with `cost` per change of key."""
    best, back = scores[0].copy(), np.zeros(scores.shape, int)
    for t in range(1, len(scores)):
        top = int(np.argmax(best))
        stay = best >= best[top] - cost
        back[t] = np.where(stay, np.arange(24), top)
        best = np.where(stay, best, best[top] - cost) + scores[t]
    path = np.zeros(len(scores), int)
    path[-1] = int(np.argmax(best))
    for t in range(len(scores) - 1, 0, -1):
        path[t - 1] = back[t, path[t]]
    return path


def key_changes(chroma: np.ndarray, edges_s: np.ndarray, global_key: int, *, beats_per_block: int = BLOCK_BEATS,
                profile: str = PROFILE) -> list[dict]:  # fmt: skip
    """The stretches in another key than `global_key`: at least MIN_CHANGE_BARS bars long, and fitting that key clearly
    better (MIN_CHANGE_GAIN) than the global one."""
    blocks = chroma.shape[0] // beats_per_block
    if blocks < 2:
        return []
    mean = chroma[:blocks * beats_per_block].reshape(blocks, beats_per_block, 12).mean(axis=1)
    audible = np.ptp(mean, axis=1) > 1e-6  # silent blocks don't vote; they get the key around them
    scores = with_prior(correlations(mean, profile))
    scores[~audible] = 0.0
    path = viterbi(scores)
    out: list[dict] = []
    start = 0
    for b in range(1, blocks + 1):
        if b < blocks and path[b] == path[start]:
            continue
        key, length = int(path[start]), (b - start) * beats_per_block
        if key != global_key and length >= MIN_CHANGE_BARS * 4:
            part = with_prior(correlations(chroma[start * beats_per_block:b * beats_per_block].mean(axis=0), profile))
            if part[key] - part[global_key] >= MIN_CHANGE_GAIN:
                out.append({"start_s": round(float(edges_s[start * beats_per_block]), 3),
                            "end_s": round(float(edges_s[min(b * beats_per_block, edges_s.size - 1)]), 3),
                            **notations(key), "confidence": round(confidence_from(part)[1], 3)})  # fmt: skip
        start = b
    return out


def analyse_signal(x: np.ndarray, beat_times_s: np.ndarray | None = None, *, source: str = "mix",
                   sr: int = SAMPLE_RATE, profile: str = PROFILE) -> dict:  # fmt: skip
    """The `key` analysis of a mono signal; `beat_times_s` are the beats to average over."""
    duration = x.size / sr
    tuning = estimate_tuning(x, sr)
    beats_given = beat_times_s is not None and len(beat_times_s) >= 8
    if beats_given:
        b = np.asarray(beat_times_s, np.float64)
        edges = np.unique(np.concatenate([[0.0], b[(b > 0) & (b < duration)], [duration]]))
    else:
        edges = np.append(np.arange(0.0, duration, FALLBACK_FRAME_S), duration)
    frames = cq_frames(x, edges, sr=sr, tuning_cents=tuning)
    chroma = chroma_of(frames)
    out: dict[str, Any] = {"version": VERSION, "key": None, "camelot": None, "open_key": None, "confidence": 0.0,
                           "best_guess": None, "source": source, "beat_synchronous": bool(beats_given),
                           "tuning_cents": round(tuning, 1), "profile": profile, "changes": [],
                           "duration_s": round(duration, 3)}  # fmt: skip
    if chroma.size == 0 or float(chroma.max()) <= 0:
        return out  # silence
    r = with_prior(correlations(chroma.mean(axis=0), profile))
    best, confidence = confidence_from(r)
    out["confidence"] = round(confidence, 3)
    out["best_guess"] = notations(best)
    if confidence >= MIN_CONFIDENCE:
        out.update(notations(best))
    out["changes"] = key_changes(chroma, edges, best, profile=profile)
    return out


# --- the job -------------------------------------------------------------------------------------------------------


def decode_mono(path: Path | str) -> np.ndarray:
    from seedbox2.analysis.waveform import decode_chunks

    parts = list(decode_chunks(path, 1 << 20, sample_rate=SAMPLE_RATE))
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def harmonic_signal(folder: Path) -> np.ndarray | None:
    """The bass and other stems in `folder`, added up."""
    files = [next(iter(sorted(folder.glob(f"{stem}.*"))), None) for stem in ("bass", "other")]
    if not all(files):
        return None
    bass, other = (decode_mono(f) for f in files)  # type: ignore[arg-type]
    n = min(bass.size, other.size)
    return bass[:n] + other[:n] if n else None


def load_signal(lib: Any, hash_: str) -> tuple[np.ndarray, str]:
    """(mono signal, "stems" or "mix"): the harmonic stems if the track has them."""
    from seedbox2.analysis import stems

    folder = stems.current(lib, hash_)
    if folder is not None:
        try:
            x = harmonic_signal(Path(folder))
        except Exception as e:  # stems evicted mid-read, or a bad file: the mix still gives a key
            log.warning("%s: can't read the stems (%s); using the mix", hash_, e)
            x = None
        if x is not None:
            return x, "stems"
    sources = [Path(p) for p in lib.paths(hash_) if Path(p).is_file()]
    if not sources:
        raise FileNotFoundError("none of the track's files is there any more")
    return decode_mono(sources[0]), "mix"


def beat_times_of(lib: Any, hash_: str) -> np.ndarray | None:
    from seedbox2.analysis import beats

    row = lib.analysis(hash_, beats.ANALYSER, beats.VERSION)
    return beats.beat_times(row["data"]) if row else None


@register("key", pool="light", per_track=True)
def key_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"hash": ...}`: analyse the track and store it as its `key` analysis. Runs on what's there; the listener
    below runs it again when better input (the beats, the stems) arrives."""
    hash_ = payload["hash"]
    lib = ctx.library
    if lib.track(hash_) is None:
        raise ValueError(f"no track {hash_}")
    x, source = load_signal(lib, hash_)
    ctx.progress(0.5)
    data = analyse_signal(x, beat_times_of(lib, hash_), source=source)
    lib.put_analysis(hash_, ANALYSER, VERSION, data)
    return {"hash": hash_, "key": data["camelot"], "confidence": data["confidence"], "source": source}


def submit(jobs: Any, hash_: str, *, priority: int | str = "library", front: bool = False) -> dict:
    return jobs.submit("key", {"hash": hash_}, priority=priority, front=front)


def on_job_finished(jobs: Any, lib: Any, row: dict) -> None:
    """The supervisor's listener: the beats or the stems just arrived, so a key made without them is redone."""
    payload = row.get("payload")
    if row.get("kind") not in ("beats", "stems") or row.get("state") != "done" or not isinstance(payload, dict):
        return
    hash_ = payload.get("hash")
    current = lib.analysis(hash_, ANALYSER, VERSION) if hash_ else None
    if current is None:  # its own job is still on the way, and will see them
        return
    data = current["data"]
    if (row["kind"] == "beats" and not data.get("beat_synchronous")) or (row["kind"] == "stems" and data.get("source") != "stems"):
        submit(jobs, hash_)


def queue_missing(jobs: Any, lib: Any) -> int:
    """Queue the key of tracks imported before there was key detection (or under an older version), in the
    background."""
    hashes = lib.hashes_without_analysis(ANALYSER, VERSION)
    for h in hashes:
        submit(jobs, h, priority="background")
    return len(hashes)


# --- the user's key, hints, and what the planner sees ---------------------------------------------------------------


def set_override(lib: Any, hash_: str, key: Any) -> dict:
    """Store the user's key for this track, in any notation ("8A", "Am", "1m"). ValueError if it isn't a key."""
    from seedbox2.library.view import to_camelot

    camelot = to_camelot(key)
    if camelot is None:
        raise ValueError(f"not a key: {key!r}")
    data = notations(int(index_of_camelot(camelot)))  # type: ignore[arg-type]
    lib.put_analysis(hash_, OVERRIDE, OVERRIDE_VERSION, data)
    return data


def clear_override(lib: Any, hash_: str) -> None:
    lib.delete_analysis(hash_, OVERRIDE)


def effective(lib: Any, hash_: str) -> dict:
    """What we say the track's key is: `{key, camelot, open_key, from}` with `from` "user" (the override), "analysis"
    or "unknown" (not analysed yet, or not confident: the rest are None). Tag hints never count."""
    row = lib.analysis(hash_, OVERRIDE)
    if row:
        return {**row["data"], "from": "user"}
    row = lib.analysis(hash_, ANALYSER, VERSION)
    if row and row["data"].get("camelot"):
        d = row["data"]
        return {"key": d["key"], "camelot": d["camelot"], "open_key": d["open_key"], "from": "analysis"}
    return {"key": None, "camelot": None, "open_key": None, "from": "unknown"}


def planning_key(lib: Any, hash_: str) -> str | None:
    """The Camelot code a set planner may rely on, None when the key is unknown (`musicreco.mixing.camelot_move`
    treats a missing code as no key move)."""
    return effective(lib, hash_)["camelot"]


def status(lib: Any, hash_: str) -> dict:
    """`GET /analysis/{hash}/key`: the analysis (None until there is one), the effective key and the tag hints that
    disagree with it."""
    from seedbox2.library.view import key_hints

    row = lib.analysis(hash_, ANALYSER, VERSION)
    track = lib.track(hash_) or {}
    eff = effective(lib, hash_)
    return {"analysis": row["data"] if row else None, "effective": eff,
            "hints": [h for h in key_hints(track.get("tags") or {}) if h["key"] != eff["camelot"]]}  # fmt: skip


