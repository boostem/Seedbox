"""Beats and downbeats (#43): Beat This! on onnxruntime, a piecewise beatgrid, the tempo octave and the bar phase.

1. **Detections**: `beatthis.run` gives frame-wise beat and downbeat logits (50 frames/s) and their peaks. final0
   by default; small0 in low-power mode (seedbox2.power, or the `models.low_power` setting) or while final0 isn't downloaded.
2. **Grid**: `grid.fit` turns the beat peaks into segments (one when the track is quantised).
3. **Tempo octave**: the grid's level is beat_this's; x0.5, x1 and x2 are scored by a log-normal prior on the BPM
   (centre OCTAVE_CENTRE, OCTAVE_SIGMA octaves; no folding into a fixed range) plus the bar length the downbeat
   activations show at that level (4 or 3 beats a bar is right, 2 or 8 is a level off), with a small bonus for
   staying at beat_this's own level. Drum & bass that beat_this tracks at 87 with downbeats every 2 beats comes out
   at 174; halftime tracked at 70 comes out at 140. Tag BPMs (file tags, Rekordbox, iTunes, tunebat) only break a
   near-tie (HINT_MARGIN) between two levels.
4. **Bar phase**: the downbeat activation, summed over every beat of each of the 4 phases across the whole track;
   the best phase wins, so one missed downbeat can't shift the bars. 4/4 is assumed; `time_signature.detected` is 3
   when the activations repeat every 3 beats far more clearly than every 4 (a waltz), and the UI can flag it.

The result (`analyse`, stored as the track's `beats` analysis):

    {"version", "model", "segments": [{start_beat, start_s, bpm}], "bpm", "constant", "beats_per_bar": 4,
     "time_signature": {"assumed": 4, "detected": 3|4, "confidence"}, "first_downbeat_s", "first_beat_s",
     "last_beat_s", "coverage", "residual_ms", "octave", "bpm_hint", "downbeat_strength"}

Beat numbers: beat 0 is the first downbeat at most 3/4 of a beat before the first detected beat (as Seedbox 1's
bar 0), downbeats are the multiples of 4; `grid.Grid(segments)` maps beats to seconds and back.

Without either model, or when Beat This! finds fewer than 8 beats, the job falls back to Seedbox 1's analysis
(`musicreco.structure`): one segment, `"model": "seedbox1"`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import numpy as np

from seedbox2.analysis import beatthis
from seedbox2.analysis.grid import Grid, fit
from seedbox2.jobs import register

log = logging.getLogger(__name__)

ANALYSER = "beats"
VERSION = "1"
MODELS = ("beat_this", "beat_this_small")  # final0, small0
BEATS_PER_BAR = 4

OCTAVE_CENTRE = 130.0  # BPM (tuned on GiantSteps and GTZAN, keeping DnB at 174 and halftime at 140)
OCTAVE_SIGMA = 0.6  # octaves
BAR_WEIGHT = 0.5  # for a bar of 3 or 4 beats at that level (-BAR_WEIGHT for 1 or 16)
STAY_BONUS = 0.5  # for beat_this's own level
HINT_MARGIN = 0.35  # a tag BPM decides between levels whose scores are this close
BAR_FRACTION = 0.7  # bar length: the shortest period whose phase contrast is >= 70 % of the best one's


class NoBeats(ValueError):
    """Too few beats detected to lay a grid (silence, speech, beatless ambient)."""


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x.astype(np.float64), -50, 50)))


def _at(p: np.ndarray, times: np.ndarray, fps: float, w: int = 2) -> np.ndarray:
    """The activation's maximum within ±w frames of each time."""
    if times.size == 0:
        return np.zeros(0)
    i = np.clip(np.round(times * fps).astype(int), 0, p.size - 1)
    return np.max([p[np.clip(i + o, 0, p.size - 1)] for o in range(-w, w + 1)], axis=0)


