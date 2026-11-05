"""A track for a gap in the set (#64): two neighbours that don't mix well, and what could go between them.

`transition` in `explain.py` gives every transition a score out of 100 and calls the ones under `GAP_SCORE` gaps. Here
`search_library` ranks the analysed tracks that aren't in the set, and `rank_online` does the same with tunebat's
recommendations of both neighbours, which `TunebatTry` fetches and can download.

A candidate costs what its two new transitions would cost on `cost.py`'s scale, so a bridge compares directly with the
gap, and only one with both transitions cheaper than the gap counts:
- **Tempo:** `tempo_cost` of each leg. It's quadratic, so a tempo halfway between the neighbours is the cheapest.
- **Key:** the Camelot move of each leg, from the keys the neighbours are played at. The bridge may be shifted a semitone,
  for `key_fix`.
- **Energy:** how far it goes outside the neighbours' range. Between them costs nothing.
- **Vibe:** CLAP distance of each leg (`_vibe_similarity`'s windows), relative to the spread among the candidates and the
  gap, since cosines aren't an absolute scale. Tunebat's tracks have no embeddings, so that term is the unknown cost.
- **Long intro:** a little off the top candidates, which gives the mix room. Sections need a read per track, so only
  those get one.

Like set ordering this reads our own analysis only: a library track without an analysed tempo and key isn't a candidate,
and the answer counts how many were left out. Tunebat's tempo and key are all there is for a track we don't have yet, so
the page shows them as hints. Its energy isn't on our scale and isn't used.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from seedbox2.analysis import energy as energy_analysis
from seedbox2.analysis import vibe
from seedbox2.library.tunebat_try import same_track
from seedbox2.sets.arcs import arc_from
from seedbox2.sets.cost import (
    _KEY_STEPS,
    _SHIFTED,
    SHIFTS,
    UNKNOWN_KEY,
    VIBE_MIN_SPREAD,
    Problem,
    Weights,
    _vibe_similarity,
    camelot_name,
    key_index,
    tempo_cost,
    tempo_distances,
)
from seedbox2.sets.explain import SAME_VIBE, VIBE_SHIFT, rating, score_of, transition
from seedbox2.sets.features import OURS, TrackFeatures, load_features
from seedbox2.sets.service import OrderRequest, request_from_settings

DEFAULT_LIMIT = 12
ENERGY_UNKNOWN = 0.2
LONG_INTRO_S = 30.0  # about 16 bars of house, long enough to mix over
INTRO_BONUS = 0.5  # what a long intro takes off a candidate's cost
INTRO_POOL = 40  # how many of the best candidates get their sections read
BOTH_BONUS = 0.5  # recommended from both neighbours
NO_KEY_BRIDGE = 3.0  # a leg this dear is a clash: no key bridges the two


class BridgeError(ValueError):
    """The request doesn't fit the set: no such transition, or the set has changed."""


@dataclass
class Gap:
    """A transition, with the keys its two tracks are played at."""

    a: TrackFeatures
    b: TrackFeatures
    key_a: int
    key_b: int
    transition: dict

    @property
    def cost(self) -> float:
        return self.transition["mix_cost"]


def find_gap(tracks: Sequence[TrackFeatures], request: OrderRequest, position: int) -> Gap:
    """The transition from `position` to the next, as the set stands."""
    if not 0 <= position < len(tracks) - 1:
        raise BridgeError(f"no transition after position {position} in this set")
    p = Problem(tracks, arc_from(request.arc), request.journey(), request.weights)
    order = list(range(p.n))
    shifts = p.best_shifts(order)
    a, b = position, position + 1
    return Gap(tracks[a], tracks[b], int(_SHIFTED[p.keys[a], SHIFTS.index(int(shifts[a]))]),
               int(_SHIFTED[p.keys[b], SHIFTS.index(int(shifts[b]))]), transition(p, order, shifts, position))  # fmt: skip


# --- what to look for --------------------------------------------------------------------------------------------------


def _key_table(w: Weights) -> np.ndarray:
    table = np.full((UNKNOWN_KEY + 1, UNKNOWN_KEY + 1), w.key_unknown)
    table[:24, :24] = w.key * _KEY_STEPS
    return table


