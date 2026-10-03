"""Engine objects -> the JSON shapes of frontend/src/api/types.ts (snake_case), plus NaN-safe JSON encoding."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from starlette.responses import Response

from musicreco.models import Score, SearchPage, Track


def track(t: Track) -> dict:
    """Track (types.ts)."""
    return t.to_json()


def score(s: Score) -> dict:
    """Score (types.ts)."""
    return {"value": s.value, "passed": s.passed, "move": s.move, "chips": list(s.chips)}


def rec(t: Track, s: Score, rule_reason: str, existing_path: Path | None, pretick_all: bool = False) -> dict:
    """Rec (types.ts): ticked_default = settings.pretick_all or the pre-tick rule (score.passed)."""
    return {
        "track": track(t),
        "score": score(s),
        "rule_reason": rule_reason,
        "ticked_default": bool(pretick_all) or s.passed,
        "existing_path": str(existing_path) if existing_path else None,
    }


def search_page(p: SearchPage) -> dict:
    """SearchPage (types.ts)."""
    return {"page": p.page, "total_count": p.total_count, "has_next": p.has_next, "items": [track(t) for t in p.items]}


def clean(obj: Any) -> Any:
    """JSON-safe copy: non-finite floats -> None, Paths -> str, tuples/sets -> lists."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Mapping):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def dumps(obj: Any) -> str:
    """Compact JSON; strict (no NaN) on the fast path, cleaned and retried if something isn't encodable."""
    try:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError):
        return json.dumps(clean(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=str)


class JSON(Response):
    """JSON response rendered with `dumps` (rendered where it's built, so sync endpoints encode off the loop)."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return dumps(content).encode("utf-8")