def acf_period(beat_logits: np.ndarray, fps: float = beatthis.FPS, lo: float = 50.0, hi: float = 230.0) -> float:
    """The beat activation's strongest autocorrelation period (s) between `lo` and `hi` BPM."""
    x = _sigmoid(beat_logits)
    x = x - x.mean()
    n = 1 << int(np.ceil(np.log2(max(2, 2 * x.size))))
    f = np.fft.rfft(x, n)
    ac = np.fft.irfft(f * np.conj(f), n)[: x.size]
    ac = ac / (ac[0] + 1e-12)
    lags = np.arange(int(fps * 60 / hi), min(int(fps * 60 / lo) + 1, x.size - 1))
    if lags.size < 3:
        return 0.0
    sc = ac[lags]
    i = int(np.argmax(sc))
    off = 0.0
    if 0 < i < sc.size - 1:
        a, b, c = sc[i - 1], sc[i], sc[i + 1]
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den else 0.0
    return float((lags[i] + off) / fps)


def phase_contrast(values: np.ndarray, period: int) -> float:
    """max - min of the mean value per phase (beat index mod `period`)."""
    if values.size < 2 * period:
        return 0.0
    m = [values[p::period].mean() for p in range(period)]
    return float(max(m) - min(m))


def bar_length(down_at_beats: np.ndarray) -> int | None:
    """Beats per bar shown by the downbeat activation at consecutive grid beats (2, 3, 4, 6 or 8), None if flat."""
    c = {b: phase_contrast(down_at_beats, b) for b in (2, 3, 4, 6, 8)}
    best = max(c.values())
    if best < 0.1:
        return None
    return next(b for b in c if c[b] >= BAR_FRACTION * best)


def _prior(bpm: float) -> float:
    return -0.5 * (np.log2(bpm / OCTAVE_CENTRE) / OCTAVE_SIGMA) ** 2


def octave_scores(bpm: float, bar: int | None) -> dict[float, float]:
    """Score of each level (x0.5, x1, x2) of a grid at `bpm` whose downbeats come every `bar` beats."""
    out = {}
    for f in (0.5, 1.0, 2.0):
        s = _prior(bpm * f) + (STAY_BONUS if f == 1.0 else 0.0)
        if bar:
            nb = bar * f
            s += BAR_WEIGHT if nb in (3, 4) else (0.0 if nb in (2, 6, 8) else -BAR_WEIGHT)
        out[f] = float(s)
    return out


def choose_octave(bpm: float, bar: int | None, hints: Iterable[float] = ()) -> tuple[float, float | None]:
    """(factor, the hint that decided it or None)."""
    scores = octave_scores(bpm, bar)
    ranked = sorted(scores, key=lambda f: -scores[f])
    best, second = ranked[0], ranked[1]
    if scores[best] - scores[second] < HINT_MARGIN:
        for h in hints:
            near = [f for f in (best, second) if abs(bpm * f / h - 1) <= 0.04]
            if len(near) == 1:
                return near[0], float(h)
    return best, None


def bpm_hints(tags: Any) -> list[float]:
    """BPMs from a track's tag hints (`tracks.tags`: file tags, Rekordbox, iTunes, Seedbox 1/tunebat), in 30-300."""
    out: list[float] = []

    def walk(d: Any, depth: int) -> None:
        if not isinstance(d, dict) or depth > 2:
            return
        for k, v in d.items():
            if k in ("bpm", "tempo", "AverageBpm") and not isinstance(v, (dict, list)):
                try:
                    b = float(str(v).replace(",", "."))
                except ValueError:
                    continue
                if 30 <= b <= 300 and all(abs(b / o - 1) > 0.005 for o in out):
                    out.append(b)
            elif isinstance(v, dict):
                walk(v, depth + 1)

    walk(tags, 0)
    return out


