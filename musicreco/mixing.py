"""Harmonic / tempo compatibility rule (pre-tick), compatibility scoring, energy ratings and set ordering."""

from __future__ import annotations

import math
import random
import re
from collections.abc import Iterator

from .models import Score, Track

_CAMELOT_RE = re.compile(r"\s*(\d{1,2})\s*([AB])\s*", re.IGNORECASE)
EPS = 1e-9  # float slack so exact boundaries (e.g. 117 vs 130 at 10%) count as inside


def _parse_camelot(code: object) -> tuple[int, str] | None:
    """'12a' -> (12, 'A'); None if not a valid Camelot code (1-12 + A/B)."""
    if not isinstance(code, str):
        return None
    m = _CAMELOT_RE.fullmatch(code)
    if not m:
        return None
    num = int(m.group(1))
    return (num, m.group(2).upper()) if 1 <= num <= 12 else None


def camelot_adjacent(seed: str, other: str) -> bool:
    """Same key, +/-1 on the wheel with the same letter (12<->1 wraps), or same number with the other letter."""
    a, b = _parse_camelot(seed), _parse_camelot(other)
    if a is None or b is None:
        return False
    (n1, l1), (n2, l2) = a, b
    if l1 == l2:
        return (n1 - n2) % 12 in (0, 1, 11)
    return n1 == n2


def _valid_bpm(bpm: object) -> bool:
    return isinstance(bpm, (int, float)) and math.isfinite(bpm) and bpm > 0


def _bpm_factor(seed_bpm: float, other_bpm: float, tol: float) -> float | None:
    """Multiplier (1, 2 or 0.5) that brings other_bpm within tol of seed_bpm, else None."""
    if not (_valid_bpm(seed_bpm) and _valid_bpm(other_bpm)):
        return None
    limit = seed_bpm * tol + EPS
    for factor in (1, 2, 0.5):
        if abs(other_bpm * factor - seed_bpm) <= limit:
            return factor
    return None


def passes_rule(seed: Track, other: Track, tol: float = 0.10) -> tuple[bool, str]:
    """(ok, reason): ok = key adjacent AND BPM within +/-tol (half/double ok); reason is a compact checklist string."""
    key_ok = camelot_adjacent(seed.camelot, other.camelot)
    parsed = _parse_camelot(other.camelot)
    key_label = f"{parsed[0]}{parsed[1]}" if parsed else (str(other.camelot or "").strip() or "?")

    factor = _bpm_factor(seed.bpm, other.bpm, tol)
    mark = "✓" if factor is not None else "✗"
    if not _valid_bpm(other.bpm):
        bpm_label = f"? {mark}"
    elif factor in (2, 0.5):
        note = "half-time" if factor == 2 else "double-time"
        bpm_label = f"{round(other.bpm)}→{round(other.bpm * factor)} {mark} ({note})"
    else:
        bpm_label = f"{round(other.bpm)} {mark}"

    reason = f"{key_label} {'✓' if key_ok else '✗'}  {bpm_label}"
    return key_ok and factor is not None, reason


# --- scoring -------------------------------------------------------------------------------------------------------

# Raw score 0-100 = key move (max 60) + BPM closeness (25) + energy (10) + danceability (5).
_KEY_POINTS = {
    "same": 60,
    "+1": 48,
    "-1": 48,
    "relative": 48,
    "+2 boost": 32,
    "diagonal": 30,
    "+7 boost": 28,
    "clash": 0,
    "?": 0,
}
_MOVE_CHIPS = {"same": "same key", "clash": "key clash", "?": "key ?"}
_BPM_POINTS = 25.0  # linear in |pitch %|: full at 0, none at the rule's tolerance (10% by default)
_HALF_DOUBLE_PENALTY = 3.0
_ENERGY_POINTS = 10.0
_DANCE_POINTS = 5.0
_CLOSE_SPAN = 0.30  # an energy/danceability gap this wide earns 0 points
_PASS_FLOOR = 48  # passing tracks are always >= this (key points alone) ...
_FAIL_SCALE = 0.6  # ... and failing ones are scaled so they stay below it (max raw 75 -> 45)


