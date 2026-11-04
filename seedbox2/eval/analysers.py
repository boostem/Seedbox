"""The analysers under test, by name. Each turns an audio file into an `Estimate`; None fields are not scored.

`seedbox1` (Seedbox 1's `musicreco/structure.py`, run without the tunebat BPM hint) is the baseline every Seedbox 2
analysis ticket has to beat. `seedbox2` and `seedbox2-small` are Seedbox 2's beats (#43) with Beat This! final0 and
small0 (the models must be downloaded). A new analyser registers itself here with `@analyser("name")`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class Estimate:
    duration_s: float
    bpm: float | None = None
    key: str | None = None
    beats: list[float] | None = None
    downbeats: list[float] | None = None
    sections: list[tuple[float, str]] | None = None  # (start s, label), as in Reference
    vocals: list[tuple[float, float]] | None = None
    cues: list[tuple[float, str]] | None = None  # for the annotation export only


ANALYSERS: dict[str, Callable[[Path], Estimate]] = {}


def analyser(name: str) -> Callable[[Callable[[Path], Estimate]], Callable[[Path], Estimate]]:
    def register(fn: Callable[[Path], Estimate]) -> Callable[[Path], Estimate]:
        ANALYSERS[name] = fn
        return fn
    return register


def grid(first_downbeat_s: float, bpm: float, duration_s: float, beats_per_bar: int = 4) -> tuple[list[float],
                                                                                                    list[float]]:
    """Beats and downbeats of a constant-tempo grid through `first_downbeat_s`, over [0, duration_s]."""
    period = 60.0 / bpm
    k0 = -int(np.floor(first_downbeat_s / period))  # earliest beat index at or after 0 s
    k = np.arange(k0, int(np.floor((duration_s - first_downbeat_s) / period)) + 1)
    t = first_downbeat_s + k * period
    keep = (t >= 0) & (t <= duration_s)
    t, k = np.round(t[keep], 4), k[keep]
    return t.tolist(), t[k % beats_per_bar == 0].tolist()


def spans(on: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> list[tuple[float, float]]:
    """Merged (start, end) spans of the consecutive intervals where `on` is true."""
    out: list[tuple[float, float]] = []
    for flag, a, b in zip(on, starts, ends):
        if not flag:
            continue
        if out and abs(out[-1][1] - a) < 1e-6:
            out[-1] = (out[-1][0], float(b))
        else:
            out.append((float(a), float(b)))
    return out


@analyser("seedbox1")
def seedbox1(path: Path) -> Estimate:
    from musicreco import structure  # noqa: PLC0415 (only workers need numpy/scipy analysis code loaded)

    a = structure.analyze(path)
    duration, first, bar_s = a["duration_s"], a["first_downbeat_s"], a["bar_s"]
    beats, downbeats = grid(first, a["bpm"], duration)
    bar_starts = first + bar_s * np.arange(a["bar_count"])
    voc = np.asarray(a["vocal_per_bar"]) >= 0.5  # the threshold structure.py itself uses for has_vocals
    vocals = spans(voc, np.clip(bar_starts, 0, duration), np.clip(bar_starts + bar_s, 0, duration))
    sections = [(max(0.0, s["start_s"]), s["kind"]) for s in a["sections"]]
    if a["sections"]:
        sections.append((a["sections"][-1]["end_s"], "end"))
    return Estimate(duration_s=duration, bpm=a["bpm"], key=None, beats=beats, downbeats=downbeats,
                    sections=sections, vocals=vocals, cues=[(c["time_s"], c["label"]) for c in a["cues"]])


def _beat_this(path: Path, model: str) -> Estimate:
    """Seedbox 2's beats analysis (#43) with the model from the models folder (`python -m seedbox2.models`
    or the app downloads it), without tag hints."""
    from seedbox2.analysis import beats as sb2  # noqa: PLC0415
    from seedbox2.models import load_session  # noqa: PLC0415

    session = load_session(model)
    r = sb2.analyse(path, session, model=model)
    return Estimate(duration_s=r["duration_s"], bpm=r["bpm"], beats=sb2.beat_times(r).round(4).tolist(),
                    downbeats=sb2.beat_times(r, sb2.BEATS_PER_BAR).round(4).tolist())


@analyser("seedbox2")
def seedbox2_beats(path: Path) -> Estimate:
    return _beat_this(path, "beat_this")


@analyser("seedbox2-small")
def seedbox2_beats_small(path: Path) -> Estimate:
    return _beat_this(path, "beat_this_small")
