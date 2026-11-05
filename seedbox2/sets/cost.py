"""What a running order costs. One unit is about one uncomfortable move: lower is better and only the sums matter.

An order is scored by
- **Key:** Camelot moves (`key_step`). A track may be played a semitone up or down (`shift`, whole semitones, as a mix
  plan's `key_shift`), which costs `Weights.key_fix`. A shift stays on for the whole track, so it changes both of the
  track's transitions: `Problem.best_shifts` picks the shifts for an order with a Viterbi pass.
- **Tempo:** the pair's distance, in percent and also at half or double time. Under `TEMPO_PREFER_PCT` it barely counts,
  above it the cost climbs, and over `TEMPO_LIMIT_PCT` it's a breach, which is as bad as several key clashes. A breach
  isn't forbidden, since some sets can't avoid one: the solver makes as few as it can and the explanation says so.
- **Tempo journey:** the distance from a slow drift across the set, `TempoJourney`. It's what makes the set go one way
  rather than wander inside the limit.
- **Energy:** the distance from the arc at the track's position, and how far each step differs from the step the arc
  takes there. Both on the set's own range, so the arc reaches from its quietest track to its most intense.
- **Vibe:** the CLAP distance from the end of one track to the start of the next and between their main parts, relative
  to how far apart this set's tracks are. CLAP cosines aren't an absolute scale (see the README).
- **Vocals:** each run of three vocal-heavy tracks.
- **Genre:** a change of genre tag, unless the tags share a word ("deep house", "tech house").

What's unknown costs a small fixed amount where a term depends on a pair, so a known good move beats an unknown one, and
nothing where it depends on a position. The weights are a prior, not fitted to anything: no human-ordered sets exist here
yet to fit them on.

Scoring is vectorised over a batch of orders, since that's how the solver asks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from seedbox2.sets.arcs import Arc
from seedbox2.sets.features import TrackFeatures

TEMPO_PREFER_PCT = 5.0
TEMPO_LIMIT_PCT = 8.0
HALF_TIME_FACTORS = (1.0, 0.5, 2.0)
SHIFTS = (0, 1, -1)  # semitones a track may be played away from its key; 0 first, so a tie keeps the track as it is
SEMITONE_CAMELOT = 7  # one semitone up is seven steps on the wheel
VOCAL_LIGHT, VOCAL_HEAVY = 0.25, 0.6  # shares of sung beats: under the first it isn't a vocal track, over the second it is
VIBE_MIN_SPREAD = 0.1  # a set with all its cosines closer than this is one vibe, whatever the order
MIN_ENERGY_RANGE = 0.05  # less than this between a set's extremes: nothing for an arc to shape


@dataclass(frozen=True)
class Weights:
    key: float = 1.0
    key_fix: float = 1.5  # dearer than a +2 move, cheaper than a clash
    key_unknown: float = 0.5
    tempo: float = 1.5  # what a pair at TEMPO_PREFER_PCT costs
    tempo_breach: float = 15.0  # on top, for a pair over the limit
    tempo_unknown: float = 0.5
    half_time: float = 0.5
    journey: float = 1.5  # per TEMPO_PREFER_PCT away from the journey's target
    energy_level: float = 3.0
    energy_step: float = 1.5
    vibe: float = 1.0
    vibe_unknown: float = 0.3
    vocal_run: float = 1.0
    genre: float = 0.4


@dataclass(frozen=True)
class TempoJourney:
    """The tempo the set drifts along: geometric from `start` to `end`, in BPM."""

    start: float
    end: float
    auto: bool = False

    def sample(self, n: int) -> np.ndarray:
        return np.geomspace(self.start, self.end, n) if n > 1 else np.full(n, float(self.start))

    def to_json(self) -> dict:
        return {"start": round(self.start, 1), "end": round(self.end, 1), "auto": self.auto}


# --- keys -----------------------------------------------------------------------------------------------------------
# A key's index here: (number - 1) + 12 for the B side, 24 for unknown.

UNKNOWN_KEY = 24


def key_index(camelot: str | None) -> int:
    if not camelot:
        return UNKNOWN_KEY
    letter = camelot[-1].upper()
    try:
        number = int(camelot[:-1])
    except ValueError:
        return UNKNOWN_KEY
    if letter not in "AB" or not 1 <= number <= 12:
        return UNKNOWN_KEY
    return number - 1 + (12 if letter == "B" else 0)


def camelot_name(index: int) -> str | None:
    return None if index == UNKNOWN_KEY else f"{index % 12 + 1}{'B' if index >= 12 else 'A'}"


def shifted(index: int, semitones: int) -> int:
    if index == UNKNOWN_KEY:
        return index
    return (index % 12 + semitones * SEMITONE_CAMELOT) % 12 + (12 if index >= 12 else 0)


def key_step(a: int, b: int) -> float:
    """The discomfort of a Camelot move in itself, 0 for the same key and 3 for a clash."""
    steps = (b % 12 - a % 12 + 6) % 12 - 6
    same_side = (a >= 12) == (b >= 12)
    if steps == 0:
        return 0.0 if same_side else 0.25
    if abs(steps) == 1:
        return 0.25 if same_side else 1.0
    if abs(steps) == 2 and same_side:
        return 1.0
    return 3.0


def key_move_name(a: int, b: int) -> str:
    steps = (b % 12 - a % 12 + 6) % 12 - 6
    same_side = (a >= 12) == (b >= 12)
    if steps == 0:
        return "same key" if same_side else "relative"
    if abs(steps) <= 2 and same_side:
        return f"{steps:+d} on the wheel"
    if abs(steps) == 1:
        return "diagonal"
    return "clash"


_KEY_STEPS = np.array([[key_step(a, b) for b in range(24)] for a in range(24)])
_SHIFTED = np.array([[shifted(k, s) for s in SHIFTS] for k in range(UNKNOWN_KEY + 1)], np.int64)  # key x shift's index


# --- tempo ----------------------------------------------------------------------------------------------------------


def tempo_distances(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(percent b is from a, the factor on b that gets closest: 1, or 0.5/2 at half or double time), element by element.
    A tie stays at the same tempo."""
    factors = np.array(HALF_TIME_FACTORS)  # 1 first
    pct = np.abs(b[..., None] * factors / a[..., None] - 1) * 100
    pick = np.argmin(pct, axis=-1)
    return np.take_along_axis(pct, pick[..., None], axis=-1)[..., 0], factors[pick]