def camelot_move(seed: str, other: str) -> str:
    """Camelot relationship seed -> other: same, +1, -1, relative, +2 boost, +7 boost, diagonal, clash or '?'."""
    a, b = _parse_camelot(seed), _parse_camelot(other)
    if a is None or b is None:
        return "?"
    (n1, l1), (n2, l2) = a, b
    d = (n2 - n1) % 12
    if l1 == l2:
        return {0: "same", 1: "+1", 11: "-1", 2: "+2 boost", 7: "+7 boost"}.get(d, "clash")
    if d == 0:
        return "relative"
    return "diagonal" if d in (1, 11) else "clash"


def pitch_factor(seed_bpm: float, other_bpm: float) -> tuple[float, float] | None:
    """(signed pitch %, factor): the tempo change of other (x1, x2 or x0.5) vs the seed with the smallest gap."""
    if not (_valid_bpm(seed_bpm) and _valid_bpm(other_bpm)):
        return None
    factor = min((1, 2, 0.5), key=lambda f: abs(other_bpm * f - seed_bpm))
    return (other_bpm * factor - seed_bpm) / seed_bpm * 100, factor


def _known(x: object) -> bool:
    """A usable energy/danceability value (tunebat's missing values arrive as 0)."""
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def _closeness(gap: float, points: float) -> float:
    return points * max(0.0, 1 - abs(gap) / _CLOSE_SPAN)


def score(seed: Track, other: Track, tol: float = 0.10) -> Score:
    """Rank/colour value for `other` as a follow-up to `seed`; `passed` is exactly the pre-tick rule (BPM +/-tol).

    Within each group key move > BPM > energy > danceability. Passing tracks land in 48-100, failing ones in 0-45.
    """
    passed = passes_rule(seed, other, tol)[0]
    move = camelot_move(seed.camelot, other.camelot)
    raw = float(_KEY_POINTS[move])
    chips = [_MOVE_CHIPS.get(move, move)]

    pitch = pitch_factor(seed.bpm, other.bpm)
    if pitch is None:
        chips.append("no BPM")
    else:
        pct, factor = pitch
        raw += _BPM_POINTS * max(0.0, 1 - abs(pct) / max(tol * 100, EPS))
        chips.append(f"{pct:+.1f}% pitch" if round(pct, 1) else "same tempo")
        if factor != 1:
            raw -= _HALF_DOUBLE_PENALTY
            chips.append("half-time" if factor == 2 else "double-time")

    if _known(seed.energy) and _known(other.energy):
        de = other.energy - seed.energy
        raw += _closeness(de, _ENERGY_POINTS)
        if round(de, 2):
            chips.append(f"energy {de:+.2f}")
    else:
        raw += _ENERGY_POINTS / 2
    if _known(seed.danceability) and _known(other.danceability):
        dd = other.danceability - seed.danceability
        raw += _closeness(dd, _DANCE_POINTS)
        if abs(dd) >= 0.10:
            chips.append(f"dance {dd:+.2f}")
    else:
        raw += _DANCE_POINTS / 2

    raw = max(0.0, min(100.0, raw))
    value = max(round(raw), _PASS_FLOOR) if passed else min(round(raw * _FAIL_SCALE), _PASS_FLOOR - 1)
    return Score(value=min(value, 100), passed=passed, move=move, chips=chips)


# --- energy ratings ------------------------------------------------------------------------------------------------

# (feature, typical mean, typical std, weight) for a house / tech-house crate; puts the features on one scale.
_ENERGY_FEATURES = (
    ("e", 0.86, 0.09, 0.55),
    ("lo", -5.8, 1.6, 0.20),
    ("da", 0.76, 0.09, 0.17),
    ("h", 0.55, 0.22, 0.08),
)
_ENERGY_MID = 5.5
_ENERGY_SLOPE = 0.9  # rating points per std of absolute energy (used when the crate is too small to rank)


def _feature(track: Track, name: str) -> float | None:
    if name == "e":
        v: object = track.energy
    elif name == "da":
        v = track.danceability
    else:
        v = (track.raw or {}).get(name)
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    if name in ("e", "da", "lo") and v == 0:  # 0 means missing for these
        return None
    return float(v)


