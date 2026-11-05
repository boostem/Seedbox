"""From a set in the library to an explained order."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from seedbox2.sets.arcs import Arc, arc_from
from seedbox2.sets.cost import Problem, TempoJourney, Weights
from seedbox2.sets.explain import rating, transitions
from seedbox2.sets.features import TrackFeatures, load_features
from seedbox2.sets.solver import SolverError, check_locks, solve

SETTINGS_VERSION = 1


@dataclass
class OrderRequest:
    arc: str | Sequence[Sequence[float]] | None = None
    tempo_start: float | None = None
    tempo_end: float | None = None
    pins: list[dict] = field(default_factory=list)  # [{position, hash}]
    weights: Weights | None = None

    def journey(self) -> TempoJourney | None:
        if self.tempo_start is None and self.tempo_end is None:
            return None
        if self.tempo_start is None or self.tempo_end is None or self.tempo_start <= 0 or self.tempo_end <= 0:
            raise SolverError("a tempo journey needs a start and an end above 0 BPM")
        return TempoJourney(float(self.tempo_start), float(self.tempo_end))

    def settings(self) -> dict:
        """What's kept with the set, so it opens as it was left."""
        return {"version": SETTINGS_VERSION, "arc": self.arc, "tempo_start": self.tempo_start, "tempo_end": self.tempo_end,
                "pins": self.pins}  # fmt: skip


def request_from_settings(settings: dict | None) -> OrderRequest:
    s = settings if isinstance(settings, dict) else {}
    return OrderRequest(arc=s.get("arc"), tempo_start=s.get("tempo_start"), tempo_end=s.get("tempo_end"),
                        pins=list(s.get("pins") or []))  # fmt: skip


def _locks(hashes: Sequence[str], pins: Sequence[dict]) -> dict[int, int]:
    """`{position: item}`. A hash may come back in a set: each pin takes the first of its copies not yet taken, and a pin
    on a track that isn't in the set is an error."""
    taken: set[int] = set()
    out: dict[int, int] = {}
    for pin in pins:
        h, position = pin.get("hash"), pin.get("position")
        if not isinstance(position, int) or not 0 <= position < len(hashes):
            raise SolverError(f"no position {position} in this set")
        if position in out:
            raise SolverError(f"two tracks are pinned at position {position}")
        item = next((i for i, x in enumerate(hashes) if x == h and i not in taken), None)
        if item is None:
            raise SolverError(f"track {h} isn't in this set, or is pinned more often than it comes")
        taken.add(item)
        out[position] = item
    return out


def live_pins(hashes: Sequence[str], pins: Sequence[dict]) -> list[dict]:
    """The pins still true of this running order: the set may have changed since they were made."""
    return [pin for pin in pins if isinstance(pin.get("position"), int) and 0 <= pin["position"] < len(hashes)
            and hashes[pin["position"]] == pin.get("hash")]  # fmt: skip


def _result(p: Problem, hashes: Sequence[str], order: Sequence[int], shifts: np.ndarray, *, locks: dict[int, int],
            before: float | None) -> dict:  # fmt: skip
    pinned = set(locks)
    items = []
    span = p.level_hi - p.level_lo
    for position, i in enumerate(order):
        t = p.tracks[i]
        items.append({
            "position": position, "from_position": i, "hash": hashes[i], "title": t.title, "artist": t.artist,
            "bpm": t.bpm, "key": t.camelot, "energy": t.energy, "rating": rating(t.energy), "shift": int(shifts[i]),
            "pinned": position in pinned,
            "target_energy": round(p.level_lo + span * float(p.arc_levels[position]), 3),
            "target_bpm": round(float(p.targets[position]), 1) if p.journey else None,
        })  # fmt: skip
    cost = p.cost(order, shifts)
    return {
        "order": items, "transitions": transitions(p, order, shifts), "cost": round(cost, 3),
        "cost_before": round(before, 3) if before is not None else round(cost, 3),
        "arc": p.arc.to_json(), "tempo": p.journey.to_json() if p.journey else None,
        "unknown": {"bpm": sum(t.bpm is None for t in p.tracks), "key": sum(t.camelot is None for t in p.tracks),
                    "energy": sum(t.energy is None for t in p.tracks), "vibe": sum(not t.vibes for t in p.tracks),
                    "vocals": sum(t.vocals is None for t in p.tracks), "genre": sum(not t.genre for t in p.tracks)},
    }  # fmt: skip


def _problem(tracks: Sequence[TrackFeatures], request: OrderRequest) -> Problem:
    arc: Arc = arc_from(request.arc)
    return Problem(tracks, arc, request.journey(), request.weights)


def order_tracks(tracks: Sequence[TrackFeatures], request: OrderRequest | None = None) -> dict:
    """The best order found for these tracks, explained, with the order they came in as the one to beat."""
    request = request or OrderRequest()
    hashes = [t.hash for t in tracks]
    p = _problem(tracks, request)
    locks = check_locks(p.n, _locks(hashes, request.pins))
    solution = solve(p, locks)
    current = list(range(p.n))
    return _result(p, hashes, solution.order, solution.shifts, locks=locks, before=p.cost(current) if p.n else 0.0)


def evaluate_tracks(tracks: Sequence[TrackFeatures], request: OrderRequest | None = None) -> dict:
    """The tracks in the order they came, explained, without changing anything."""
    request = request or OrderRequest()
    p = _problem(tracks, request)
    current = list(range(p.n))
    hashes = [t.hash for t in tracks]
    locks = {pin["position"]: pin["position"] for pin in live_pins(hashes, request.pins)}
    return _result(p, hashes, current, p.best_shifts(current), locks=locks, before=None)


def _tracks_of(lib: Any, hashes: Sequence[str]) -> list[TrackFeatures]:
    features = load_features(lib, set(hashes))
    return [features[h] for h in hashes]


def order_set(lib: Any, set_id: int, request: OrderRequest | None = None) -> dict | None:
    """The set's best order, not saved. None if there is no such set."""
    hashes = lib.set_hashes(set_id)
    return None if hashes is None else order_tracks(_tracks_of(lib, hashes), request)


def evaluate_set(lib: Any, set_id: int, request: OrderRequest | None = None) -> dict | None:
    hashes = lib.set_hashes(set_id)
    return None if hashes is None else evaluate_tracks(_tracks_of(lib, hashes), request)