def tempo_cost(pct: np.ndarray, w: Weights) -> np.ndarray:
    prefer = (pct / TEMPO_PREFER_PCT) ** 2
    over = 1.0 + (pct - TEMPO_PREFER_PCT) * 2.0 / (TEMPO_LIMIT_PCT - TEMPO_PREFER_PCT)
    breach = w.tempo_breach * (pct > TEMPO_LIMIT_PCT)
    return w.tempo * np.where(pct <= TEMPO_PREFER_PCT, prefer, over) + breach


def _fold(bpm: np.ndarray, target: np.ndarray | float) -> np.ndarray:
    """The tempos at the half or double time nearest `target`."""
    ratio = bpm / target
    return np.where(ratio < 0.707, bpm * 2, np.where(ratio > 1.414, bpm / 2, bpm))


def auto_journey(tracks: Sequence[TrackFeatures], arc: Arc) -> TempoJourney | None:
    """Drift across the middle of the set's tempos, in the direction the arc goes. None if no tempo is known."""
    known = np.array([t.bpm for t in tracks if t.bpm], np.float64)
    if known.size == 0:
        return None
    folded = _fold(known, float(np.median(known)))
    low, mid, high = np.percentile(folded, [20, 50, 80])
    direction = arc.direction()
    start, end = (low, high) if direction > 0 else (high, low) if direction < 0 else (mid, mid)
    return TempoJourney(float(start), float(end), auto=True)


# --- pairs ----------------------------------------------------------------------------------------------------------


def _genre_words(genre: str | None) -> frozenset[str]:
    return frozenset(genre.casefold().replace("-", " ").replace("/", " ").split()) if genre else frozenset()


def _vibe_similarity(a: TrackFeatures, b: TrackFeatures) -> float | None:
    """Cosine from the end of `a` to the start of `b`, averaged with their main parts'."""
    sims = []
    for x, y in (("outro", "intro"), ("main", "main")):
        if x in a.vibes and y in b.vibes:
            sims.append(float(a.vibes[x] @ b.vibes[y]))
    return sum(sims) / len(sims) if sims else None


def _vibe_matrix(tracks: Sequence[TrackFeatures]) -> tuple[np.ndarray, np.ndarray]:
    """(0-1 distance relative to this set's spread, whether it's known), n x n."""
    n = len(tracks)
    sims = np.full((n, n), np.nan)
    for i, a in enumerate(tracks):
        if a.vibes:
            for j, b in enumerate(tracks):
                if i != j and b.vibes:
                    s = _vibe_similarity(a, b)
                    if s is not None:
                        sims[i, j] = s
    known = ~np.isnan(sims)
    if not known.any():
        return np.zeros((n, n)), known
    dist = 1.0 - sims
    low, high = np.percentile(dist[known], [10, 90])
    scaled = np.clip((dist - low) / max(high - low, VIBE_MIN_SPREAD), 0.0, 1.0)
    return np.nan_to_num(scaled), known