def _energy_z(track: Track) -> float | None:
    """Weighted z-score of energy / loudness / danceability / valence vs a typical dance crate; None if all missing."""
    total = weight = 0.0
    for name, mean, std, w in _ENERGY_FEATURES:
        v = _feature(track, name)
        if v is not None:
            total += w * max(-3.0, min(3.0, (v - mean) / std))
            weight += w
    return total / weight if weight else None


def energy_levels(tracks: list[Track]) -> dict[str, float]:
    """Continuous 1-10 energy per track id: crate rank + min-max blended with an absolute estimate for tiny crates."""
    zs: dict[str, float | None] = {}
    for t in tracks:
        zs.setdefault(t.id, _energy_z(t))
    known = {i: z for i, z in zs.items() if z is not None}
    n = len(known)

    def absolute(z: float) -> float:
        return max(1.0, min(10.0, _ENERGY_MID + _ENERGY_SLOPE * z))

    levels: dict[str, float] = {}
    if n:
        lo, hi = min(known.values()), max(known.values())
        w = min(1.0, (n - 1) / 6)  # 7+ tracks: purely crate-relative
        ordered = sorted(known.values())
        for i, z in known.items():
            level = absolute(z)
            if w:
                below = sum(1 for v in ordered if v < z - EPS)
                ties = sum(1 for v in ordered if abs(v - z) <= EPS)
                pct = (below + (ties - 1) / 2) / (n - 1)
                span = (z - lo) / (hi - lo) if hi - lo > EPS else 0.5
                level = w * (1 + 9 * (pct + span) / 2) + (1 - w) * level
            levels[i] = level
    fallback = sorted(levels.values())[len(levels) // 2] if levels else _ENERGY_MID
    return {i: levels.get(i, fallback) for i in zs}


def energy_ratings(tracks: list[Track]) -> dict[str, int]:
    """1-10 energy per track id, relative to this crate (a lone track gets ~4-7 from its absolute energy)."""
    return {i: max(1, min(10, math.floor(v + 0.5))) for i, v in energy_levels(tracks).items()}


# --- set order -----------------------------------------------------------------------------------------------------

_KEY_STEP_COST = {"same": 0.0, "+1": 1.0, "-1": 1.0, "relative": 1.0, "+2 boost": 2.0, "+7 boost": 2.0, "diagonal": 2.0}
_PITCH_COST = 0.5  # per % of pitch change (2% drift costs about one +/-1 key move)
SHAPE_COST = 4.0  # weight of straying from the target energy curve
JUMP_COST = 3.0  # weight of the energy step between neighbours (keeps the ease-off gentle)
_UNKNOWN_COST = 5.0  # unparseable key / missing BPM
_PEAK_AT = 2 / 3
_MAX_BLOCK = 4
_KICKS = 12  # perturb-and-reimprove rounds after the initial local search
_MAX_EVALS = 150_000  # shared cap on candidate evaluations so 30 tracks stay well under a second


def _key_cost(seed: str, other: str) -> float:
    """Camelot distance seed -> other: 0 same, 1 for +/-1 or relative, 2 for boosts/diagonal, 3.5-7 for clashes."""
    a, b = _parse_camelot(seed), _parse_camelot(other)
    if a is None or b is None:
        return _UNKNOWN_COST
    move = camelot_move(seed, other)
    if move in _KEY_STEP_COST:
        return _KEY_STEP_COST[move]
    d = (b[0] - a[0]) % 12
    return 3.0 + 0.5 * min(d, 12 - d) + (1.0 if a[1] != b[1] else 0.0)


def pair_cost(a: Track, b: Track) -> float:
    """Mixing cost of playing b after a: Camelot distance plus BPM drift (pitch %, half/double aware)."""
    pitch = pitch_factor(a.bpm, b.bpm)
    if pitch is None:
        bpm_cost = _UNKNOWN_COST
    else:
        bpm_cost = _PITCH_COST * abs(pitch[0]) + (0.5 if pitch[1] != 1 else 0.0)
    return _key_cost(a.camelot, b.camelot) + bpm_cost


def energy_target(t: float, start: float, end: float = 0.55) -> float:
    """Desired normalised energy at fraction t of the set: ramps up to 1.0 at 2/3, then eases down to `end`."""
    if t <= _PEAK_AT:
        return start + (1.0 - start) * t / _PEAK_AT
    return 1.0 - (1.0 - end) * (t - _PEAK_AT) / (1.0 - _PEAK_AT)


def order_set(seed: Track, tracks: list[Track]) -> list[Track]:
    """Playable running order: seed first, every track once, smooth keys/tempo, energy building to a peak ~2/3 in."""
    seen = {seed.id}
    pool: list[Track] = []
    for t in tracks:
        if t.id not in seen:
            seen.add(t.id)
            pool.append(t)
    if len(pool) < 2:
        return [seed, *pool]

    items = [seed, *pool]
    n = len(items)
    pair = [[pair_cost(a, b) if a is not b else 0.0 for b in items] for a in items]

    levels = energy_levels(items)
    raw_en = [levels[t.id] for t in items]
    lo, hi = min(raw_en), max(raw_en)
    en = [(v - lo) / (hi - lo) if hi - lo > EPS else 0.5 for v in raw_en]
    start = max(0.2, min(0.6, en[0]))
    target = [energy_target(p / (n - 1), start) for p in range(n)]
    shape = [[SHAPE_COST * abs(en[j] - target[p]) for p in range(n)] for j in range(n)]
    step = [[pair[a][b] + JUMP_COST * abs(en[a] - en[b]) for b in range(n)] for a in range(n)]

    def cost(order: list[int]) -> float:
        return sum(step[a][b] for a, b in zip(order, order[1:])) + sum(shape[j][p] for p, j in enumerate(order) if p)

    def greedy() -> list[int]:
        order, left = [0], set(range(1, n))
        while left:
            p, cur = len(order), order[-1]
            nxt = min(sorted(left), key=lambda j: step[cur][j] + shape[j][p])
            order.append(nxt)
            left.remove(nxt)
        return order

    def neighbours(order: list[int]) -> Iterator[list[int]]:
        """Candidate orders: segment reversal, swap, and moving a block of 1-4 tracks (either way round) elsewhere."""
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                yield order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                yield order[:i] + [order[j]] + order[i + 1 : j] + [order[i]] + order[j + 1 :]
        for size in range(1, _MAX_BLOCK + 1):
            for i in range(1, n - size + 1):
                block, rest = order[i : i + size], order[:i] + order[i + size :]
                for k in range(1, len(rest) + 1):
                    if k == i:
                        continue
                    yield rest[:k] + block + rest[k:]
                    if size > 1:
                        yield rest[:k] + block[::-1] + rest[k:]

    evals = 0

    def improve(order: list[int]) -> list[int]:
        nonlocal evals
        best, best_cost = order, cost(order)
        improved = True
        while improved and evals < _MAX_EVALS:
            improved = False
            for cand in neighbours(best):
                evals += 1
                c = cost(cand)
                if c < best_cost - 1e-9:
                    best, best_cost, improved = cand, c, True
                    break
        return best

    shaped = [0] * n  # perfect energy-curve fit, ignoring keys: lowest-energy track at the lowest-target slot, etc.
    for p, j in zip(sorted(range(1, n), key=lambda p: (target[p], p)), sorted(range(1, n), key=lambda j: (en[j], j))):
        shaped[p] = j
    starts = [greedy(), list(range(n)), [0, *range(n - 1, 0, -1)], shaped]
    best = min((improve(s) for s in starts), key=cost)
    rng = random.Random(n)
    for _ in range(_KICKS):
        i, j = sorted(rng.sample(range(1, n), 2))
        kicked = best[:i] + best[j:] + best[i:j] if rng.random() < 0.5 else best[:i] + best[i:j][::-1] + best[j:]
        cand = improve(kicked)
        if cost(cand) < cost(best) - 1e-9:
            best = cand
    return [items[i] for i in best]
