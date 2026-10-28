"""Set-level transition planning: one planner technique per consecutive pair, chosen across the whole set.

mixplan.generate on its own sees one pair at a time, so a set of similar tracks gets the same transition again and
again. plan_set gives every pair a few techniques that work for it (the pair's own pick costs nothing, alternatives
cost what they give up) and picks one per pair with a dynamic program over (previous technique, transitions since
the last cut or echo out), adding set costs: repeating a cut or effect, a run of blends that drags on without a slam,
cut or echo out, cuts in the quiet stretches (and cheaper ones at the energy peaks). Long blends stay the default.

The picks depend only on the running order, the tracks and their analyses, never on saved plan edits: regenerating
or editing one transition leaves every other pair's pick as it was.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import mixing, mixplan
from .models import Track

BLENDS = ("long blend", "phrase blend", "bass swap")
CUTS = ("slam", "quick cut", "drop swap", "reverb wash")  # B in on the 1, A out under an effect or not at all
BREAKERS = (*CUTS, "echo out")  # what breaks up a run of blends
# the effect a technique leans on: two in a row with the same one sound like a repeat even when the names differ
FAMILY = {
    "slam": "cut", "quick cut": "cut", "drop swap": "cut", "reverb wash": "reverb", "echo out": "echo",
    "filter sweep": "filter", "loop roll": "loop",
}  # fmt: skip

# what an alternative to the pair's own pick costs (it gives up some of the pair's fit); the pick itself costs 0
_ALT_COST = {
    "long blend": 1.0, "phrase blend": 3.0, "bass swap": 3.0, "echo out": 3.0, "filter sweep": 4.0, "slam": 2.0,
    "drop swap": 4.0, "reverb wash": 5.0, "quick cut": 5.0,
}  # fmt: skip
_KEY_BLEND_COST = 6.0  # a plain blend over keys that don't sit together (a filter sweep or echo out hides them)
_REPEAT = 25.0  # the same cut or effect technique twice in a row: never, unless nothing else works
_REPEAT_BLEND = {"long blend": 0.5, "phrase blend": 1.5, "bass swap": 1.5}  # blends may run on, long ones most
_SAME_FAMILY = 5.0  # e.g. a quick cut right after a slam
_LONG_BONUS = 1.0  # long blends are the default way through a set
_PACE_MIN = 1  # a breaker right after another one (fewer transitions in between than this) costs _TOO_SOON
_TOO_SOON = 5.0
_PACE_EVERY = 3  # this many transitions in a row without a breaker are fine; each one more is overdue ...
_PACE_STEP = 2.5  # ... and costs this much more than the one before
_PACE_CAP = 12  # transitions counted since the last breaker
_PEAK_CUT = 4.0  # cuts get this much cheaper at the set's energy peak, dearer in its quietest stretch
_START_SINCE = 0  # the set's start counts as a breaker


@dataclass(frozen=True)
class Pick:
    """The set's technique for one pair; technique None when the pair isn't analysed (no planner pick yet)."""

    from_id: str
    to_id: str
    technique: str | None
    own: str | None  # the pair's own pick (mixplan.generate without a technique)
    reason: str | None  # why the set picked something else, for the notes


def _candidates(m: mixplan.Mix) -> tuple[str, dict[str, float]]:
    """(the pair's own technique, {technique: fit cost}) for one analysed pair."""
    e_tech, e = mixplan.engine_place(m)
    own = mixplan.auto_technique(m, e_tech, e)
    out = {own: 0.0}
    if not (m.real_a and m.real_b):
        return own, out
    gap = abs(m.b_bpm / m.a_bpm - 1)
    good_key = m.move in mixplan.GOOD_KEYS
    blendable = e.overlap > 0 and e_tech not in mixplan.CUTS and gap <= mixplan.MAX_BLEND_GAP
    alts: list[str] = ["echo out"]  # a blend then echo out, or (no blend) echo A out and B in on the 1
    if blendable:
        if e.overlap >= 16 and good_key and gap <= mixplan.SMALL_GAP:
            alts.append("long blend")
        if good_key:
            alts += ["phrase blend", "bass swap"]
        alts.append("filter sweep")
    if mixplan.b_slam_drop(m) is not None or mixplan.a_build_end(m) is not None:
        alts.append("slam")
    a_drops = [d for d in m.ga.drops if m.ga.end * 0.4 <= d <= m.ga.end - mixplan._PHRASE]
    if a_drops and any(d > m.gb.intro[0] for d in m.gb.drops):
        alts.append("drop swap")
    alts += ["reverb wash", "quick cut"]
    for tech in alts:
        cost = _ALT_COST[tech] + (_KEY_BLEND_COST if tech in BLENDS and not good_key else 0.0)
        out.setdefault(tech, cost)
    return own, out


def _step(prev: str | None, since: int, tech: str | None, energy: float) -> tuple[float, int]:
    """Set cost of playing `tech` after `prev` with `since` transitions since the last breaker, and the new since.
    energy: the pair's place in the set's energy range, 0 (quietest) to 1 (peak)."""
    if tech is None:
        return 0.0, min(_PACE_CAP, since + 1)
    if tech in BREAKERS:
        cost = _TOO_SOON if since < _PACE_MIN else 0.0
    else:
        cost = _PACE_STEP * max(0, since + 1 - _PACE_EVERY)
    if tech == prev:
        cost += _REPEAT_BLEND.get(tech, _REPEAT)
    elif tech in FAMILY and FAMILY.get(prev) == FAMILY[tech]:
        cost += _SAME_FAMILY
    if tech == "long blend":
        cost -= _LONG_BONUS
    if tech in CUTS:
        cost += _PEAK_CUT * (1 - 2 * energy)
    return cost, 0 if tech in BREAKERS else min(_PACE_CAP, since + 1)


def _energies(tracks: list[Track]) -> list[float]:
    """Per pair: the mean energy of its two tracks, scaled to the set's range (0.5 everywhere for a flat set)."""
    levels = mixing.energy_levels(tracks)
    raw = [levels.get(t.id, 5.0) for t in tracks]
    lo, hi = min(raw, default=0.0), max(raw, default=0.0)
    norm = [(v - lo) / (hi - lo) if hi - lo > 1e-9 else 0.5 for v in raw]
    return [(x + y) / 2 for x, y in zip(norm, norm[1:])]


def choose(options: list[dict[str | None, float]], energies: list[float]) -> list[str | None]:
    """The cheapest technique per pair: the options' fit costs plus _step's set costs, summed over the set.
    Ties keep the option listed first."""
    State = tuple[str | None, int]  # (previous technique, transitions since the last breaker)
    states: dict[State, float] = {(None, _START_SINCE): 0.0}
    back: list[dict[State, State]] = []  # per pair: state reached -> the state it came from
    for opts, energy in zip(options, energies):
        nxt: dict[State, float] = {}
        came: dict[State, State] = {}
        for (prev, since), total in states.items():
            for tech, fit in opts.items():
                cost, since2 = _step(prev, since, tech, energy)
                key, val = (tech, since2), total + fit + cost
                if key not in nxt or val < nxt[key] - 1e-9:
                    nxt[key], came[key] = val, (prev, since)
        states = nxt
        back.append(came)
    if not back:
        return []
    key = min(states, key=lambda k: states[k])
    picks: list[str | None] = []
    for came in reversed(back):
        picks.append(key[0])
        key = came[key]
    return picks[::-1]


def _reason(tech: str, own: str, prev: str | None, since: int, energy: float) -> str:
    if tech in CUTS and energy >= 2 / 3:
        why = "at an energy peak of the set"
    elif prev == own and own not in BLENDS:
        why = f"so the set doesn't play two {own}s in a row"
    elif tech in BREAKERS and since >= 2:
        why = f"to break up a run of {since} blends"
    else:
        why = "for variety across the set"
    return f"Set plan: {tech} here instead of this pair's own {own}, {why}."


def plan_set(
    tracks: list[Track],
    analyses: dict[str, dict | None],
    transitions: list[dict] | None = None,
    mix_length: str = "normal",
) -> list[Pick]:
    """One Pick per consecutive pair tracks[i] → tracks[i+1]. `transitions`: transitions.suggest_set's output for
    the same tracks (reused, else each pair is suggested again)."""
    options: list[dict[str | None, float]] = []
    owns: list[str | None] = []
    for i, (a, b) in enumerate(zip(tracks, tracks[1:])):
        a_an, b_an = analyses.get(a.id), analyses.get(b.id)
        if a_an is None or b_an is None:
            options.append({None: 0.0})
            owns.append(None)
            continue
        tr = transitions[i] if transitions is not None and i < len(transitions) else None
        own, opts = _candidates(mixplan.make_mix(a, a_an, b, b_an, tr, mix_length))
        options.append(dict(opts))
        owns.append(own)
    energies = _energies(tracks)
    picks = choose(options, energies)

    out: list[Pick] = []
    prev, since = None, _START_SINCE
    for a, b, tech, own, energy in zip(tracks, tracks[1:], picks, owns, energies):
        reason = None if tech is None or own is None or tech == own else _reason(tech, own, prev, since, energy)
        out.append(Pick(a.id, b.id, tech, own, reason))
        since = _step(prev, since, tech, energy)[1]
        prev = tech
    return out


def annotate(
    tracks: list[Track],
    analyses: dict[str, dict | None],
    transitions: list[dict],
    mix_length: str = "normal",
) -> list[dict]:
    """suggest_set's transitions with the set plan added: plan_technique (the planner technique the set uses for
    the pair, None until both are analysed), plan_bars (its blend length, 0 for a cut) and a note when the set
    plays something else than the pair's own pick."""
    out = []
    for tr, pick, a, b in zip(transitions, plan_set(tracks, analyses, transitions, mix_length), tracks, tracks[1:]):
        tr = {**tr, "plan_technique": pick.technique, "plan_bars": None}
        if pick.technique is not None:
            try:
                plan = mixplan.generate(a, analyses[a.id], b, analyses[b.id], "", technique=pick.technique,
                                        transition=tr, mix_length=mix_length)  # fmt: skip
            except ValueError:
                plan = None
            if plan is not None:
                tr["plan_technique"] = plan["technique"]
                tr["plan_bars"] = overlap_bars(plan)
        if pick.reason:
            tr["notes"] = [*tr.get("notes", []), pick.reason]
        out.append(tr)
    return out


def overlap_bars(plan: dict) -> int:
    """Bars both decks play together in a MixPlan: where A's fader is first closed, 0 for a cut on the 1."""
    vol = (plan["a"].get("lanes") or {}).get("volume") or []
    end = next((k["beat"] for k in vol if k["beat"] >= 0 and k["value"] <= 1e-6), plan["a"]["play_to_beat"])
    return max(0, round(end / 4))