def bridge_keys(key_a: int, key_b: int) -> list[str]:
    """The keys that make the worst of the two moves as gentle as it gets, gentlest overall first. None if each of them
    leaves a clash, or a neighbour's key is unknown."""
    if UNKNOWN_KEY in (key_a, key_b):
        return []
    steps = _KEY_STEPS
    worst = np.maximum(steps[key_a, :], steps[:, key_b])
    if worst.min() >= NO_KEY_BRIDGE:
        return []
    picks = np.flatnonzero(worst == worst.min())
    return [camelot_name(int(k)) for k in sorted(picks, key=lambda k: (steps[key_a, k] + steps[k, key_b], k))]  # type: ignore[misc]


def _middle_bpm(a: float | None, b: float | None) -> float | None:
    """Halfway between the two, geometrically, with `b` at the half or double time closest to `a`."""
    if not a or not b:
        return a or b or None
    _, factor = tempo_distances(np.array(float(a)), np.array(float(b)))
    return float(np.sqrt(a * b * float(factor)))


def wanted(gap: Gap) -> dict:
    """What would fit, for the page: `{bpm, keys, energy, text}`, as "a 124 BPM 8A/9A track with a long intro and a
    similar vibe"."""
    bpm = _middle_bpm(gap.a.bpm, gap.b.bpm)
    keys = bridge_keys(gap.key_a, gap.key_b)
    energies = [e for e in (gap.a.energy, gap.b.energy) if e is not None]
    what = " ".join(x for x in (f"{bpm:.0f} BPM" if bpm else "", "/".join(keys)) if x)
    vibe_known = bool(gap.a.vibes and gap.b.vibes)
    return {
        "bpm": round(bpm, 1) if bpm else None, "keys": keys,
        "energy": [min(energies), max(energies)] if energies else None,
        "text": f"a {what + ' ' if what else ''}track with a long intro" + (" and a similar vibe" if vibe_known else ""),
    }  # fmt: skip


# --- ranking -------------------------------------------------------------------------------------------------------------


def _tempo_leg(x: np.ndarray, y: np.ndarray, known: bool, w: Weights) -> np.ndarray:
    if not known:
        return np.full(np.broadcast(x, y).shape, w.tempo_unknown)
    pct, factor = tempo_distances(x, y)
    return tempo_cost(pct, w) + w.half_time * (factor != 1.0)


def _vibe_legs(gap: Gap, sim_in: np.ndarray, sim_out: np.ndarray, w: Weights) -> tuple[np.ndarray, np.ndarray]:
    """Each leg's vibe term, on the spread of what's known among the candidates and the gap itself."""
    dist_in, dist_out = 1.0 - sim_in, 1.0 - sim_out
    known = np.concatenate([dist_in[~np.isnan(dist_in)], dist_out[~np.isnan(dist_out)]])
    if known.size == 0:
        return np.full(sim_in.shape, w.vibe_unknown), np.full(sim_out.shape, w.vibe_unknown)
    own = _vibe_similarity(gap.a, gap.b)
    if own is not None:
        known = np.append(known, 1.0 - own)
    low, high = np.percentile(known, [10, 90])
    span = max(high - low, VIBE_MIN_SPREAD)

    def term(d: np.ndarray) -> np.ndarray:
        return np.where(np.isnan(d), w.vibe_unknown, w.vibe * np.clip(np.nan_to_num(d, nan=low) - low, 0.0, span) / span)

    return term(dist_in), term(dist_out)


@dataclass
class Ranked:
    """Per candidate: its cost, each leg's cost, the shift it would be played at, and its vibe term."""

    cost: np.ndarray
    leg_in: np.ndarray
    leg_out: np.ndarray
    shift: np.ndarray
    vibe: np.ndarray


