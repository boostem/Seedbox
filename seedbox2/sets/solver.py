"""Finding a cheap running order: beam search for a start, then local search with kicks, alternating with the Viterbi
pass that picks the semitone fixes. Pinned tracks never leave their positions.

The budget is a number of steps, not a time, so the same set always gets the same order (the random moves have a fixed
seed).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from seedbox2.sets.cost import Problem

BEAM_WIDTH = 300
BATCH = 3000  # moves tried per local search step, when there are more than this
BLOCK = 3  # the longest block of tracks a move carries elsewhere
MAX_STEPS = 120
EXACT_FREE = 8  # this few free tracks are simply all tried: 40,320 orders
KICKS = 8
MANY_FREE = 40  # more free tracks than this get FEW_KICKS: the set's already long, and each costs more
FEW_KICKS = 2
TRUE_BATCH = 1000
EXACT_CHUNK = 5000
MAX_ROUNDS = 4  # of local search and Viterbi
TRUE_STEPS = 12  # of local search on the true cost, per start: much the dearest kind


class SolverError(ValueError):
    pass


@dataclass
class Solution:
    order: list[int]
    shifts: np.ndarray  # per item, semitones
    cost: float


def check_locks(n: int, locks: Mapping[int, int] | None) -> dict[int, int]:
    """`{position: item}` checked: positions in range, and no item in two places."""
    locks = dict(locks or {})
    if any(not 0 <= p < n or not 0 <= i < n for p, i in locks.items()):
        raise SolverError("a pinned position or track is out of range")
    if len(set(locks.values())) != len(locks):
        raise SolverError("a track is pinned in two places")
    return locks


def _beam(p: Problem, step: np.ndarray, locks: dict[int, int], width: int) -> np.ndarray:
    """The cheapest orders found by keeping the best partial orders at each position, cheapest first. Two partial orders
    with the same tracks and the same last track can only differ by what they've spent, so the dearer one goes."""
    n = p.n
    rng = np.random.default_rng(n)
    zobrist = rng.integers(1, 2**62, n, dtype=np.int64)
    pinned = np.zeros(n, bool)
    for item in locks.values():
        pinned[item] = True

    paths = np.zeros((1, 0), np.int64)
    visited = np.zeros((1, n), bool)
    costs = np.zeros(1)
    codes = np.zeros(1, np.int64)
    for pos in range(n):
        allowed = np.zeros(n, bool)
        if pos in locks:
            allowed[locks[pos]] = True
        else:
            allowed[~pinned] = True
        add = np.broadcast_to(p.slot[:, pos], (costs.size, n)).copy()
        if pos:
            last = paths[:, -1]
            add += step[last]
            away = np.abs(p.levels[None, :] - p.levels[last][:, None] - p.arc_steps[pos - 1])
            add += p.w.energy_step * away * (p.has_energy[None, :] & p.has_energy[last][:, None])
        if pos > 1:
            add += p.w.vocal_run * p.heavy[paths[:, -2]][:, None] * p.heavy[paths[:, -1]][:, None] * p.heavy[None, :]
        total = costs[:, None] + add
        total[visited | ~allowed] = np.inf
        flat = total.ravel()
        take = min(flat.size, width * 4)
        best = np.argpartition(flat, take - 1)[:take] if take < flat.size else np.arange(flat.size)
        best = best[np.isfinite(flat[best])]
        best = best[np.argsort(flat[best], kind="stable")]
        parent, item = np.divmod(best, n)
        code = codes[parent] ^ zobrist[item]
        _, first = np.unique(np.stack([code, item], axis=1), axis=0, return_index=True)
        keep = np.sort(first)[:width]
        parent, item = parent[keep], item[keep]
        paths = np.concatenate([paths[parent], item[:, None]], axis=1)
        visited = visited[parent].copy()
        visited[np.arange(keep.size), item] = True
        costs = flat[best[keep]]
        codes = code[keep]
    return paths


