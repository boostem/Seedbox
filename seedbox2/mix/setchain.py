"""A set as the engines play it: its plans, each stretched over its tracks, on one tempo line. `planChain` in
`frontend2/src/features/nowplaying/session.ts` is the same thing for the live engine, and `tests/test_seedbox2_setchain.py`
holds the two to one fixture. Change both together.

A plan's window covers its transition. `extend_plan` stretches it: the first plan over its A from the top, the last over
its B to the end, and each one from early enough to pick up the track the plan before hands it, `HANDOFF_BEATS` before
that plan's window ends. Outside its keyframes a lane holds, and a grid's outer segments hold too.

The tempo line (`tempo_line`): generated plans drift from A's own tempo to where the two meet, and on to B's own tempo. A
set would go back to each track's own tempo between two transitions, even when both of them meet well away from it. So
between one blend's end and the next one's start, the tempo goes in a straight line over the track's file time. Being on
the file's time, both plans read the same tempo at the same place in the track, so nothing moves when one takes over. Only
the tempo lane changes: which beat is where in a file doesn't depend on it. An edited plan keeps every keyframe in its
window: the line runs up to the window's edge there.
"""

from __future__ import annotations

from .plan import Keyframe, MixPlan, Window, deck_grid, deck_time, lane_value

HANDOFF_BEATS = 8.0  # the next plan takes over this many beats before the end of a plan's window
ENTRY_MARGIN_S = 10.0  # a plan's window starts this long before where it picks its track up


def extend_plan(plan: MixPlan, from_s: float | None, to_s: float | None) -> MixPlan:
    """The plan stretched: its window starts where A comes in and ends where B ends. None leaves that end as it is."""
    start, end = plan.window.start_beat, plan.window.end_beat
    a, b = plan.a, plan.b
    if from_s is not None:
        start = min(start, float(deck_grid(a).beat_at(from_s)))
        a = a.model_copy(update={"play_from_beat": min(a.play_from_beat, start)})
    if to_s is not None:
        end = max(end, float(deck_grid(b).beat_at(to_s)))
        b = b.model_copy(update={"play_to_beat": max(b.play_to_beat, end)})
    return plan.model_copy(update={"window": Window(start_beat=start, end_beat=end), "a": a, "b": b})


def blend_span(plan: MixPlan) -> tuple[float, float]:
    """Where both decks are heard. For a cut, the lead-in of `bars` before it."""
    start, end = plan.b.play_from_beat, plan.a.play_to_beat
    return (start, end) if end > start else (end - plan.bars * 4, end)


def handoff_beat(plan: MixPlan) -> float:
    return plan.window.end_beat - HANDOFF_BEATS


def enter_beat(plan: MixPlan, before: MixPlan) -> float:
    """Where on `plan`'s timeline the track is when the plan before hands it over."""
    return float(deck_grid(plan.a).beat_at(float(deck_time(before.b, handoff_beat(before)))))


def _line_out(stored: MixPlan) -> float:
    return handoff_beat(stored) if stored.edited else blend_span(stored)[1]


def _line_in(stored: MixPlan) -> float:
    return stored.window.start_beat if stored.edited else blend_span(stored)[0]


def _curve_at(lane: list[Keyframe], beat: float) -> str:
    """The curve the lane is on at this beat: a line ending here hands over to it."""
    on = [k for k in lane if k.beat <= beat]
    return on[-1].curve if on else "linear"


def tempo_line(chain: list[MixPlan], plans: list[MixPlan]) -> list[MixPlan]:
    """`chain` (the plans stretched) with a straight tempo line over the body of every track between two plans. `plans`
    are the same plans as stored. A track whose two transitions overlap has no body to draw it on: it stays as planned."""
    tempo = [list(p.tempo) for p in chain]
    for i, (p, q) in enumerate(zip(chain, chain[1:])):
        out, into = _line_out(plans[i]), _line_in(plans[i + 1])
        x0, x1 = float(deck_time(p.b, out)), float(deck_time(q.a, into))
        if x1 <= x0:
            continue
        v0, v1 = float(lane_value(p.tempo, out)), float(lane_value(q.tempo, into))

        def line(x: float, x0: float = x0, x1: float = x1, v0: float = v0, v1: float = v1) -> float:
            return v0 + (v1 - v0) * min(max((x - x0) / (x1 - x0), 0.0), 1.0)

        end = max(p.window.end_beat, out)
        tempo[i] = [k for k in tempo[i] if k.beat <= out] + [
            Keyframe(beat=out, value=v0, curve="linear"),
            Keyframe(beat=end, value=line(float(deck_time(p.b, end))), curve="linear"),
        ]
        start = min(q.window.start_beat, into)
        tempo[i + 1] = [
            Keyframe(beat=start, value=line(float(deck_time(q.a, start))), curve="linear"),
            Keyframe(beat=into, value=v1, curve=_curve_at(q.tempo, into)),
        ] + [k for k in tempo[i + 1] if k.beat >= into]
    return [p.model_copy(update={"tempo": t}) for p, t in zip(chain, tempo)]


def plan_chain(plans: list[MixPlan], last_duration_s: float) -> list[MixPlan]:
    """Each neighbouring pair's plan, in set order, as the set plays them. `last_duration_s` is the last track's length."""
    chain: list[MixPlan] = []
    for i, plan in enumerate(plans):
        before = chain[-1] if chain else None
        from_s = max(0.0, float(deck_time(before.b, handoff_beat(before))) - ENTRY_MARGIN_S) if before else 0.0
        chain.append(extend_plan(plan, from_s, last_duration_s if i == len(plans) - 1 else None))
    return tempo_line(chain, plans)
