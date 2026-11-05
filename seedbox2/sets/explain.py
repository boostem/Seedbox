"""Why each transition is what it is, in the words a DJ would use: "energy +1, 8A → 9A, same vibe"."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from seedbox2.sets.cost import (
    TEMPO_LIMIT_PCT,
    TEMPO_PREFER_PCT,
    UNKNOWN_KEY,
    Problem,
    camelot_name,
    key_move_name,
    shifted,
)

SAME_VIBE, VIBE_SHIFT = 0.35, 0.7  # of the set's own spread (`cost._vibe_matrix`)
SHOW_TEMPO_PCT = 2.5  # a smaller change isn't worth a word
ZERO_SCORE_COST = 6.0  # what the two tracks may cost each other before a transition scores 0
GAP_SCORE = 55  # under this it's a gap (`bridge.py`): any clash left in the order; not a +2 move with a vibe shift and a genre change


def rating(energy: float | None) -> int | None:
    """The 1-10 rating an 0-1 energy came from."""
    return None if energy is None else round(energy * 9 + 1)


def score_of(mix_cost: float) -> int:
    """A transition's 0-100, from what its two tracks cost each other."""
    return round(100 * max(0.0, 1.0 - mix_cost / ZERO_SCORE_COST))


def _key_words(p: Problem, a: int, b: int, shifts: np.ndarray) -> tuple[str, str | None]:
    """("8A → 9A", the move's name)."""
    ka, kb = int(p.keys[a]), int(p.keys[b])
    if ka == UNKNOWN_KEY or kb == UNKNOWN_KEY:
        return "key unknown", None
    sa, sb = int(shifts[a]), int(shifts[b])
    pa, pb = shifted(ka, sa), shifted(kb, sb)
    left, right = camelot_name(pa), camelot_name(pb)
    if sa:
        left = f"{left} ({camelot_name(ka)} {sa:+d} st)"
    if sb:
        right = f"{right} ({camelot_name(kb)} {sb:+d} st)"
    return f"{left} → {right}", key_move_name(pa, pb)


def _tempo_words(p: Problem, a: int, b: int) -> tuple[str | None, list[str]]:
    if not p.tempo_known[a, b]:
        return "tempo unknown", []
    pct, factor = float(p.tempo_pct[a, b]), float(p.tempo_factor[a, b])
    flags = []
    if pct > TEMPO_LIMIT_PCT:
        flags.append("tempo_over_limit")
    elif pct > TEMPO_PREFER_PCT:
        flags.append("tempo_wide")
    sign = "+" if p.bpm[b] * factor >= p.bpm[a] else "-"
    if factor == 0.5:
        return f"half time, tempo {sign}{pct:.1f} %", flags
    if factor == 2.0:
        return f"double time, tempo {sign}{pct:.1f} %", flags
    if pct < SHOW_TEMPO_PCT:
        return None, flags
    return f"tempo {sign}{pct:.1f} %" + (" (over the limit)" if pct > TEMPO_LIMIT_PCT else ""), flags


def _vibe_words(p: Problem, a: int, b: int) -> str | None:
    if not p.vibe_known[a, b]:
        return None
    d = float(p.vibe[a, b])
    return "same vibe" if d <= SAME_VIBE else "vibe shift" if d >= VIBE_SHIFT else "related vibe"


def transition(p: Problem, order: Sequence[int], shifts: np.ndarray, at: int) -> dict:
    """The step from position `at` to the next: its words, its flags and what each term cost."""
    a, b = order[at], order[at + 1]
    ta, tb = p.tracks[a], p.tracks[b]
    words: list[str] = []
    flags: list[str] = []

    ra, rb = rating(ta.energy), rating(tb.energy)
    words.append(f"energy {rb - ra:+d}" if ra is not None and rb is not None and rb != ra
                 else "energy level" if ra is not None and rb is not None else "energy unknown")  # fmt: skip

    key_text, move = _key_words(p, a, b, shifts)
    words.append(key_text)
    if move == "clash":
        flags.append("key_clash")
    if shifts[b]:
        flags.append("key_fix")

    tempo_text, tempo_flags = _tempo_words(p, a, b)
    flags += tempo_flags
    if tempo_text:
        words.append(tempo_text)
    vibe_text = _vibe_words(p, a, b)
    if vibe_text:
        words.append(vibe_text)
    if p.genre_change[a, b]:
        words.append(f"{ta.genre} → {tb.genre}")
        flags.append("genre_change")

    three = at + 2 < len(order) and p.heavy[a] * p.heavy[b] * p.heavy[order[at + 2]] > 0.25
    if three:
        words.append("third vocal track in a row")
        flags.append("vocal_run")

    pos = np.array(order, np.int64).reshape(1, -1)
    energy_step = float(p.energy_step_costs(pos)[0, at])
    key_cost = float(p.key_matrix(shifts)[a, b]) + (p.w.key_fix if shifts[b] else 0.0)
    terms = {"key": key_cost, "tempo": float(p.terms_tempo[a, b]), "energy": energy_step + float(p.level_cost[b, at + 1]),
             "journey": float(p.journey_cost[b, at + 1]), "vibe": float(p.terms_vibe[a, b]),
             "genre": float(p.terms_genre[a, b]), "vocals": float(p.run_costs(pos)[0, at]) if at + 2 < len(order) else 0.0}  # fmt: skip
    if p.groups is not None:
        terms["crate"] = float(p.crate_cost[b, at + 1])
    # Only what the two tracks do to each other: not how they fit the arc or the journey, nor a run of three. A shift
    # stays on for the whole track, so each of its two transitions carries half.
    fixes = p.w.key_fix / 2 * (int(shifts[a] != 0) + int(shifts[b] != 0))
    mix_cost = (float(p.key_matrix(shifts)[a, b]) + fixes + energy_step + float(p.terms_tempo[a, b] + p.terms_vibe[a, b]
                + p.terms_genre[a, b]))  # fmt: skip
    score = score_of(mix_cost)
    return {
        "from": ta.hash, "to": tb.hash, "text": ", ".join(words), "flags": flags, "key_move": move,
        "tempo_pct": round(float(p.tempo_pct[a, b]), 2) if p.tempo_known[a, b] else None,
        "energy_change": rb - ra if ra is not None and rb is not None else None,
        "cost": round(sum(terms.values()), 3), "terms": {k: round(v, 3) for k, v in terms.items()},
        "mix_cost": round(mix_cost, 3), "score": score, "gap": score < GAP_SCORE,
    }  # fmt: skip


def transitions(p: Problem, order: Sequence[int], shifts: np.ndarray) -> list[dict]:
    return [transition(p, order, shifts, at) for at in range(len(order) - 1)]
