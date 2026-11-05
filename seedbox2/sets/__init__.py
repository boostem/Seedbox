"""Set ordering on Seedbox 2's own analysis (#60): `arcs` (the energy shape), `features` (what the analysis says about
each track), `cost` (what a running order costs), `solver` (finds a cheap one, with pinned tracks), `explain` and
`service` (one call from a set in the library to an explained order). `bridge` finds a track for a gap in the set (#64), and `longset` builds
long sets across crates (#62)."""

from seedbox2.sets.arcs import TEMPLATES, Arc, arc_from
from seedbox2.sets.cost import Problem, TempoJourney, Weights
from seedbox2.sets.features import TrackFeatures, load_features
from seedbox2.sets.service import (
           OrderRequest,
           evaluate_set,
           evaluate_tracks,
           order_set,
           order_tracks,
)
from seedbox2.sets.solver import Solution, solve

__all__ = [
           "TEMPLATES",
           "Arc",
           "OrderRequest",
           "Problem",
           "Solution",
           "TempoJourney",
           "TrackFeatures",
           "Weights",
           "arc_from",
           "evaluate_set",
           "evaluate_tracks",
           "load_features",
           "order_set",
           "order_tracks",
           "solve",
]  # fmt: skip
