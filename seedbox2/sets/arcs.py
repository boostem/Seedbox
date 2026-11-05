"""Energy arcs: the shape a set's energy should follow, as a piecewise-linear curve from 0 (the opener) to 1 (the closer).

A level of 0 is the set's quietest track and 1 its most intense, so an arc works on a set of deep house as well as on one
of peak-time techno. Templates are data, and a drawn curve is the same thing with more points.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

MAX_POINTS = 512  # a drawn curve is sampled at most once per pixel of a wide window
RISING = 0.1  # how far the last third of an arc must sit above its first for a set to drift up in tempo


class ArcError(ValueError):
    pass


@dataclass(frozen=True)
class Arc:
    name: str
    points: tuple[tuple[float, float], ...]  # (time 0-1, level 0-1), times increasing
    label: str = ""
    description: str = ""

    def at(self, t: np.ndarray | float) -> np.ndarray:
        xs, ys = zip(*self.points, strict=True)
        return np.interp(t, xs, ys)

    def sample(self, n: int) -> np.ndarray:
        """The level at each of `n` positions: the first track at 0, the last at 1."""
        return self.at(np.linspace(0.0, 1.0, n)) if n > 1 else self.at(np.zeros(n))

    def direction(self) -> int:
        """+1 if the arc ends higher than it starts, -1 if lower, 0 if it comes back to where it was."""
        levels = self.at(np.linspace(0.0, 1.0, 90))
        change = float(levels[60:].mean() - levels[:30].mean())
        return 1 if change > RISING else -1 if change < -RISING else 0

    def to_json(self) -> dict:
        return {"name": self.name, "label": self.label, "description": self.description,
                "points": [[round(t, 4), round(v, 4)] for t, v in self.points]}  # fmt: skip


TEMPLATES: dict[str, Arc] = {a.name: a for a in (
    Arc("warm_up", ((0.0, 0.05), (0.5, 0.4), (0.85, 0.7), (1.0, 0.8)), "Warm-up",
        "Starts low and builds slowly, without reaching the top"),
    Arc("peak_time", ((0.0, 0.55), (0.25, 0.8), (0.7, 1.0), (0.9, 0.95), (1.0, 0.75)), "Peak time",
        "Starts up, peaks about two thirds in, and eases off a little at the end"),
    Arc("sunset", ((0.0, 0.8), (0.35, 0.6), (0.75, 0.3), (1.0, 0.1)), "Sunset",
        "Comes down from a high start to a quiet close"),
    Arc("rollercoaster", ((0.0, 0.4), (0.2, 0.9), (0.4, 0.35), (0.6, 0.95), (0.8, 0.4), (1.0, 0.9)), "Rollercoaster",
        "Waves of tension and release"),
    Arc("flat", ((0.0, 0.5), (1.0, 0.5)), "Flat", "Holds one level throughout"),
)}  # fmt: skip
DEFAULT_ARC = "peak_time"


def custom(points: Sequence[Sequence[float]]) -> Arc:
    """A drawn curve: `[[t, level], ...]`, both 0-1. Points may come in any order (a stroke can go back), and the last
    one at a time wins. The ends are held flat out to 0 and 1."""
    if not 2 <= len(points) <= MAX_POINTS:
        raise ArcError(f"a curve needs 2 to {MAX_POINTS} points")
    by_time: dict[float, float] = {}
    for p in points:
        if len(p) != 2:
            raise ArcError("a point is [time, level]")
        t, v = float(p[0]), float(p[1])
        if not (np.isfinite(t) and np.isfinite(v) and 0.0 <= t <= 1.0 and 0.0 <= v <= 1.0):
            raise ArcError("a point's time and level go from 0 to 1")
        by_time[t] = v
    if len(by_time) < 2:
        raise ArcError("a curve needs points at two different times")
    return Arc("custom", tuple(sorted(by_time.items())), "Custom", "A curve you drew")


def arc_from(spec: str | Sequence[Sequence[float]] | None) -> Arc:
    """A template's name, a list of points, or None for the default."""
    if spec is None:
        return TEMPLATES[DEFAULT_ARC]
    if isinstance(spec, str):
        arc = TEMPLATES.get(spec)
        if arc is None:
            raise ArcError(f"no arc called {spec!r}")
        return arc
    return custom(spec)
