"""Long sets (#62): a set built from several crates and sets, or from the whole library, in the order they're given; the
best links from the end of one crate to the start of another; and #64's search for a gap, before the set is saved.

Everything is scored by `cost.py`, so these say the same as the set they end up in. A library is far too large for
`Problem`'s n x n tables, so it's narrowed first with key, tempo and arc alone (`_Pool`, one row of numbers per track), and
only a shortlist has its embeddings read and goes through `Problem`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from seedbox2.sets import bridge
from seedbox2.sets.arcs import TEMPLATES, arc_from
from seedbox2.sets.cost import (
    TEMPO_PREFER_PCT,
    UNKNOWN_KEY,
    Problem,
    TempoJourney,
    Weights,
    _fold,
    auto_journey,
    group_bands,
    key_index,
    key_table,
    tempo_cost,
    tempo_distances,
)
from seedbox2.sets.explain import rating, transition
from seedbox2.sets.features import TrackFeatures, add_detail, load_features
from seedbox2.sets.service import OrderRequest, evaluate_tracks, order_tracks
from seedbox2.sets.solver import SolverError

MAX_TRACKS = 200  # what the solver takes in about 10 s (see the README)
LIBRARY_TRACKS = 60  # how long a set with the whole library in it gets without being told: about four hours
SHORTLIST = 30  # tracks per pick, or per gap, that get their embeddings read
EDGE = 5  # the last tracks of a crate that count as its end, and the first as its start
GOOD_LINK = 2.0  # a pair costing this or less mixes well: say a +1 move, a small tempo change, a related vibe
_FLAT = TEMPLATES["flat"]
_BROKEN = {"key_clash", "tempo_over_limit"}


class SourceError(LookupError):
    pass


@dataclass
class Source:
    kind: str  # "crate", "set" or "library"
    id: int | None
    name: str
    hashes: list[str]  # empty for the library until `build_long_set` fills it in

    def to_json(self) -> dict:
        return {"kind": self.kind, "id": self.id, "name": self.name}


def resolve_sources(lib: Any, specs: Sequence[dict]) -> list[Source]:
    """`[{kind, id}]` as sources. An imported playlist is a crate, so that's how a playlist comes."""
    out = []
    for spec in specs:
        kind, id_ = spec.get("kind"), spec.get("id")
        if kind == "library":
            out.append(Source("library", None, "Whole library", []))
            continue
        found = lib.crate(id_) if kind == "crate" else lib.set(id_) if kind == "set" else None
        hashes = lib.crate_hashes(id_) if kind == "crate" else lib.set_hashes(id_) if kind == "set" else None
        if found is None or hashes is None:
            raise SourceError(f"no such {kind}: {id_}")
        out.append(Source(kind, id_, found["name"], hashes))
    return out


class _Pool:
    """The numbers of many tracks, one row each: what can be said about them without reading their embeddings."""

    def __init__(self, tracks: Sequence[TrackFeatures], w: Weights, level_lo: float = 0.0, level_hi: float = 1.0) -> None:
        self.tracks = list(tracks)
        self.w = w
        self.table = key_table(w)
        self.keys = np.array([key_index(t.camelot) for t in self.tracks], np.int64)
        self.bpm = np.array([t.bpm or np.nan for t in self.tracks], np.float64)
        energy = np.array([np.nan if t.energy is None else t.energy for t in self.tracks], np.float64)
        self.has_energy = ~np.isnan(energy)
        span = level_hi - level_lo
        self.levels = np.nan_to_num((energy - level_lo) / span, nan=0.5) if span > 0 else np.full(energy.shape, 0.5)

    def _tempo(self, bpm: float | None) -> np.ndarray:
        """From a track at `bpm` to each of these."""
        known = ~np.isnan(self.bpm)
        if not bpm:
            return np.full(self.bpm.shape, self.w.tempo_unknown)
        mine = np.nan_to_num(self.bpm, nan=1.0)
        pct, factor = tempo_distances(np.full(mine.shape, float(bpm)), mine)
        cost = tempo_cost(pct, self.w) + self.w.half_time * (factor != 1.0)
        return np.where(known, cost, self.w.tempo_unknown)

    def steps_from(self, t: TrackFeatures) -> np.ndarray:
        return self.table[key_index(t.camelot), self.keys] + self._tempo(t.bpm)

    def position_costs(self, level: float, target: float | None) -> np.ndarray:
        """As `Problem.slot` has it, at a position whose arc level and journey target are these."""
        cost = self.w.energy_level * np.abs(self.levels - level) * self.has_energy
        if target:
            known = ~np.isnan(self.bpm)
            away = np.abs(_fold(np.nan_to_num(self.bpm, nan=1.0), target) / target - 1) * 100 / TEMPO_PREFER_PCT
            cost = cost + self.w.journey * away * known
        return cost


