"""Scores of an `Estimate` against a `Reference`, in points (0-100). A metric is left out when either side lacks it.

| Metric | What | How |
|---|---|---|
| `beat_f` | beat F-measure, ±70 ms | `mir_eval.beat.f_measure` (the first 5 s are ignored, as in MIREX) |
| `downbeat_f` | downbeat F-measure, ±70 ms | the same on downbeats |
| `tempo_acc1` | tempo within 4 % | MIREX/GiantSteps Accuracy1 |
| `tempo_acc2` | within 4 % of ×1, ×2, ×3, ×½ or ×⅓ | Accuracy2: also counts octave errors |
| `octave_err` | right tempo family, wrong octave (lower is better) | Accuracy2 and not Accuracy1 |
| `key_score` | MIREX weighted key score | `mir_eval.key.weighted_score`: 1 same, .5 fifth, .3 relative, .2 parallel |
| `boundary_f` | section boundaries hit within ±1 bar | `mir_eval.segment.detection` (track start/end trimmed) |
| `vocal_f` | vocal activity F-measure on 10 ms frames | `mir_eval.util.f_measure` |
"""

from __future__ import annotations

import warnings

import mir_eval
import numpy as np

from .analysers import Estimate
from .datasets import Reference

LOWER_IS_BETTER = {"octave_err"}
METRICS = ("beat_f", "downbeat_f", "tempo_acc1", "tempo_acc2", "octave_err", "key_score", "boundary_f", "vocal_f")
TEMPO_TOLERANCE = 0.04
TEMPO_FACTORS = (1.0, 2.0, 3.0, 1 / 2, 1 / 3)
VOCAL_HOP_S = 0.01


def _beat_f(ref: list[float], est: list[float]) -> float:
    return float(mir_eval.beat.f_measure(mir_eval.beat.trim_beats(np.asarray(ref, float)),
                                         mir_eval.beat.trim_beats(np.asarray(est, float))))


def tempo_scores(ref_bpm: float, est_bpm: float) -> dict[str, float]:
    def within(factor: float) -> bool:
        return abs(est_bpm - factor * ref_bpm) <= TEMPO_TOLERANCE * factor * ref_bpm
    acc1 = within(1.0)
    acc2 = any(within(f) for f in TEMPO_FACTORS)
    return {"tempo_acc1": float(acc1), "tempo_acc2": float(acc2), "octave_err": float(acc2 and not acc1)}


def _intervals(sections: list[tuple[float, str]], end: float) -> np.ndarray:
    starts = sorted({round(t, 4) for t, label in sections if label != "end" and 0 <= t < end})
    ends = [t for t, label in sections if label == "end"]
    stop = min(min(ends), end) if ends else end
    starts = [t for t in starts if t < stop]
    if not starts or starts[0] > 0:
        starts = [0.0] + starts
    bounds = np.array(starts + [stop])
    return np.column_stack([bounds[:-1], bounds[1:]])


def bar_seconds(ref: Reference, est: Estimate) -> float:
    if ref.downbeats and len(ref.downbeats) > 1:
        return float(np.median(np.diff(ref.downbeats)))
    if ref.beats and len(ref.beats) > 1:
        return 4 * float(np.median(np.diff(ref.beats)))
    return 240.0 / (ref.bpm or est.bpm or 120.0)


def boundary_f(ref: Reference, est: Estimate) -> float:
    end = max(ref.duration_s or 0.0, est.duration_s,
              max((t for t, _ in ref.sections or []), default=0.0) + 1e-3)
    r, e = _intervals(ref.sections or [], end), _intervals(est.sections or [], end)
    if len(r) < 3:  # nothing left after trimming the start and the end
        return float("nan")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _, _, f = mir_eval.segment.detection(r, e, window=bar_seconds(ref, est), trim=True)
    return float(f)


def _frames(spans: list[tuple[float, float]], n: int) -> np.ndarray:
    on = np.zeros(n, bool)
    for a, b in spans:
        on[max(0, int(round(a / VOCAL_HOP_S))):max(0, int(round(b / VOCAL_HOP_S)))] = True
    return on


def vocal_f(ref: list[tuple[float, float]], est: list[tuple[float, float]], duration_s: float) -> float:
    n = int(np.ceil(duration_s / VOCAL_HOP_S))
    r, e = _frames(ref, n), _frames(est, n)
    if not r.any() and not e.any():
        return 1.0  # an instrumental, correctly called one
    tp = float(np.sum(r & e))
    p = tp / max(1.0, float(e.sum()))
    rec = tp / max(1.0, float(r.sum()))
    return float(mir_eval.util.f_measure(p, rec))


def score(ref: Reference, est: Estimate) -> dict[str, float]:
    """Every metric both sides have, in points."""
    out: dict[str, float] = {}
    if ref.beats and est.beats is not None:
        out["beat_f"] = _beat_f(ref.beats, est.beats)
    if ref.downbeats and est.downbeats is not None:
        out["downbeat_f"] = _beat_f(ref.downbeats, est.downbeats)
    if ref.bpm and est.bpm:
        out.update(tempo_scores(ref.bpm, est.bpm))
    if ref.key and est.key:
        out["key_score"] = float(mir_eval.key.weighted_score(ref.key, est.key))
    if ref.sections and est.sections is not None:
        out["boundary_f"] = boundary_f(ref, est)
    if ref.vocals is not None and est.vocals is not None:
        out["vocal_f"] = vocal_f(ref.vocals, est.vocals, ref.duration_s or est.duration_s)
    return {k: round(100 * v, 2) for k, v in out.items() if np.isfinite(v)}