def analyse_activations(act: beatthis.Activations, hints: Iterable[float] = (), model: str = "beat_this") -> dict:
    """The grid, tempo octave and bar phase from Beat This!'s output (see the module docstring)."""
    beats = np.asarray(act.beats, dtype=np.float64)
    f = fit(beats, [acf_period(act.beat, act.fps)]) if beats.size >= 8 else None
    if f is None:
        raise NoBeats(f"only {beats.size} beats detected")
    grid = f.grid
    first, last = float(beats[0]), float(beats[-1])
    pb, pd = _sigmoid(act.beat), _sigmoid(act.downbeat)
    k = np.arange(np.ceil(grid.beat_at(first) - 0.5), np.floor(grid.beat_at(last) + 0.5) + 1)
    db1 = _at(pd, np.asarray(grid.time_at(k)), act.fps)
    bpm1 = grid.main_bpm(first, last)
    factor, hint = choose_octave(bpm1, bar_length(db1), list(hints))
    if factor == 2.0:
        grid = grid.scaled(2.0)
    elif factor == 0.5:  # keep the half of the beats that carries the downbeats (and the stronger beats)
        par = []
        for q in (0, 1):
            on = (k % 2) == q
            beat_q = _at(pb, np.asarray(grid.time_at(k[on])), act.fps)
            par.append(float(db1[on].mean() + 0.25 * beat_q.mean()) if on.any() else -1.0)
        grid = grid.scaled(0.5, offset=float(int(np.argmax(par))))
    # bar phase, voted over every beat of the detected span
    kk = np.arange(np.ceil(grid.beat_at(first) - 0.5), np.floor(grid.beat_at(last) + 0.5) + 1)
    dv = _at(pd, np.asarray(grid.time_at(kk)), act.fps)
    votes = [float(dv[(kk % BEATS_PER_BAR) == p].sum()) for p in range(BEATS_PER_BAR)]
    phase = int(np.argmax(votes))
    c3, c4 = phase_contrast(dv, 3), phase_contrast(dv, 4)
    detected = 3 if c3 > 1.5 * c4 and c3 >= 0.2 else 4
    # beat 0: the first downbeat at most 3/4 of a beat before the first detection
    b_first = float(grid.beat_at(first))
    zero = phase + BEATS_PER_BAR * np.ceil((b_first - 0.75 - phase) / BEATS_PER_BAR)
    grid = grid.shifted(zero)
    on = np.asarray(grid.time_at(np.round(grid.beat_at(beats))))
    hit = np.abs(on - beats) < 0.07
    return {
        "version": VERSION,
        "model": model,
        "segments": grid.to_json(),
        "bpm": round(grid.main_bpm(first, last), 3),
        "constant": grid.is_constant,
        "beats_per_bar": BEATS_PER_BAR,
        "time_signature": {"assumed": BEATS_PER_BAR, "detected": detected,
                           "confidence": round(abs(c4 - c3) / max(c3, c4, 1e-9), 3)},  # fmt: skip
        "first_downbeat_s": round(float(grid.time_at(0.0)), 4),
        "first_beat_s": round(first, 4),
        "last_beat_s": round(last, 4),
        "coverage": round(f.coverage, 3),
        "residual_ms": round(f.residual_ms, 1) if np.isfinite(f.residual_ms) else None,
        "octave": factor,
        "bpm_hint": hint,
        "downbeat_strength": round(max(votes) / max(1, int(np.sum((kk % BEATS_PER_BAR) == phase))), 3),
        "on_grid": round(float(np.mean(hit)), 3),
    }


def beat_times(result: dict, step: float = 1.0, pad_beats: float = 0.25) -> np.ndarray:
    """The grid's beats (step 1) or downbeats (step BEATS_PER_BAR) over the detected span."""
    g = Grid(result["segments"])
    spb0 = 60.0 / float(g.bpm_at(g.beat_at(result["first_beat_s"])))
    spb1 = 60.0 / float(g.bpm_at(g.beat_at(result["last_beat_s"])))
    return g.beats(result["first_beat_s"] - pad_beats * spb0, result["last_beat_s"] + pad_beats * spb1, step)