def walk(tracks: Sequence[TrackFeatures], count: int, levels: Sequence[float], targets: Sequence[float] | None, *,
         before: TrackFeatures | None, weights: Weights, detail: Callable[[TrackFeatures], None],
         energy_range: tuple[float, float]) -> list[TrackFeatures]:  # fmt: skip
    """`count` of these tracks, picked one at a time along the arc (`levels`) and the journey (`targets`), each the best to
    follow the last. Greedy: the solver puts them in order afterwards, so this only has to pick tracks that can go together.
    Analysed tracks only, unless there aren't enough."""
    analysed = [t for t in tracks if t.bpm and t.camelot]
    pool = _Pool(analysed if len(analysed) >= count else tracks, weights, *energy_range)
    used = np.zeros(len(pool.tracks), bool)
    out: list[TrackFeatures] = []
    last = before
    for step in range(count):
        places = pool.position_costs(levels[step], targets[step] if targets is not None else None)
        rough = places + (pool.steps_from(last) if last is not None else 0.0)
        rough[used] = np.inf
        if last is None:
            best = int(np.argmin(rough))
        else:
            short = np.argsort(rough, kind="stable")[:SHORTLIST]
            short = short[np.isfinite(rough[short])]
            detail(last)
            for i in short:
                detail(pool.tracks[i])
            p = Problem([last, *(pool.tracks[i] for i in short)], _FLAT, TempoJourney(1.0, 1.0), weights)
            best = int(short[np.argmin(p.step_matrix()[0, 1:] + places[short])])
        used[best] = True
        last = pool.tracks[best]
        out.append(last)
    return out


def quotas(sizes: Sequence[int], library: Sequence[bool], limit: int) -> list[int]:
    """How many tracks each source gets. Crates and sets were chosen track by track, so they stay whole if they fit and the
    library fills up what's left; if they don't fit, each gets its share."""
    sizes = list(sizes)
    if sum(sizes) <= limit:
        return sizes
    named = sum(s for s, lib in zip(sizes, library, strict=True) if not lib)
    if named <= limit:
        room = limit - named
        out = []
        for s, lib in zip(sizes, library, strict=True):
            take = min(s, room) if lib else s
            room -= take if lib else 0
            out.append(take)
        return out
    shares = [s * limit / named if not lib else 0.0 for s, lib in zip(sizes, library, strict=True)]
    out = [int(x) for x in shares]
    # largest remainder, the earlier source first
    for i in sorted(range(len(sizes)), key=lambda i: -(shares[i] - out[i]))[: limit - sum(out)]:
        out[i] += 1
    return out


def _detail_once(lib: Any) -> Callable[[TrackFeatures], None]:
    done: set[str] = set()

    def detail(t: TrackFeatures) -> None:
        if t.hash not in done:
            done.add(t.hash)
            add_detail(lib, t)

    return detail


