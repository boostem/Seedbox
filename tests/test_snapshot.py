"""Plan summaries of every fixture pair, compared with tests/snapshots/plan_summaries.txt. A planner change shows up
as a diff of that file: regenerate it with `UPDATE_SNAPSHOTS=1 python -m pytest tests/test_snapshot.py` and commit
it with the change."""

from __future__ import annotations

import os
from pathlib import Path

from musicreco import mixplan, transitions

from .fixtures import PAIRS, analysis, track

SNAPSHOT = Path(__file__).parent / "snapshots" / "plan_summaries.txt"


def _render() -> str:
    out = []
    for name, a, b in PAIRS:
        ta, tb, aa, ab = track(a), track(b), analysis(a), analysis(b)
        tr = transitions.suggest(ta, aa, tb, ab)
        plan = mixplan.generate(ta, aa, tb, ab, "folder")
        out += [
            f"## {name}",
            f"suggest: {tr['technique']}, {tr['overlap_bars']} bars, swap {tr['swap_bar']}, "
            f"A out {tr['out_start_s']} s, B in {tr['in_start_s']} s, score {tr['score']}",
            f"plan: {mixplan.summary(plan, aa, ab)}",
            *(f"  note: {n}" for n in plan["notes"]),
            "",
        ]
    return "\n".join(out)


def test_plan_summaries_match_snapshot() -> None:
    text = _render()
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(text, encoding="utf-8")
    print(text)
    assert SNAPSHOT.exists(), f"no snapshot yet: rerun with UPDATE_SNAPSHOTS=1 to write {SNAPSHOT.name}"
    assert text == SNAPSHOT.read_text(encoding="utf-8"), (
        "plan summaries changed: review the diff and rerun with UPDATE_SNAPSHOTS=1 to accept"
    )