def rank(gap: Gap, bpm: np.ndarray, keys: np.ndarray, energy: np.ndarray, sim_in: np.ndarray, sim_out: np.ndarray,
         w: Weights | None = None) -> Ranked:  # fmt: skip
    """Scores candidates: `bpm` and `keys` (`key_index`) are known, `energy` and the similarities may be NaN."""
    w = w or Weights()
    table = _key_table(w)
    options = _SHIFTED[keys]  # candidate x shift
    fixes = np.where(np.array(SHIFTS) != 0, w.key_fix, 0.0)
    key_in, key_out = table[gap.key_a, options], table[options, gap.key_b]
    pick = np.argmin(key_in + key_out + fixes, axis=1)
    rows = np.arange(keys.size)
    fix = fixes[pick] / 2  # a shift serves both legs, so each carries half
    key_in, key_out = key_in[rows, pick] + fix, key_out[rows, pick] + fix

    tempo_in = _tempo_leg(np.full(bpm.shape, gap.a.bpm or 1.0), bpm, gap.a.bpm is not None, w)
    tempo_out = _tempo_leg(bpm, np.full(bpm.shape, gap.b.bpm or 1.0), gap.b.bpm is not None, w)
    vibe_in, vibe_out = _vibe_legs(gap, sim_in, sim_out, w)

    if gap.a.energy is None or gap.b.energy is None:
        detour = np.full(energy.shape, ENERGY_UNKNOWN)
    else:  # 0 between the two, twice the overshoot outside them
        detour = w.energy_level * (np.abs(energy - gap.a.energy) + np.abs(energy - gap.b.energy) - abs(gap.a.energy - gap.b.energy))
        detour = np.where(np.isnan(energy), ENERGY_UNKNOWN, detour)

    leg_in, leg_out = key_in + tempo_in + vibe_in, key_out + tempo_out + vibe_out
    return Ranked(leg_in + leg_out + detour, leg_in, leg_out, np.array(SHIFTS)[pick], (vibe_in + vibe_out) / 2)


def _key_with_shift(key: int, shift: int) -> str:
    name = camelot_name(int(_SHIFTED[key, SHIFTS.index(shift)]))
    return f"{name} ({camelot_name(key)} {shift:+d} st)" if shift else str(name)


def _why(gap: Gap, bpm: float, key: int, shift: int, vibe_term: float | None, intro_s: float | None) -> str:
    """"124 BPM, 8A → 9A → 10A, same vibe, long intro"."""
    left, right = camelot_name(gap.key_a) or "?", camelot_name(gap.key_b) or "?"
    words = [f"{bpm:.0f} BPM", f"{left} → {_key_with_shift(key, shift)} → {right}"]
    if vibe_term is not None:
        words.append("same vibe" if vibe_term <= SAME_VIBE else "vibe shift" if vibe_term >= VIBE_SHIFT else "related vibe")
    if intro_s is not None and intro_s >= LONG_INTRO_S:
        words.append("long intro")
    return ", ".join(words)


def _gap_json(gap: Gap) -> dict:
    t = gap.transition
    return {"from": t["from"], "to": t["to"], "text": t["text"], "score": t["score"], "cost": t["mix_cost"]}


# --- the library -------------------------------------------------------------------------------------------------------


def leg_similarities(index: vibe.Index, gap: Gap) -> tuple[np.ndarray, np.ndarray]:
    """For each track of the index, the similarity from `a` to it and from it to `b`: end to start and main to main, as
    `cost._vibe_similarity`. NaN where they share no window."""
    names = np.array(index.names) if index.names else np.zeros(0, str)
    n = len(index.hashes)

    def leg(pairs: Iterable[tuple[np.ndarray | None, str]]) -> np.ndarray:
        total, count = np.zeros(n, np.float64), np.zeros(n, np.int64)
        for v, window in pairs:
            if v is None:
                continue
            mask = names == window
            total[index.owner[mask]] += index.vectors[mask] @ v
            count[index.owner[mask]] += 1
        return np.where(count > 0, total / np.maximum(count, 1), np.nan)

    into = leg([(gap.a.vibes.get("outro"), "intro"), (gap.a.vibes.get("main"), "main")])
    out_of = leg([(gap.b.vibes.get("intro"), "outro"), (gap.b.vibes.get("main"), "main")])
    return into, out_of


def _intro_seconds(lib: Any, hash_: str) -> float | None:
    row = lib.analysis(hash_, energy_analysis.ANALYSER, energy_analysis.VERSION)
    for span in vibe.spans_of(lib, hash_, row["data"] if row else None):
        if span.get("kind") == "intro":
            return float(span["end_s"]) - float(span["start_s"])
    return None if row is None else 0.0