def _pools(sources: Sequence[Source], features: dict[str, TrackFeatures]) -> list[list[TrackFeatures]]:
    """Each source's tracks that exist, once each across all of them: a track belongs to the first crate that has it, and
    the library is what no crate has (in hash order, so the same library always gives the same set)."""
    seen: set[str] = set()
    pools: list[list[TrackFeatures]] = []
    for s in sources:
        fresh = [h for h in dict.fromkeys(s.hashes) if h not in seen and h in features]
        seen.update(fresh)
        pools.append([features[h] for h in fresh])
    for s, pool in zip(sources, pools, strict=True):
        if s.kind == "library":
            fresh = [h for h in sorted(features) if h not in seen]
            seen.update(fresh)
            pool.extend(features[h] for h in fresh)
    return pools


def build_long_set(lib: Any, specs: Sequence[dict], request: OrderRequest | None = None, *, max_tracks: int | None = None,
                   keep_order: bool = True) -> dict:  # fmt: skip
    """The best order found for these sources, nothing saved: `order_tracks`' answer, each track with the `source` it came
    from, and `sources`, `left_out` and `moved` (tracks playing outside their source's share)."""
    request = request or OrderRequest()
    if not specs:
        raise SolverError("a long set needs a crate, a set or the library")
    sources = resolve_sources(lib, specs)
    has_library = any(s.kind == "library" for s in sources)
    features = load_features(lib, None if has_library else {h for s in sources for h in s.hashes}, detail=False)

    pools = _pools(sources, features)

    sizes = [len(p) for p in pools]
    named_tracks = sum(n for n, s in zip(sizes, sources, strict=True) if s.kind != "library")
    default = max(LIBRARY_TRACKS, named_tracks) if has_library else MAX_TRACKS
    limit = min(max_tracks or default, MAX_TRACKS)
    takes = quotas(sizes, [s.kind == "library" for s in sources], limit)
    total = sum(takes)

    arc = arc_from(request.arc)
    everything = [t for p in pools for t in p]
    known = [t.energy for t in everything if t.energy is not None]
    energy_range = (min(known), max(known)) if known else (0.0, 0.0)
    journey = request.journey() or auto_journey(everything, arc)
    levels, targets = arc.sample(total), journey.sample(total) if journey else None
    detail = _detail_once(lib)

    w = request.weights or Weights()
    tracks: list[TrackFeatures] = []
    groups: list[int] = []
    for g, (pool, take) in enumerate(zip(pools, takes, strict=True)):
        at = len(tracks)
        picked = pool if take >= len(pool) else walk(
            pool, take, levels[at:at + take], targets[at:at + take] if targets is not None else None,
            before=tracks[-1] if tracks else None, weights=w, detail=detail, energy_range=energy_range)  # fmt: skip
        tracks += picked
        groups += [g] * len(picked)
    for t in tracks:
        detail(t)

    result = order_tracks(tracks, request, groups if keep_order else None)
    first, last = group_bands(groups) if groups else (np.zeros(0), np.zeros(0))
    moved = 0
    for position, item in enumerate(result["order"]):
        i = item["from_position"]
        item["source"] = groups[i]
        moved += not first[i] <= position <= last[i]
    return {**result, "sources": [{**s.to_json(), "tracks": n, "picked": k}
                                  for s, n, k in zip(sources, sizes, takes, strict=True)],
            "left_out": sum(sizes) - total, "moved": moved}  # fmt: skip


def _tracks_of(lib: Any, hashes: Sequence[str]) -> list[TrackFeatures]:
    features = load_features(lib, set(hashes))
    missing = [h for h in hashes if h not in features]
    if missing:
        raise SourceError(f"no such track: {missing[0]}")
    return [features[h] for h in hashes]


def explain_hashes(lib: Any, hashes: Sequence[str], request: OrderRequest | None = None) -> dict:
    """These tracks in this order, explained: a built set after a track was put into it."""
    return evaluate_tracks(_tracks_of(lib, hashes), request)


# --- bridges --------------------------------------------------------------------------------------------------------


