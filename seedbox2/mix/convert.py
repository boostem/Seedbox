"""Seedbox 1 plans -> plan v3. Seedbox 1's plan (version 2, or older without a version) has the same master timeline, so
the lanes, tempo map, markers and notes carry over as they are, and a plan someone edited stays their plan.

Seedbox 1 was laxer about one thing: an edited plan can have a loop off the beat. Those move to the nearest whole beat
(and a loop that vanishes doing so is dropped), and the plan's notes say so.
"""

from __future__ import annotations

from musicreco import mixplan as sb1

from . import plan as v3


def _loop_length(length: float) -> float:
    if length >= 1.0:
        return float(max(1, round(length)))
    return min(v3.LOOP_DIVISIONS, key=lambda d: abs(d - length))


def _loops(raw: list[dict]) -> tuple[list[dict], int]:
    """On whole beats; and how many had to change."""
    out, moved = [], 0
    for lp in raw:
        snapped = {
            "start_beat": float(round(lp["start_beat"])), "end_beat": float(round(lp["end_beat"])),
            "source_beat": float(round(lp["source_beat"])), "length_beats": _loop_length(lp["length_beats"]),
        }  # fmt: skip
        moved += any(abs(snapped[k] - lp[k]) > 1e-9 for k in snapped)
        if snapped["end_beat"] > snapped["start_beat"]:
            out.append(snapped)
    return out, moved


def _deck(deck: dict, track_id: str | None) -> tuple[dict, int]:
    grid = deck.get("grid") or [{"start_beat": 0.0, "start_s": deck["beat0_s"], "bpm": deck["native_bpm"]}]
    loops, moved = _loops(deck["loops"])
    return {
        "track_id": track_id or deck["track_id"], "grid": grid, "play_from_beat": deck["play_from_beat"],
        "play_to_beat": deck["play_to_beat"], "lanes": deck["lanes"], "loops": loops, "fx": deck["fx"],
        "sends": {"echo_beats": deck["echo_beats"], "echo_tap": deck["echo_tap"],
                  "reverb_size": deck["reverb_size"], "reverb_tap": deck["reverb_tap"]},
    }, moved  # fmt: skip


def from_seedbox1(raw: object, *, from_track: str | None = None, to_track: str | None = None) -> v3.MixPlan:
    """A v3 plan from a Seedbox 1 plan, ValueError if Seedbox 1 itself wouldn't take it. Its `from_id` and `to_id` are
    Spotify IDs: pass the library's hashes as `from_track` and `to_track` to have the decks refer to those."""
    plan = sb1.validate(raw)  # refuses non-objects; clamps, sorts and fills in every lane
    a, moved_a = _deck(plan["a"], from_track)
    b, moved_b = _deck(plan["b"], to_track)
    notes = plan["notes"]
    if moved_a + moved_b:
        notes = [*notes, f"Off-beat loops moved onto whole beats when converting from Seedbox 1: {moved_a + moved_b}."]
    return v3.validate({
        "technique": plan["technique"], "bars": plan["bars"], "key_lock": plan["key_lock"], "window": plan["window"],
        "tempo": plan["tempo"], "a": a, "b": b, "markers": plan["markers"], "notes": notes, "edited": plan["edited"],
    })  # fmt: skip


def load(raw: object, *, from_track: str | None = None, to_track: str | None = None) -> v3.MixPlan:
    """A plan as the library stores it, whichever version wrote it (imports keep Seedbox 1's JSON)."""
    if isinstance(raw, dict) and raw.get("version") == v3.PLAN_VERSION:
        return v3.validate(raw)
    return from_seedbox1(raw, from_track=from_track, to_track=to_track)