def _block(base: list[int], i: int, size: int, at: int, flip: bool) -> list[int] | None:
    """`size` things from `i` carried to `at` in what's left, perhaps turned round. None if they don't fit."""
    if i + size > len(base):
        return None
    chunk, rest = base[i : i + size], base[:i] + base[i + size :]
    return None if at > len(rest) else rest[:at] + (chunk[::-1] if flip else chunk) + rest[at:]


@lru_cache(maxsize=32)
def _every_move(free: int) -> np.ndarray:
    base = list(range(free))
    out: list[list[int]] = []
    for i in range(free - 1):
        for j in range(i + 1, free):
            out.append(base[:i] + base[i : j + 1][::-1] + base[j + 1 :])
            swap = base.copy()
            swap[i], swap[j] = swap[j], swap[i]
            out.append(swap)
    for size in range(1, BLOCK + 1):
        for i in range(free):
            for at in range(free - size + 1):
                for flip in (False, True) if size > 1 else (False,):
                    m = _block(base, i, size, at, flip)
                    if m is not None:
                        out.append(m)
    return np.array(out, np.int64) if out else np.zeros((0, free), np.int64)


def _moves(free: int, rng: np.random.Generator, batch: int = BATCH) -> np.ndarray:
    """Rearrangements of `free` things (batch x free): reversals, swaps and blocks carried elsewhere, either way round.
    All of them while they fit in a batch, otherwise a fresh random sample each step."""
    if free * (free - 1) + BLOCK * free * free * 2 <= batch:
        return _every_move(free)
    grid = np.arange(free)[None, :]
    i, j = np.sort(rng.integers(0, free, (batch, 2)), axis=1).T
    kinds = rng.integers(0, 3, batch)
    out = np.repeat(grid, batch, axis=0)

    flip = kinds == 0  # a reversal: position p takes what was at i + j - p
    inside = (grid >= i[:, None]) & (grid <= j[:, None])
    out[flip] = np.where(inside, i[:, None] + j[:, None] - grid, grid)[flip]

    swap = np.flatnonzero(kinds == 1)
    out[swap, i[swap]], out[swap, j[swap]] = j[swap], i[swap]

    carry = np.flatnonzero(kinds == 2)  # a block: where each thing goes, then the inverse
    size = np.minimum(rng.integers(1, BLOCK + 1, carry.size), free - i[carry])[:, None]
    start = i[carry][:, None]
    at = rng.integers(0, free, carry.size)[:, None] % (free - size + 1)
    turned = rng.integers(0, 2, carry.size)[:, None].astype(bool)
    now = grid[:, :]
    rest = np.where(now < start, now, now - size)  # the place of what stays, among what stays
    goes = np.where(rest < at, rest, rest + size)
    offset = now - start
    block = at + np.where(turned, size - 1 - offset, offset)
    goes = np.where((now >= start) & (now < start + size), block, goes)
    out[carry[:, None], goes] = now
    return out


def _polish(cost: Callable[[np.ndarray], np.ndarray], order: np.ndarray, slots: np.ndarray, rng: np.random.Generator,
            max_steps: int = MAX_STEPS, batch_size: int = BATCH) -> tuple[np.ndarray, float]:  # fmt: skip
    """Best-improvement local search over the free positions `slots`."""
    current = order.copy()
    best_cost = float(cost(current[None, :])[0])
    if slots.size < 2:
        return current, best_cost
    dry = 0
    for _ in range(max_steps):
        moves = _moves(slots.size, rng, batch_size)
        if not moves.size:
            break
        batch = np.repeat(current[None, :], moves.shape[0], axis=0)
        batch[:, slots] = current[slots][moves]
        costs = cost(batch)
        i = int(np.argmin(costs))
        if costs[i] < best_cost - 1e-9:
            current, best_cost = batch[i].copy(), float(costs[i])
            dry = 0
        else:
            dry += 1
            if moves.shape[0] < batch_size or dry >= 3:
                break
    return current, best_cost