def decode(path: Path | str) -> np.ndarray:
    """Mono float32 at 22,050 Hz (the waveform job's ffmpeg decoder)."""
    from seedbox2.analysis.waveform import decode_chunks

    parts = list(decode_chunks(path, 1 << 20))
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def analyse(path: Path | str, session: Any, *, model: str = "beat_this", hints: Iterable[float] = (),
            progress: Callable[[float], None] | None = None) -> dict:  # fmt: skip
    x = decode(path)
    if x.size < beatthis.SAMPLE_RATE * 2:
        raise NoBeats("audio shorter than 2 s")
    act = beatthis.run(x, beatthis.session_runner(session), progress)
    out = analyse_activations(act, hints, model)
    out["duration_s"] = round(x.size / beatthis.SAMPLE_RATE, 3)
    return out


def seedbox1_fallback(path: Path | str, hints: Iterable[float] = ()) -> dict:
    """Seedbox 1's constant grid (no model downloaded), in the same shape."""
    from musicreco import structure

    hints = list(hints)
    a = structure.analyze(Path(path), bpm_hint=hints[0] if hints else None)
    fd, bpm = float(a["first_downbeat_s"]), float(a["bpm"])
    return {
        "version": VERSION, "model": "seedbox1", "segments": Grid.constant(fd, bpm).to_json(), "bpm": round(bpm, 3),
        "constant": True, "beats_per_bar": BEATS_PER_BAR,
        "time_signature": {"assumed": BEATS_PER_BAR, "detected": BEATS_PER_BAR, "confidence": 0.0},
        "first_downbeat_s": round(fd, 4), "first_beat_s": round(fd, 4),
        "last_beat_s": round(fd + a["bar_count"] * a["bar_s"], 4), "coverage": None, "residual_ms": None,
        "octave": 1.0, "bpm_hint": hints[0] if hints else None, "downbeat_strength": None, "on_grid": None,
        "duration_s": round(float(a["duration_s"]), 3),
    }  # fmt: skip


def model_order(low_power: bool) -> tuple[str, ...]:
    return tuple(reversed(MODELS)) if low_power else MODELS


@register("beats", pool="light", per_track=True)
def beats_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"hash": ...}`: analyse the track's first readable file and store it as its `beats` analysis."""
    from seedbox2.models.manager import read_settings

    hash_ = payload["hash"]
    lib = ctx.library
    track = lib.track(hash_)
    if track is None:
        raise ValueError(f"no track {hash_}")
    sources = [Path(p) for p in lib.paths(hash_) if Path(p).is_file()]
    if not sources:
        raise FileNotFoundError("none of the track's files is there any more")
    hints = bpm_hints(track.get("tags"))
    low_power = read_settings(ctx._settings())["low_power"] or getattr(ctx, "low_power", False)
    session, model = None, None
    for name in model_order(low_power):
        session = ctx.session_or_none(name)
        if session is not None:
            model = name
            break
    data = None
    if session is not None:
        try:
            data = analyse(sources[0], session, model=model or MODELS[0], hints=hints, progress=ctx.progress)
        except NoBeats as e:  # beatless (ambient, speech): Seedbox 1's grid is better than none
            log.info("%s: %s; using Seedbox 1's grid", hash_, e)
    if data is None:
        data = seedbox1_fallback(sources[0], hints)
    lib.put_analysis(hash_, ANALYSER, VERSION, data)
    return {"hash": hash_, "model": data["model"], "bpm": data["bpm"], "segments": len(data["segments"])}


def submit(jobs: Any, hash_: str, *, priority: int | str = "library", front: bool = False) -> dict:
    return jobs.submit("beats", {"hash": hash_}, priority=priority, front=front)
