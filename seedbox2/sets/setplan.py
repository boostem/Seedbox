"""Planning the whole set's transitions with lookahead (#61): the running order, each transition's cue pair and style,
and how much of each track plays, chosen together.

#60's solver orders tracks on what they cost each other; #57 picks a style one pair at a time. Here every ordered pair
gets a few **options** (`Option`): a style that applies to it (`styles.rank`) on a cue pair from the tracks' sections
(where A goes out: its outro, its last bars, after its last drop, after its first; where B comes in: its top, so its
intro ends as the overlap does, or onto its first drop), with what the option costs the pair (`Weights`) and the
seconds of the two tracks it leaves unplayed (`loss`).

For one order, a Viterbi pass (`Chain`) picks an option per transition over the state (last style, how many times in a
row, transitions since the last long blend), which carries the variety rules:

- a style never plays three times in a row; the same kind (blend, swap, filter, echo, cut) twice in a row costs a little;
- a cut is cheap where the set peaks (the arc's level there and B's own energy) or onto B's drop, dear in a quiet stretch;
- a long blend at least every `long_every` transitions: each one overdue costs `Weights.overdue`.

The set's length is the tracks' durations less each transition's loss, a sum over transitions, so a price per second
(`lam`) keeps the pass a Viterbi pass: it climbs a ladder until the length passes the target, then bisects there, and the
price that does best on the true cost wins (`Weights.length` per minute away from the target beyond `LENGTH_SLACK_S`).

The order and the options are searched together (`SetPlanner.solve`), from #60's order and the order as it stands. A
rolling-horizon search in the manner of DJ-MC (Liebman, Saar-Tsechansky & Stone 2015) fills the positions one at a
time: each next track tried is followed by every continuation of `lookahead` tracks (the `branch` cheapest at each level,
and always #60's next), rolled out to the end in #60's order, and the next track is the one whose best continuation
costs least, #60's cost of the whole order plus the plan's pass. The cheapest starts are then polished: every window of
up to `MAX_WINDOW` positions in every order of its tracks. The pass is kept as forward and backward tables, so a window
only re-runs its own transitions. The result is never worse than #60's order with its best plan, and the same input
always gets the same answer (no randomness).

`SetPlanner` keeps each pair's options, so planning the set again after one track moved (`plan_order`) costs the few new
pairs and one pass. **The weights are a prior** like #60's and #57's: they haven't met human-planned sets.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from seedbox2.mix import styles as style_lib
from seedbox2.sets.cost import Problem
from seedbox2.sets.solver import check_locks, solve

LOOKAHEAD = 4  # tracks the order search looks ahead (the ticket asks for 3-5), and the polishing window's size
BRANCH = 3  # next tracks tried at each level of the lookahead
LONG_EVERY = 4  # a long blend at least every this many transitions
LONG_TECHNIQUES = frozenset({"long blend", "rolling blend", "acapella intro"})
LONG_BARS = 16  # a blend or swap this long counts as a long blend too
LENGTH_SLACK_S = 60.0  # this close to the target length costs nothing
MAX_OPTIONS = 18  # per pair, cheapest first
MAX_PER_STYLE = 3  # cue pairs per style: the cheapest, the longest and the shortest
LAMBDA_FIRST = 0.002  # the first price of a second tried (a unit of cost per 8 minutes) ...
LAMBDA_LADDER = 13  # ... doubled up to this many times (to 8 per second: every cut the options offer)
LAMBDA_STEPS = 8  # then bisection steps where the length crossed the target
POLISH_ROUNDS = 2
POLISHED = 2  # starts polished, cheapest first
MAX_WINDOW = 4  # positions a polishing window spans: 5 would try 120 orders per window
A_MIN_SHARE = 0.45  # A goes out no earlier than this share of its duration
B_MAX_SHARE = 0.45  # B comes in no later than this share of its duration
UNKNOWN = "unknown"  # the style of a pair that can't be planned yet (a track without beats)

# bars of overlap per technique when its style doesn't say; by kind otherwise
_BARS = {"long blend": (16, 32), "rolling blend": (32,), "acapella intro": (16,), "phrase blend": (8, 16),
         "bass swap": (16,), "loop roll": (8,), "double drop": (16,), "echo freeze": (4,)}  # fmt: skip
_KIND_BARS = {"blend": (16,), "swap": (16,), "filter": (8,), "echo": (4,), "cut": (0,)}


@dataclass(frozen=True)
class Weights:
    style: float = 0.4  # per point of #57's score below the pair's best style
    cue_a: Mapping[str, float] = field(default_factory=lambda: {"outro": 0.0, "end": 0.2, "last drop": 0.8, "first drop": 1.6})
    cue_b: Mapping[str, float] = field(default_factory=lambda: {"start": 0.0, "intro": 0.1, "drop": 0.4})
    vocal_clash: float = 1.5  # a blend or swap with both tracks singing where they meet
    key_clash: float = 2.0  # a blend or swap over keys #60 calls a clash (3 or more); filters, echoes and cuts hide it
    key_clash_at: float = 3.0
    no_drop: float = 1.5  # a style that needs drops, on a pair without them
    cut_peak: float = 2.0  # a cut at the set's quietest costs this much more, at its peak this much less
    cut_on_drop: float = 0.8  # a cut onto B's drop is this much cheaper
    same_kind: float = 1.0  # the same kind twice in a row (blends may run on: half)
    overdue: float = 10.0  # per transition beyond `long_every` without a long blend: a rule, unless no pair offers one
    long_bonus: float = 0.3  # long blends are the default way through a set
    length: float = 4.0  # per minute away from the target, beyond the slack: more than a key clash
    unknown: float = 1.0  # a pair that can't be planned yet


@dataclass(frozen=True)
class Shape:
    """What the planner reads of a track: its analysis in `timeline.seedbox1_analysis`'s shape (None without beats),
    its genre family and energy."""

    hash: str
    duration_s: float
    analysis: dict | None = None
    genre: str | None = None
    energy: float | None = None
    stems: bool = False
    acapella: bool = False

    @property
    def bar_s(self) -> float:
        return float(self.analysis["bar_s"]) if self.analysis else 2.0

    def sections(self, kind: str) -> list[dict]:
        return [s for s in (self.analysis or {}).get("sections") or [] if s.get("kind") == kind]


@dataclass(frozen=True)
class Option:
    style: int  # index into `SetPlanner.styles`
    technique: str
    kind: str
    bars: int
    a_cue: str
    a_out_s: float  # where the transition starts in A
    b_cue: str
    b_in_s: float  # where B starts playing
    fit: float  # what it costs the pair, before the set's rules
    loss: float  # seconds of A after its cue and of B before its cue: what the set doesn't play
    on_drop: bool = False  # a cut onto B's drop

    @property
    def long(self) -> bool:
        return self.technique in LONG_TECHNIQUES or (self.kind in ("blend", "swap") and self.bars >= LONG_BARS)

    def to_json(self) -> dict:
        return {"technique": self.technique, "kind": self.kind, "bars": self.bars, "a_cue": self.a_cue,
                "a_out_s": round(self.a_out_s, 3), "b_cue": self.b_cue, "b_in_s": round(self.b_in_s, 3),
                "long": self.long, "on_drop": self.on_drop}  # fmt: skip


# --- options per pair ------------------------------------------------------------------------------------------------


def _snap(shape: Shape, t: float) -> float:
    """`t` on the track's bar grid (its first downbeat plus whole bars)."""
    an = shape.analysis
    if not an:
        return t
    fd, bar = float(an["first_downbeat_s"]), float(an["bar_s"])
    return round(fd + max(0, round((t - fd) / bar)) * bar, 3)