def search_library(lib: Any, index: vibe.Index, gap: Gap, exclude: Iterable[str], limit: int = DEFAULT_LIMIT,
                   weights: Weights | None = None) -> dict:  # fmt: skip
    """`{gap, want, results, candidates, unanalysed}`: the analysed tracks outside the set that do better than the gap,
    best first."""
    skip = set(exclude)
    rows = [r for r in lib.view_rows() if r["hash"] not in skip]
    usable = [r for r in rows if r.get("bpm") and r.get("bpm_from") == "analysis" and r.get("key_from") in OURS
              and key_index(r.get("key")) != UNKNOWN_KEY]  # fmt: skip
    out = {"gap": _gap_json(gap), "want": wanted(gap), "results": [], "candidates": len(usable),
           "unanalysed": len(rows) - len(usable)}  # fmt: skip
    if not usable:
        return out

    index.refresh(lib)
    into, out_of = leg_similarities(index, gap)
    at = {h: i for i, h in enumerate(index.hashes)}
    where = [at.get(r["hash"]) for r in usable]
    sim_in = np.array([np.nan if i is None else into[i] for i in where], np.float64)
    sim_out = np.array([np.nan if i is None else out_of[i] for i in where], np.float64)
    bpm = np.array([float(r["bpm"]) for r in usable], np.float64)
    keys = np.array([key_index(r["key"]) for r in usable], np.int64)
    energy = np.array([np.nan if r.get("energy") is None or r.get("energy_from") != "analysis" else float(r["energy"])
                       for r in usable], np.float64)  # fmt: skip
    ranked = rank(gap, bpm, keys, energy, sim_in, sim_out, weights)

    better = np.flatnonzero((ranked.leg_in < gap.cost) & (ranked.leg_out < gap.cost))
    best = sorted(better.tolist(), key=lambda i: (float(ranked.cost[i]), usable[i]["hash"]))[: max(INTRO_POOL, limit)]
    intros = {i: _intro_seconds(lib, usable[i]["hash"]) for i in best}

    def final(i: int) -> float:
        return float(ranked.cost[i]) - INTRO_BONUS * min((intros[i] or 0.0) / LONG_INTRO_S, 1.0)

    results = []
    for i in sorted(best, key=lambda i: (final(i), usable[i]["hash"]))[:limit]:
        r = usable[i]
        vibe_term = None if np.isnan(sim_in[i]) and np.isnan(sim_out[i]) else float(ranked.vibe[i])
        e = None if np.isnan(energy[i]) else float(energy[i])
        results.append({
            "hash": r["hash"], "title": r.get("title") or "", "artist": r.get("artist") or "", "bpm": float(bpm[i]),
            "key": r["key"], "energy": e, "rating": rating(e), "shift": int(ranked.shift[i]), "intro_s": intros[i],
            "cost": round(final(i), 3), "score_in": score_of(float(ranked.leg_in[i])),
            "score_out": score_of(float(ranked.leg_out[i])),
            "text": _why(gap, float(bpm[i]), int(keys[i]), int(ranked.shift[i]), vibe_term, intros[i]),
        })  # fmt: skip
    return {**out, "results": results}


# --- tunebat -------------------------------------------------------------------------------------------------------------