def _track_json(t: TrackFeatures) -> dict:
    return {"hash": t.hash, "title": t.title, "artist": t.artist, "bpm": t.bpm, "key": t.camelot, "energy": t.energy,
            "rating": rating(t.energy)}  # fmt: skip


def _fit(p: Problem, a: int, b: int, cost: float, told: dict) -> str:
    if told["gap"] or _BROKEN & set(told["flags"]):
        return "poor"
    if not p.tempo_known[a, b] or p.keys[a] == UNKNOWN_KEY or p.keys[b] == UNKNOWN_KEY:
        return "unknown"  # what a pair costs without its key or tempo says little
    return "good" if cost <= GOOD_LINK else "fair"


def link_costs(p: Problem) -> np.ndarray:
    """Every pair as a link between two crates: played as they are, and on no arc, so what counts is the step in energy."""
    both = p.has_energy[None, :] & p.has_energy[:, None]
    return p.step_matrix() + p.w.energy_step * np.abs(p.levels[None, :] - p.levels[:, None]) * both


def links_between(tracks: Sequence[TrackFeatures], edges: Sequence[tuple[Sequence[str], Sequence[str]]],
                  weights: Weights | None = None) -> list[dict]:  # fmt: skip
    """The best pair from each source's end to each other source's start, cheapest first. `tracks` has all of them and
    `edges` each source's `(first hashes, last hashes)`. A track in both isn't a link. Sources with nothing to link are left
    out."""
    p = Problem(tracks, _FLAT, TempoJourney(1.0, 1.0), weights)
    index = {t.hash: i for i, t in enumerate(tracks)}
    costs = link_costs(p)
    shifts = np.zeros(p.n, np.int64)
    out = []
    for s, (_, ends) in enumerate(edges):
        for d, (starts, _) in enumerate(edges):
            if s == d:
                continue
            a_ids, b_ids = [index[h] for h in ends], [index[h] for h in starts]
            pairs = [(float(costs[a, b]), a, b) for a in a_ids for b in b_ids if a != b]
            if not pairs:
                continue
            cost, a, b = min(pairs, key=lambda x: x[0])
            told = transition(p, [a, b], shifts, 0)
            out.append({"from_source": s, "to_source": d, "from": _track_json(tracks[a]), "to": _track_json(tracks[b]),
                        "cost": round(cost, 3), "text": told["text"], "flags": told["flags"],
                        "fit": _fit(p, a, b, cost, told)})  # fmt: skip
    out.sort(key=lambda x: x["cost"])
    return out


def bridges(lib: Any, specs: Sequence[dict], *, edge: int = EDGE) -> dict:
    """How the end of each crate or set mixes into the start of each other one. The library has no end or start, so it
    has no links. A crate's end is its last tracks as it stands."""
    sources = resolve_sources(lib, specs)
    named = [(s, g) for g, s in enumerate(sources) if s.kind != "library"]
    edges = [(s.hashes[:edge], s.hashes[-edge:]) for s, _ in named]
    features = load_features(lib, {h for pair in edges for side in pair for h in side})
    edges = [tuple([h for h in side if h in features] for side in pair) for pair in edges]
    links = links_between(list(features.values()), edges) if features else []
    for link in links:
        link["from_source"], link["to_source"] = named[link["from_source"]][1], named[link["to_source"]][1]
    return {"sources": [s.to_json() for s in sources], "links": links}


# --- gaps -----------------------------------------------------------------------------------------------------------


def find_bridges(lib: Any, index: Any, hashes: Sequence[str], position: int, request: OrderRequest | None = None,
                 limit: int = bridge.DEFAULT_LIMIT) -> dict:  # fmt: skip
    """#64's search of the library for a gap, for a set that isn't saved yet, so it has no id. Its own tracks are left out."""
    tracks = _tracks_of(lib, hashes)
    return bridge.search_library(lib, index, bridge.find_gap(tracks, request or OrderRequest(), position), hashes, limit)