def a_cues(shape: Shape, overlap_s: float) -> list[tuple[str, float]]:
    """Where a transition of `overlap_s` may start in A, latest first."""
    dur = shape.duration_s
    out: list[tuple[str, float]] = []
    earliest = A_MIN_SHARE * dur
    outro = (shape.analysis or {}).get("outro") or {}
    music_end = float(outro.get("end_s") or dur)
    if outro and float(outro.get("start_s", dur)) < dur - 1.0:
        out.append(("outro", _snap(shape, float(outro["start_s"]))))
    out.append(("end", _snap(shape, music_end - overlap_s)))
    drops = shape.sections("drop")
    if drops:
        out.append(("last drop", _snap(shape, float(drops[-1].get("end_s", drops[-1]["start_s"])))))
        if len(drops) > 1:
            out.append(("first drop", _snap(shape, float(drops[0].get("end_s", drops[0]["start_s"])))))
    seen, kept = set(), []
    for name, t in out:
        if earliest <= t <= dur - min(overlap_s, dur * 0.1) + 1e-6 and t not in seen:
            seen.add(t)
            kept.append((name, t))
    return kept or [("end", max(earliest, _snap(shape, dur - overlap_s)))]


def b_cues(shape: Shape, overlap_s: float, cut: bool) -> list[tuple[str, float, bool]]:
    """Where B may start: `(name, seconds, onto its drop)`."""
    dur = shape.duration_s
    out: list[tuple[str, float, bool]] = [("start", 0.0, False)]
    intro = (shape.analysis or {}).get("intro") or {}
    if not cut and float(intro.get("end_s") or 0.0) - overlap_s > 1.0:
        out.append(("intro", _snap(shape, float(intro["end_s"]) - overlap_s), False))
    drops = shape.sections("drop")
    if drops:
        d = float(drops[0]["start_s"])
        t = d if cut else d - overlap_s
        if t > 1.0:
            out.append(("drop", _snap(shape, t), cut))
    return [c for c in out if c[1] <= B_MAX_SHARE * dur]