def rank_online(gap: Gap, tracks_from: Sequence[Any], tracks_to: Sequence[Any], library: Iterable[dict],
                limit: int = DEFAULT_LIMIT, weights: Weights | None = None) -> list[dict]:  # fmt: skip
    """Tunebat's recommendations of the two neighbours (`musicreco.models.Track`), best first: those not in the library
    yet that do better than the gap and have a tempo and key. A track recommended from both comes first, all else equal."""
    sources: dict[str, list[str]] = {}
    tracks: dict[str, Any] = {}
    for side, items in (("from", tracks_from), ("to", tracks_to)):
        for t in items:
            tracks.setdefault(t.id, t)
            sources.setdefault(t.id, []).append(side)
    owned = [(r.get("title") or "", r.get("artist") or "") for r in library]
    usable = [t for t in tracks.values() if t.bpm and key_index(t.camelot) != UNKNOWN_KEY
              and not any(same_track(t.name, t.artist_str, title, artist) for title, artist in owned)]  # fmt: skip
    if not usable:
        return []
    nothing = np.full(len(usable), np.nan)
    keys = np.array([key_index(t.camelot) for t in usable], np.int64)
    ranked = rank(gap, np.array([float(t.bpm) for t in usable]), keys, nothing, nothing, nothing, weights)

    def final(i: int) -> float:
        return float(ranked.cost[i]) - (BOTH_BONUS if len(set(sources[usable[i].id])) > 1 else 0.0)

    results = []
    for i in sorted(range(len(usable)), key=lambda i: (final(i), usable[i].id)):
        if ranked.leg_in[i] >= gap.cost or ranked.leg_out[i] >= gap.cost:
            continue
        t = usable[i]
        results.append({
            "id": t.id, "name": t.name, "artists": list(t.artists), "bpm": float(t.bpm), "camelot": t.camelot,
            "shift": int(ranked.shift[i]), "cover_url": t.cover_url, "from": sorted(set(sources[t.id])),
            "cost": round(final(i), 3), "score_in": score_of(float(ranked.leg_in[i])),
            "score_out": score_of(float(ranked.leg_out[i])),
            "text": _why(gap, float(t.bpm), int(keys[i]), int(ranked.shift[i]), None, None),
        })  # fmt: skip
    return results[:limit]


def search_online(lib: Any, tunebat: Any, gap: Gap, limit: int = DEFAULT_LIMIT) -> dict:
    """`{results, seeds}`, each seed `{hash, id}`, with no id if tunebat doesn't know that neighbour. `TunebatUnavailable`
    if it can't be reached."""
    seeds, lists = [], []
    for t in (gap.a, gap.b):
        known = _tunebat_id(lib, t.hash)
        seed_id, items = tunebat.recommendations(t.title, t.artist, known)
        seeds.append({"hash": t.hash, "id": seed_id})
        lists.append(items)
    return {"gap": _gap_json(gap), "seeds": seeds, "results": rank_online(gap, lists[0], lists[1], lib.view_rows(), limit)}


def _tunebat_id(lib: Any, hash_: str) -> str | None:
    """The tunebat id Seedbox 1's import kept with the track, if any."""
    track = lib.track(hash_)
    tags = track.get("tags") if track else None
    data = tags.get("tunebat") if isinstance(tags, dict) else None
    found = data.get("id") if isinstance(data, dict) else None
    return found if isinstance(found, str) and found else None


# --- one call from a set in the library ----------------------------------------------------------------------------------


def set_gap(lib: Any, set_id: int, position: int) -> tuple[Gap, list[str]] | None:
    """The gap after `position` in the set, and the set's tracks. None if there is no such set."""
    hashes = lib.set_hashes(set_id)
    if hashes is None:
        return None
    features = load_features(lib, set(hashes))
    return find_gap([features[h] for h in hashes], request_from_settings(lib.set_order_settings(set_id)), position), hashes


def insert_bridge(lib: Any, set_id: int, position: int, hash_: str, from_hash: str, to_hash: str) -> OrderRequest | None:
    """Puts a track between positions `position` and `position + 1`, moving the pins after it with their tracks. The
    settings as saved. None if there is no such set; BridgeError if the transition isn't there any more."""
    hashes = lib.set_hashes(set_id)
    if hashes is None:
        return None
    if not 0 <= position < len(hashes) - 1 or hashes[position] != from_hash or hashes[position + 1] != to_hash:
        raise BridgeError("the set has changed: find a bridge again")
    if lib.track(hash_) is None:
        raise BridgeError(f"no track {hash_} in the library")
    request = request_from_settings(lib.set_order_settings(set_id))
    request.pins = [{**pin, "position": pin["position"] + 1} if isinstance(pin.get("position"), int) and pin["position"] > position
                    else pin for pin in request.pins]  # fmt: skip
    lib.put_set_order(set_id, [*hashes[: position + 1], hash_, *hashes[position + 1 :]], request.settings())
    return request