def _kick(order: np.ndarray, slots: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A shake too large for one move to undo: the free tracks cut in three and the pieces swapped, or one reversed."""
    out = order.copy()
    i, j = np.sort(rng.choice(slots.size, 2, replace=False))
    q = order[slots]
    q = np.concatenate([q[:i], q[j:], q[i:j]]) if rng.random() < 0.5 else np.concatenate([q[:i], q[i:j][::-1], q[j:]])
    out[slots] = q
    return out


def _exact(p: Problem, best: Solution, slots: np.ndarray) -> Solution:
    """Every order of the free tracks. `best` stays unless one beats it, so a tie keeps what the search found."""
    current = np.array(best.order, np.int64)
    free = current[slots]
    perms = np.array(list(itertools.permutations(range(free.size))), np.int64)
    for at in range(0, perms.shape[0], EXACT_CHUNK):
        part = perms[at : at + EXACT_CHUNK]
        batch = np.repeat(current[None, :], part.shape[0], axis=0)
        batch[:, slots] = free[part]
        costs = p.true_cost(batch)
        i = int(np.argmin(costs))
        if costs[i] < best.cost - 1e-9:
            order = batch[i].tolist()
            best = Solution(order, p.best_shifts(order), float(costs[i]))
    return best


def solve(p: Problem, locks: Mapping[int, int] | None = None, *, width: int = BEAM_WIDTH) -> Solution:
    """The cheapest order found. `locks` maps a position to the item that must play there."""
    n = p.n
    pins = check_locks(n, locks)
    if n == 0:
        return Solution([], np.zeros(0, np.int64), 0.0)
    rng = np.random.default_rng(n + 1)
    slots = np.array([q for q in range(n) if q not in pins], np.int64)

    plain = p.step_matrix()
    free_shift = p.base_pairs + p.free_shift_keys()
    # the order as it is goes first, so a tie keeps it and the result is never worse than what the set had
    pinned_items = set(pins.values())
    as_it_is = iter(i for i in range(n) if i not in pinned_items)
    current = np.array([pins[q] if q in pins else next(as_it_is) for q in range(n)], np.int64)
    starts = [current, _beam(p, plain, pins, width)[0], _beam(p, free_shift, pins, width)[0]]

    best: Solution | None = None
    for start in starts:
        # quick rounds with the shifts held still, each followed by a new choice of shifts
        order = start
        shifts = p.best_shifts(order.tolist())
        cost = p.cost(order.tolist(), shifts)
        for _ in range(MAX_ROUNDS):
            step = p.step_matrix(shifts)
            polished, _c = _polish(lambda o: p.batch_cost(o, step), order, slots, rng)  # noqa: B023 - called right here
            new_shifts = p.best_shifts(polished.tolist())
            new_cost = p.cost(polished.tolist(), new_shifts)
            if new_cost >= cost - 1e-9:
                break
            order, shifts, cost = polished, new_shifts, new_cost
        if best is None or cost < best.cost - 1e-9:
            best = Solution(order.tolist(), shifts, cost)
    assert best is not None

    if slots.size <= EXACT_FREE:
        return _exact(p, best, slots)
    if slots.size:
        # then the few steps that a move and a new choice of shifts only pay off together: this cost is the true one
        order = np.array(best.order, np.int64)
        order, cost = _polish(p.true_cost, order, slots, rng, TRUE_STEPS, TRUE_BATCH)
        for _ in range(KICKS if slots.size <= MANY_FREE else FEW_KICKS):
            kicked, c = _polish(p.true_cost, _kick(order, slots, rng), slots, rng, TRUE_STEPS, TRUE_BATCH)
            if c < cost - 1e-9:
                order, cost = kicked, c
        if cost < best.cost - 1e-9:
            best = Solution(order.tolist(), p.best_shifts(order.tolist()), p.cost(order.tolist()))
    return best