def _context(a: Shape, b: Shape) -> style_lib.Context:
    if a.analysis and b.analysis:
        return style_lib.context(a.analysis, b.analysis, genre_a=a.genre, genre_b=b.genre, energy_a=a.energy,
                                 energy_b=b.energy, a_stems=a.stems, b_stems=b.stems, b_acapella=b.acapella)  # fmt: skip
    return style_lib.Context(genre_a=a.genre, genre_b=b.genre, energy_a=a.energy, energy_b=b.energy)


def pair_options(a: Shape, b: Shape, all_styles: Sequence[style_lib.Style], index: Mapping[str, int], w: Weights,
                 key_cost: float = 0.0) -> list[Option]:  # fmt: skip
    """The options for A then B, cheapest first. A pair with a track that has no beats gets one: play both whole."""
    if not (a.analysis and b.analysis):
        return [Option(index[UNKNOWN], UNKNOWN, "blend", 0, "end", a.duration_s, "start", 0.0, w.unknown, 0.0)]
    ctx = _context(a, b)
    ranked = style_lib.rank(ctx, tuple(all_styles))
    if not ranked:  # nothing applies (the generator's own pick would): a plain bass swap
        ranked = [s for s in all_styles if s.technique == "bass swap"]
    best = max(style_lib.score(s, ctx) for s in ranked)
    sings_a, sings_b = ctx.vocals in ("a", "both"), ctx.vocals in ("b", "both")
    has_drops = bool(b.sections("drop"))
    out: list[Option] = []
    for s in ranked:
        cut = s.kind == "cut"
        base = w.style * (best - style_lib.score(s, ctx))
        if s.kind in ("blend", "swap"):
            if sings_a and sings_b and s.technique not in ("chorus swap", "acapella intro"):
                base += w.vocal_clash
            if key_cost >= w.key_clash_at:
                base += w.key_clash
        if "drops" in s.when.needs and not has_drops:
            base += w.no_drop
        own: list[Option] = []
        for bars in ((s.bars,) if s.bars else _BARS.get(s.technique, _KIND_BARS[s.kind])):
            overlap = bars * a.bar_s
            for (an, at), (bn, bt, on_drop) in itertools.product(a_cues(a, overlap), b_cues(b, bars * b.bar_s, cut)):
                fit = base + w.cue_a.get(an, 1.0) + w.cue_b.get(bn, 0.5) - (w.cut_on_drop if on_drop else 0.0)
                own.append(Option(index[s.id], s.technique, s.kind, int(bars), an, at, bn, bt, round(fit, 4),
                                  round(a.duration_s - at + bt, 3), on_drop))  # fmt: skip
        if not own:
            continue
        own.sort(key=lambda o: o.fit)
        keep = {own[0], max(own, key=lambda o: (o.loss, -o.fit)), min(own, key=lambda o: (o.loss, o.fit))}
        out += sorted(keep, key=lambda o: o.fit)[:MAX_PER_STYLE]
    out.sort(key=lambda o: (o.fit, o.loss))
    return out[:MAX_OPTIONS]


# --- the plan's pass over one order -----------------------------------------------------------------------------------


@dataclass
class Arrays:
    """A pair's options as arrays, for the pass."""

    options: list[Option]  # by rule, then cheapest first
    rule: np.ndarray  # the rules' entry per option: style * 2 + long
    fit: np.ndarray
    loss: np.ndarray
    cut: np.ndarray
    on_drop: np.ndarray

    starts: np.ndarray  # where each rule's options start
    rules: np.ndarray  # the rule of each of those runs

    @classmethod
    def of(cls, options: list[Option]) -> Arrays:
        options = sorted(options, key=lambda o: (2 * o.style + int(o.long), o.fit, o.loss))
        rule = np.array([2 * o.style + int(o.long) for o in options], np.int64)
        starts = np.flatnonzero(np.r_[True, rule[1:] != rule[:-1]])
        return cls(options, rule, np.array([o.fit for o in options]), np.array([o.loss for o in options]),
                   np.array([o.kind == "cut" for o in options]), np.array([o.on_drop for o in options]), starts,
                   rule[starts])  # fmt: skip