class Problem:
    """The numbers of one set under one arc and journey: items by index, positions 0..n-1."""

    def __init__(self, tracks: Sequence[TrackFeatures], arc: Arc, journey: TempoJourney | None = None,
                 weights: Weights | None = None) -> None:  # fmt: skip
        self.tracks = list(tracks)
        self.arc = arc
        self.w = w = weights or Weights()
        self.n = n = len(self.tracks)
        self.journey = journey if journey is not None else auto_journey(self.tracks, arc)

        self.keys = np.array([key_index(t.camelot) for t in self.tracks], np.int64)
        self.bpm = np.array([t.bpm or np.nan for t in self.tracks], np.float64)
        energy = np.array([np.nan if t.energy is None else t.energy for t in self.tracks], np.float64)
        self.energy = energy
        self.has_energy = ~np.isnan(energy)
        self.levels, self.level_lo, self.level_hi = self._levels(energy)
        self.arc_levels = arc.sample(n)
        self.arc_steps = np.diff(self.arc_levels)
        self.targets = self.journey.sample(n) if self.journey else np.zeros(n)

        heavy = [(t.vocals if t.vocals is not None else 0.0) for t in self.tracks]
        self.heavy = np.clip((np.array(heavy, np.float64) - VOCAL_LIGHT) / (VOCAL_HEAVY - VOCAL_LIGHT), 0.0, 1.0)
        self.tempo_pct, self.tempo_factor, self.tempo_known = self._tempo_pairs()
        self.vibe, self.vibe_known = _vibe_matrix(self.tracks)
        self.genre_change = self._genre_changes()

        tempo = np.where(self.tempo_known, tempo_cost(self.tempo_pct, w), w.tempo_unknown)
        tempo = tempo + w.half_time * (self.tempo_known & (self.tempo_factor != 1.0))
        self.terms_tempo = tempo
        self.terms_vibe = np.where(self.vibe_known, w.vibe * self.vibe, w.vibe_unknown)
        self.terms_genre = w.genre * self.genre_change
        self.base_pairs = tempo + self.terms_vibe + self.terms_genre
        self._key_table = np.full((UNKNOWN_KEY + 1, UNKNOWN_KEY + 1), w.key_unknown)
        self._key_table[:24, :24] = w.key * _KEY_STEPS
        self.level_cost = self._level_cost()
        self.journey_cost = self._journey_cost()
        self.slot = self.level_cost + self.journey_cost

    # --- set-up -----------------------------------------------------------------------------------------------------

    @staticmethod
    def _levels(energy: np.ndarray) -> tuple[np.ndarray, float, float]:
        known = energy[~np.isnan(energy)]
        if known.size == 0:
            return np.full(energy.shape, 0.5), 0.0, 1.0
        lo, hi = float(known.min()), float(known.max())
        if hi - lo < MIN_ENERGY_RANGE:
            return np.full(energy.shape, 0.5), lo, hi
        return np.nan_to_num((energy - lo) / (hi - lo), nan=0.5), lo, hi

    def _tempo_pairs(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = self.n
        known = ~np.isnan(self.bpm)
        both = known[:, None] & known[None, :] & ~np.eye(n, dtype=bool)
        filled = np.nan_to_num(self.bpm, nan=1.0)
        pct, factor = tempo_distances(filled[:, None], filled[None, :])
        return np.where(both, pct, 0.0), np.where(both, factor, 1.0), both

    def _genre_changes(self) -> np.ndarray:
        words = [_genre_words(t.genre) for t in self.tracks]
        return np.array([[1.0 if a and b and not (a & b) else 0.0 for b in words] for a in words]).reshape(self.n, self.n)

    def _level_cost(self) -> np.ndarray:
        if not self.n:
            return np.zeros((0, 0))
        away = np.abs(self.levels[:, None] - self.arc_levels[None, :])
        return self.w.energy_level * away * self.has_energy[:, None]

    def _journey_cost(self) -> np.ndarray:
        n = self.n
        if self.journey is None or not n:
            return np.zeros((n, n))
        known = ~np.isnan(self.bpm)
        bpm = np.nan_to_num(self.bpm, nan=1.0)[:, None]
        away = np.abs(_fold(bpm, self.targets[None, :]) / self.targets[None, :] - 1) * 100 / TEMPO_PREFER_PCT
        return self.w.journey * away * known[:, None]

    # --- keys -------------------------------------------------------------------------------------------------------

    def key_matrix(self, shifts: np.ndarray | None = None) -> np.ndarray:
        """Key cost of every pair, with each track played at its shift (all 0 if none)."""
        keys = self.keys if shifts is None else _SHIFTED[self.keys, [SHIFTS.index(int(s)) for s in shifts]] if self.n else self.keys
        return self._key_table[np.ix_(keys, keys)]

    def free_shift_keys(self) -> np.ndarray:
        """Each pair at the best shift of the second track, ignoring what the shift does to its other transition: what
        the beam search looks at to give a fix a chance."""
        best = self.key_matrix()
        known = self.keys != UNKNOWN_KEY
        for s in range(1, len(SHIFTS)):
            trial = self._key_table[np.ix_(self.keys, _SHIFTED[self.keys, s])] + self.w.key_fix
            best = np.where(known[None, :], np.minimum(best, trial), best)
        return best

    def fix_cost(self, shifts: np.ndarray | None) -> float:
        return 0.0 if shifts is None else float(self.w.key_fix * np.count_nonzero(shifts))

    def _key_paths(self, orders: np.ndarray) -> tuple[np.ndarray, list[np.ndarray]]:
        """Viterbi over each order's shifts: (the cheapest cost so far, batch x shifts, at the last position; for each
        later position, which shift came before each shift). A track of unknown key isn't shifted."""
        keys = _SHIFTED[self.keys[orders]]  # batch x n x shifts
        fix = np.where(np.array(SHIFTS) != 0, self.w.key_fix, 0.0)
        fixes = np.where((self.keys[orders] == UNKNOWN_KEY)[..., None] & (fix > 0), np.inf, fix)
        best = fixes[:, 0]
        back: list[np.ndarray] = []
        for pos in range(1, orders.shape[1]):
            steps = self._key_table[keys[:, pos - 1, :, None], keys[:, pos, None, :]]  # batch x from x to
            through = best[:, :, None] + steps
            came = np.argmin(through, axis=1)
            back.append(came)
            best = np.take_along_axis(through, came[:, None, :], axis=1)[:, 0] + fixes[:, pos]
        return best, back

    def best_shifts(self, order: Sequence[int]) -> np.ndarray:
        """The shift per item that makes this order's key costs and fixes cheapest. A tie plays the track as it is."""
        shifts = np.zeros(self.n, np.int64)
        if self.n < 2:
            return shifts
        o = np.asarray(order, np.int64)
        best, back = self._key_paths(o[None, :])
        s = int(np.argmin(best[0]))
        shifts[o[-1]] = SHIFTS[s]
        for pos in range(self.n - 1, 0, -1):
            s = int(back[pos - 1][0, s])
            shifts[o[pos - 1]] = SHIFTS[s]
        return shifts

    def true_cost(self, orders: np.ndarray) -> np.ndarray:
        """The cost of each order at its own best shifts."""
        if self.n == 0:
            return np.zeros(orders.shape[0])
        return self.batch_cost(orders, self.base_pairs) + self._key_paths(orders)[0].min(axis=1)

    # --- scoring ----------------------------------------------------------------------------------------------------

    def step_matrix(self, shifts: np.ndarray | None = None) -> np.ndarray:
        return self.base_pairs + self.key_matrix(shifts)

    def energy_step_costs(self, orders: np.ndarray) -> np.ndarray:
        """Batch x (n - 1): how far each step's energy change differs from the arc's there."""
        a, b = orders[:, :-1], orders[:, 1:]
        away = np.abs(self.levels[b] - self.levels[a] - self.arc_steps)
        return self.w.energy_step * away * (self.has_energy[a] & self.has_energy[b])

    def run_costs(self, orders: np.ndarray) -> np.ndarray:
        """Batch x (n - 2): each three tracks in a row, by how vocal-heavy all three are."""
        if orders.shape[1] < 3 or not self.heavy.any():
            return np.zeros((orders.shape[0], max(orders.shape[1] - 2, 0)))
        return self.w.vocal_run * self.heavy[orders[:, :-2]] * self.heavy[orders[:, 1:-1]] * self.heavy[orders[:, 2:]]

    def batch_cost(self, orders: np.ndarray, step: np.ndarray) -> np.ndarray:
        """The cost of each order (batch x n), with `step` from `step_matrix`, without the fix costs."""
        if self.n == 0:
            return np.zeros(orders.shape[0])
        total = self.slot[orders, np.arange(self.n)].sum(axis=1)
        if self.n > 1:
            total = total + step[orders[:, :-1], orders[:, 1:]].sum(axis=1) + self.energy_step_costs(orders).sum(axis=1)
        return total + self.run_costs(orders).sum(axis=1)

    def cost(self, order: Sequence[int], shifts: np.ndarray | None = None) -> float:
        """The whole order's cost. With no shifts given, the best ones for this order."""
        if shifts is None:
            shifts = self.best_shifts(order)
        o = np.asarray(order, np.int64).reshape(1, -1)
        return float(self.batch_cost(o, self.step_matrix(shifts))[0]) + self.fix_cost(shifts)