class Rules:
    """The state machine of the variety rules, as tables: state = (last style, times in a row 1-2, transitions since a
    long blend 0..long_every). `NEXT[rule, state]` is the state after a transition of that rule (style * 2 + whether
    it's a long blend) and `PEN[rule, state]` what it costs (inf: never)."""

    def __init__(self, all_styles: Sequence[tuple[str, str]], long_every: int, w: Weights) -> None:
        self.n_styles = n = len(all_styles)
        self.long_every = c_max = max(1, int(long_every))
        self.n_states = (n + 1) * 2 * (c_max + 1)
        kinds = [k for _, k in all_styles]
        nxt = np.zeros((2 * n, self.n_states), np.int64)
        pen = np.zeros((2 * n, self.n_states))
        for state in range(self.n_states):
            last, run, since = self.decode(state)
            for rule in range(2 * n):
                o, long = divmod(rule, 2)
                sid, kind = all_styles[o]
                same = last == o and sid != UNKNOWN
                new_run = run + 1 if same else 1
                new_since = 0 if long else min(since + 1, c_max)
                cost = 0.0
                if new_run >= 3:
                    cost = math.inf
                elif last < n and all_styles[last][0] != UNKNOWN and kinds[last] == kind and sid != UNKNOWN:
                    cost += w.same_kind * (0.5 if kind == "blend" else 1.0)
                if not long and since + 1 >= c_max:
                    cost += w.overdue * (since + 2 - c_max)
                if long:
                    cost -= w.long_bonus
                nxt[rule, state] = self.encode(o, min(new_run, 2), new_since)
                pen[rule, state] = cost
        self.NEXT, self.PEN = nxt, pen
        self.start = self.encode(n, 1, 0)  # nothing played yet
        # the states a rule can lead to (one style, either run, any count) and, per target, 0 for the states that lead
        # there and inf for the rest: a step is then a masked min, no sort
        groups = 2 * (c_max + 1)
        self.TGT = np.array([[self.encode(r // 2, 1 + g // (c_max + 1), g % (c_max + 1)) for g in range(groups)]
                             for r in range(2 * n)], np.int64)  # fmt: skip
        self.MASK = np.where(nxt[:, None, :] == self.TGT[:, :, None], 0.0, math.inf)


    def encode(self, last: int, run: int, since: int) -> int:
        return (last * 2 + (run - 1)) * (self.long_every + 1) + since

    def decode(self, state: int) -> tuple[int, int, int]:
        rest, since = divmod(state, self.long_every + 1)
        last, run = divmod(rest, 2)
        return last, run + 1, since


class Chain:
    """The pass over one order's transitions: `costs[t]` per option of transition t (fit, the cut's place, the price of
    the seconds it drops); forward tables `alpha[t]` (before transition t) and backward `beta[t]` (after it)."""

    def __init__(self, rules: Rules, pairs: list[Arrays], extra: list[np.ndarray]) -> None:
        self.rules, self.pairs, self.extra = rules, pairs, extra
        self.alpha = [self._start()]
        for t, pair in enumerate(pairs):
            self.alpha.append(step(rules, self.alpha[-1], pair, extra[t])[0])
        self.beta = [np.zeros(rules.n_states)]
        for t in range(len(pairs) - 1, -1, -1):
            self.beta.append(back_step(rules, self.beta[-1], pairs[t], extra[t]))
        self.beta.reverse()  # beta[t]: the cheapest from the state before transition t to the end; beta[T] = 0

    def _start(self) -> np.ndarray:
        a = np.full(self.rules.n_states, math.inf)
        a[self.rules.start] = 0.0
        return a

    @property
    def cost(self) -> float:
        return float(self.alpha[-1].min()) if self.pairs else 0.0

    def window(self, t0: int, pairs: list[Arrays], extra: list[np.ndarray]) -> float:
        """The whole pass's cost with transitions t0 .. t0+len-1 replaced."""
        a = self.alpha[t0]
        for pair, e in zip(pairs, extra, strict=True):
            a = step(self.rules, a, pair, e)[0]
        return float((a + self.beta[t0 + len(pairs)]).min())


def step(rules: Rules, alpha: np.ndarray, pair: Arrays, extra: np.ndarray,
         back: bool = False) -> tuple[np.ndarray, np.ndarray | None]:  # fmt: skip
    """One transition forward: the new table, and with `back` for each new state (option, state before)."""
    c = pair.fit + extra
    cmin = np.minimum.reduceat(c, pair.starts)  # options of one rule differ only in what they cost
    v = alpha[None, :] + rules.PEN[pair.rules] + cmin[:, None]  # rules x states
    to = v[:, None, :] + rules.MASK[pair.rules]  # rules x targets x states
    vals = to.min(axis=2)
    tgt = rules.TGT[pair.rules]
    out = np.full(rules.n_states, math.inf)
    np.minimum.at(out, tgt.ravel(), vals.ravel())  # a style's long and other rules share targets
    if not back:
        return out, None
    came = np.zeros((rules.n_states, 2), np.int64)
    ends = np.r_[pair.starts[1:], c.size]
    best_option = np.array([int(a + np.argmin(c[a:b])) for a, b in zip(pair.starts, ends, strict=True)])
    won = (vals <= out[tgt]) & np.isfinite(vals)
    rows, cols = np.nonzero(won)
    came[tgt[rows, cols], 0] = best_option[rows]
    came[tgt[rows, cols], 1] = to.argmin(axis=2)[rows, cols]
    return out, came


def back_step(rules: Rules, beta: np.ndarray, pair: Arrays, extra: np.ndarray) -> np.ndarray:
    cmin = np.minimum.reduceat(pair.fit + extra, pair.starts)
    cand = rules.PEN[pair.rules] + cmin[:, None] + beta[rules.NEXT[pair.rules]]
    return cand.min(axis=0)


# --- the planner -----------------------------------------------------------------------------------------------------


@dataclass
class Transition:
    from_hash: str
    to_hash: str
    option: Option

    def to_json(self) -> dict:
        return {"from": self.from_hash, "to": self.to_hash, **self.option.to_json()}


@dataclass
class SetPlan:
    order: list[int]
    transitions: list[Transition]
    plays: list[tuple[float, float]]  # per position: from and to, seconds in the track
    seconds: float
    target_s: float | None
    order_cost: float
    plan_cost: float
    found_by: str = "as it stands"  # which start won: "as it stands", "solver" (#60's order) or "lookahead"

    @property
    def cost(self) -> float:
        return self.order_cost + self.plan_cost

    def to_json(self, hashes: Sequence[str]) -> dict:
        return {
            "order": [hashes[i] for i in self.order], "seconds": round(self.seconds, 1),
            "target_s": self.target_s, "cost": round(self.cost, 3), "order_cost": round(self.order_cost, 3),
            "plan_cost": round(self.plan_cost, 3), "found_by": self.found_by, "transitions": [t.to_json() for t in self.transitions],
            "plays": [{"hash": hashes[i], "from_s": round(a, 3), "to_s": round(b, 3)}
                      for i, (a, b) in zip(self.order, self.plays, strict=True)],
        }  # fmt: skip


class SetPlanner:
    """Plans one set's tracks (#60's `Problem` and a `Shape` per track). Keeps each pair's options between calls."""

    def __init__(self, problem: Problem, shapes: Sequence[Shape], *, target_s: float | None = None,
                 long_every: int = LONG_EVERY, weights: Weights | None = None,
                 all_styles: Sequence[style_lib.Style] | None = None) -> None:  # fmt: skip
        if len(shapes) != problem.n:
            raise ValueError("one shape per track")
        self.p, self.shapes, self.target_s = problem, list(shapes), target_s
        self.w = weights or Weights()
        self.styles = list(all_styles if all_styles is not None else style_lib.all_styles())
        self.style_ids = [s.id for s in self.styles] + [UNKNOWN]
        self.index = {sid: i for i, sid in enumerate(self.style_ids)}
        self.rules = Rules([(s.id, s.kind) for s in self.styles] + [(UNKNOWN, "blend")], long_every, self.w)
        self._options: dict[tuple[int, int], Arrays] = {}
        self.shifts = np.zeros(problem.n, np.int64)
        self._keys = problem.key_matrix()
        peak = 0.5 * problem.levels  # B's own energy, half of what makes a peak; the arc's level at its place is the other
        self._peak_b = peak
        self.long_every = self.rules.long_every

    # options --------------------------------------------------------------------------------------------------------

    def options(self, i: int, j: int) -> Arrays:
        key = (i, j)
        got = self._options.get(key)
        if got is None:
            opts = pair_options(self.shapes[i], self.shapes[j], self.styles, self.index, self.w, float(self._keys[i, j]))
            got = self._options[key] = Arrays.of(opts)
        return got

    def _extra(self, pair: Arrays, position: int, j: int, lam: float) -> np.ndarray:
        """Per option of the transition into position `position` (track j): the cut's place and the seconds' price."""
        level = 0.5 * float(self.p.arc_levels[position]) + float(self._peak_b[j])  # 0 quiet .. 1 peak
        cut = np.where(pair.cut, self.w.cut_peak * (1.0 - 2.0 * level), 0.0)
        return cut - lam * pair.loss

    def chain(self, order: Sequence[int], lam: float) -> Chain:
        pairs = [self.options(a, b) for a, b in itertools.pairwise(order)]
        extra = [self._extra(pr, k + 1, order[k + 1], lam) for k, pr in enumerate(pairs)]
        return Chain(self.rules, pairs, extra)

    def _length(self, order: Sequence[int], picked: list[Option]) -> float:
        return float(sum(self.shapes[i].duration_s for i in order) - sum(o.loss for o in picked))

    def _length_cost(self, seconds: float) -> float:
        if self.target_s is None:
            return 0.0
        return self.w.length * max(0.0, abs(seconds - self.target_s) - LENGTH_SLACK_S) / 60.0

    def _true(self, order: Sequence[int], lam: float) -> tuple[float, list[Option]]:
        """The plan's own cost for this order at this price of a second (without the price) and its options."""
        if len(order) < 2:
            return 0.0, []
        a = np.full(self.rules.n_states, math.inf)
        a[self.rules.start] = 0.0
        pairs, choices = [], []
        for k in range(len(order) - 1):
            pair = self.options(order[k], order[k + 1])
            a, came = step(self.rules, a, pair, self._extra(pair, k + 1, order[k + 1], lam), back=True)
            pairs.append(pair)
            choices.append(came)
        state = int(np.argmin(a))
        cost = float(a[state])
        picked: list[Option] = []
        for pair, came in zip(reversed(pairs), reversed(choices), strict=True):
            o, state = came[state]
            picked.append(pair.options[o])
        picked.reverse()
        loss = sum(o.loss for o in picked)
        return cost + lam * loss + self._length_cost(self._length(order, picked)), picked

    def best_plan(self, order: Sequence[int]) -> tuple[float, list[Option], float]:
        """(the plan's cost, its options, the price per second) for this order, with the set's length on target."""
        cost0, picked0 = self._true(order, 0.0)
        if self.target_s is None or len(order) < 2:
            return cost0, picked0, 0.0
        best = (cost0, picked0, 0.0)
        long0 = self._length(order, picked0)
        if abs(long0 - self.target_s) <= LENGTH_SLACK_S:
            return best
        # too long: a positive price drops more; too short: a negative one keeps more. The price climbs a ladder until
        # the length passes the target (or nothing moves any more), then bisects where it crossed; whichever price does
        # best on the true cost wins, so a target out of reach ends where the plan stops paying for the next minute.
        sign = 1.0 if long0 > self.target_s else -1.0
        prev, crossed = 0.0, None
        for k in range(LAMBDA_LADDER):
            lam = sign * LAMBDA_FIRST * 2.0**k
            cost, picked = self._true(order, lam)
            if cost < best[0] - 1e-9:
                best = (cost, picked, lam)
            seconds = self._length(order, picked)
            if (seconds - self.target_s) * sign <= 0:
                crossed = (prev, lam)
                break
            prev = lam
        if crossed is not None:
            lo, hi = crossed
            for _ in range(LAMBDA_STEPS):
                lam = (lo + hi) / 2
                cost, picked = self._true(order, lam)
                if cost < best[0] - 1e-9:
                    best = (cost, picked, lam)
                seconds = self._length(order, picked)
                if abs(seconds - self.target_s) <= LENGTH_SLACK_S / 4:
                    break
                if (seconds - self.target_s) * sign > 0:
                    lo = lam
                else:
                    hi = lam
        return best

    # the order ------------------------------------------------------------------------------------------------------

    def _order_cost(self, order: Sequence[int]) -> float:
        """#60's cost of the order, at its own best semitone shifts."""
        return self.p.cost(list(order)) if self.p.n else 0.0

    def _append_cost(self, step: np.ndarray, prefix: Sequence[int], j: int) -> float:
        """What putting j after `prefix` adds to #60's cost (with the shifts held)."""
        p, k = self.p, len(prefix)
        c = float(p.slot[j, k])
        if k:
            i = prefix[-1]
            c += float(step[i, j])
            if p.has_energy[i] and p.has_energy[j]:
                c += p.w.energy_step * abs(float(p.levels[j] - p.levels[i] - p.arc_steps[k - 1]))
            if k >= 2:
                c += p.w.vocal_run * float(p.heavy[prefix[-2]] * p.heavy[i] * p.heavy[j])
        return c

    def lookahead_order(self, locks: Mapping[int, int], lam: float, guide: Sequence[int], depth: int = LOOKAHEAD,
                        branch: int = BRANCH) -> list[int]:  # fmt: skip
        """Fills the order a position at a time, as DJ-MC does: each next track tried is followed by every continuation
        of `depth` tracks (the `branch` cheapest next tracks at each level), and each of those by the rest of `guide`
        (#60's order) as the rollout. The next track is the one whose best continuation costs least: #60's cost of the
        whole rolled-out order plus the plan's pass over the transitions placed so far. The first unplayed track of the
        guide is always among those tried, and first, so a tie follows the guide."""
        n = self.p.n
        step_m = self.p.step_matrix(self.shifts)
        pinned = set(locks.values())
        free_guide = [j for j in guide if j not in pinned]

        def allowed(k: int, used: set[int]) -> list[int]:
            if k in locks:
                return [locks[k]]
            return [j for j in range(n) if j not in used and j not in pinned]

        def cheapest(prefix: list[int], used: set[int], k: int) -> list[int]:
            cands = allowed(k, used)
            if len(cands) <= branch:
                return cands
            i = prefix[-1] if prefix else None
            score = [float(self.p.slot[j, k]) + (float(step_m[i, j]) if i is not None else 0.0) for j in cands]
            out = [cands[x] for x in np.argsort(score, kind="stable")[:branch]]
            g = next(j for j in free_guide if j not in used)
            return [g, *(j for j in out if j != g)][:branch]

        def rollout(prefix: list[int], used: set[int]) -> list[int]:
            rest = iter(j for j in free_guide if j not in used)
            return prefix + [locks[q] if q in locks else next(rest) for q in range(len(prefix), n)]

        def explore(prefix: list[int], used: set[int], alpha: np.ndarray, left: int, first: int,
                    leaves: list[tuple[int, list[int], float]]) -> None:  # fmt: skip
            k = len(prefix)
            if left == 0 or k == n:
                leaves.append((first, rollout(prefix, used), float(alpha.min())))
                return
            for j in cheapest(prefix, used, k):
                a = alpha
                if prefix:
                    pair = self.options(prefix[-1], j)
                    a = step(self.rules, alpha, pair, self._extra(pair, k, j, lam))[0]
                prefix.append(j)
                used.add(j)
                explore(prefix, used, a, left - 1, j if first < 0 else first, leaves)
                prefix.pop()
                used.discard(j)

        prefix: list[int] = []
        used: set[int] = set()
        alpha = np.full(self.rules.n_states, math.inf)
        alpha[self.rules.start] = 0.0
        while len(prefix) < n:
            leaves: list[tuple[int, list[int], float]] = []
            explore(prefix, used, alpha, depth, -1, leaves)
            values = self.p.batch_cost(np.array([o for _, o, _ in leaves], np.int64), step_m) + [v for *_, v in leaves]
            j = leaves[int(np.argmin(values))][0]
            if prefix:
                pair = self.options(prefix[-1], j)
                alpha = step(self.rules, alpha, pair, self._extra(pair, len(prefix), j, lam))[0]
            prefix.append(j)
            used.add(j)
        return prefix

    def polish(self, order: list[int], locks: Mapping[int, int], lam: float, size: int = LOOKAHEAD) -> list[int]:
        """Every window of `size` positions tried in every order of its free tracks, while that helps: #60's cost of
        the whole order plus the plan's pass, of which only the window's transitions are run again."""
        n = len(order)
        if n < 3:
            return order
        step_m = self.p.step_matrix(self.shifts)
        order = list(order)
        for _ in range(POLISH_ROUNDS):
            improved = False
            chain = self.chain(order, lam)
            base = self.p.batch_cost(np.array([order]), step_m)[0] + chain.cost
            for s in range(n - 1):
                free = [q for q in range(s, min(s + size, n)) if q not in locks]
                if len(free) < 2:
                    continue
                items = [order[q] for q in free]
                perms = [pm for pm in itertools.permutations(items) if list(pm) != items]
                if not perms:
                    continue
                cands = np.array([order] * len(perms))
                for r, pm in enumerate(perms):
                    cands[r, free] = pm
                order_costs = self.p.batch_cost(cands, step_m)
                lo, hi = max(0, s - 1), min(n - 1, free[-1] + 1)  # the transitions that change: lo .. hi-1
                best_r, best_v = -1, base - 1e-9
                for r in range(len(perms)):
                    o = cands[r]
                    pairs = [self.options(int(o[t]), int(o[t + 1])) for t in range(lo, hi)]
                    extra = [self._extra(pr, t + 1, int(o[t + 1]), lam) for pr, t in zip(pairs, range(lo, hi), strict=True)]
                    v = order_costs[r] + chain.window(lo, pairs, extra)
                    if v < best_v:
                        best_r, best_v = int(r), v
                if best_r >= 0:
                    order = cands[best_r].tolist()
                    chain = self.chain(order, lam)
                    base = best_v
                    improved = True
            if not improved:
                break
        return order

    # answers --------------------------------------------------------------------------------------------------------

    def _result(self, order: list[int]) -> SetPlan:
        plan_cost, picked, _ = self.best_plan(order)
        n = len(order)
        plays = []
        for k, i in enumerate(order):
            start = picked[k - 1].b_in_s if k > 0 else 0.0
            end = picked[k].a_out_s + picked[k].bars * self.shapes[i].bar_s if k < n - 1 else self.shapes[i].duration_s
            plays.append((start, min(end, self.shapes[i].duration_s)))
        transitions = [Transition(self.shapes[order[k]].hash, self.shapes[order[k + 1]].hash, o)
                       for k, o in enumerate(picked)]  # fmt: skip
        return SetPlan(order, transitions, plays, self._length(order, picked), self.target_s,
                       self._order_cost(order), plan_cost)  # fmt: skip

    def plan_order(self, order: Sequence[int]) -> SetPlan:
        """The best plan for this order as it is: what a drag in the timeline asks for. Pairs met before are cached."""
        order = list(order)
        self.shifts = self.p.best_shifts(order) if self.p.n else self.shifts
        return self._result(order)

    def solve(self, locks: Mapping[int, int] | None = None, *, depth: int = LOOKAHEAD,
              start: Sequence[int] | None = None) -> SetPlan:  # fmt: skip
        """The order and plan found together. `start` is the order as it stands (default: as given); #60's best order
        is the other start, and the result is never worse than either with its best plan."""
        n = self.p.n
        pins = check_locks(n, locks)
        if n < 2:
            return self.plan_order(list(range(n)))
        base = solve(self.p, pins)
        self.shifts = base.shifts
        starts = {"solver": list(base.order)}
        current = list(start) if start is not None else list(range(n))
        if all(current[q] == i for q, i in pins.items()):
            starts = {"as it stands": current, **starts}  # first, so a tie keeps it
        _, _, lam = self.best_plan(starts["solver"])
        starts["lookahead"] = self.lookahead_order(pins, lam, starts["solver"], depth)
        scored = sorted(((self._order_cost(o) + self.best_plan(o)[0], q, name) for q, (name, o) in enumerate(starts.items())))
        best = (scored[0][0], starts[scored[0][2]], scored[0][2])
        for _, _, name in scored[:POLISHED]:  # the cheapest starts are polished
            polished = self.polish(starts[name], pins, lam, min(depth, MAX_WINDOW))
            total = self._order_cost(polished) + self.best_plan(polished)[0]
            if total < best[0] - 1e-9:
                best = (total, polished, name)
        out = self._result(best[1])
        out.found_by = best[2]
        self.shifts = self.p.best_shifts(best[1])
        return out


# --- from the library ------------------------------------------------------------------------------------------------


def load_shapes(lib: Any, hashes: Sequence[str], energies: Mapping[str, float | None] | None = None) -> list[Shape]:
    """A `Shape` per hash, from the library's analyses (beats, energy, sections, vocals, genre, stems)."""
    from seedbox2.analysis import stems as stem_cache
    from seedbox2.analysis import vocals
    from seedbox2.mix import genre
    from seedbox2.sets.timeline import seedbox1_analysis

    out: dict[str, Shape] = {}
    for h in hashes:
        if h in out:
            continue
        row = lib.track(h) or {}
        duration = float(row.get("duration") or 0.0)
        an = seedbox1_analysis(lib, h, duration) if duration > 0 else None
        if an is not None:
            an = vocals.plan_input_for(lib, h, an)
        out[h] = Shape(h, duration, an, genre=genre.effective(lib, h)["genre"],
                       energy=(energies or {}).get(h), stems=stem_cache.current(lib, h) is not None)  # fmt: skip
    return [out[h] for h in hashes]


_CACHE: dict[tuple, tuple[SetPlanner, list[str]]] = {}
_CACHE_SIZE = 8


def planner(lib: Any, hashes: Sequence[str], request: Any = None, *, target_s: float | None = None,
            long_every: int = LONG_EVERY) -> tuple[SetPlanner, list[str]]:  # fmt: skip
    """A planner for these tracks in any order, and the hashes its items stand for. The same tracks and settings get
    the same planner back (the last few are kept), so planning again after a drag only meets the new pairs. A change
    to a track's analysis is seen once the planner drops out of the cache, or with other settings."""
    from seedbox2.sets.service import OrderRequest, _problem, _tracks_of

    request = request or OrderRequest()
    items = sorted(hashes)
    settings = request.settings()
    key = (tuple(items), repr(settings.get("arc")), settings.get("tempo_start"), settings.get("tempo_end"), target_s,
           long_every)  # fmt: skip
    got = _CACHE.pop(key, None)
    if got is None:
        tracks = _tracks_of(lib, items)
        shapes = load_shapes(lib, items, {t.hash: t.energy for t in tracks})
        got = (SetPlanner(_problem(tracks, request), shapes, target_s=target_s, long_every=long_every), items)
    _CACHE[key] = got
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.pop(next(iter(_CACHE)))
    return got


def as_items(items: Sequence[str], hashes: Sequence[str]) -> list[int]:
    """The running order `hashes` as indices into `items` (a track in the set twice takes each of its copies once)."""
    free: dict[str, list[int]] = {}
    for i, h in enumerate(items):
        free.setdefault(h, []).append(i)
    return [free[h].pop(0) for h in hashes]


def plan_hashes(lib: Any, hashes: Sequence[str], request: Any = None, *, target_s: float | None = None,
                long_every: int = LONG_EVERY, reorder: bool = False, lookahead: int = LOOKAHEAD) -> dict:  # fmt: skip
    """The set's transitions planned together for the running order `hashes`; with `reorder` the order too (from
    `hashes`, #60's best and the lookahead, keeping `request`'s pins)."""
    import time

    from seedbox2.sets.service import _locks

    t0 = time.perf_counter()
    pl, items = planner(lib, hashes, request, target_s=target_s, long_every=long_every)
    order = as_items(items, hashes)
    if reorder:
        locks = {q: order[i] for q, i in _locks(list(hashes), request.pins if request else []).items()}
        result = pl.solve(locks, depth=lookahead, start=order)
    else:
        result = pl.plan_order(order)
    out = result.to_json(items)
    out["elapsed_s"] = round(time.perf_counter() - t0, 3)
    return out


def picks(lib: Any, hashes: Sequence[str], request: Any = None) -> dict[tuple[str, str], dict]:
    """Each transition's pick in the set's plan for this order, as a `set_plan` job's `pick`: style, bars and cues."""
    pl, items = planner(lib, hashes, request)
    result = pl.plan_order(as_items(items, hashes))
    return {(t.from_hash, t.to_hash): {"style": pl.style_ids[t.option.style], **t.option.to_json()}
            for t in result.transitions if t.option.technique != UNKNOWN}  # fmt: skip
