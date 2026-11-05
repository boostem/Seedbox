"""What the model can do, and all it can see. A `Session` holds one run's draft set: the tracks chosen, the energy arc, the
tempo journey and the locks. Nothing here writes to the library, so a run's worst case is a draft the person throws away.

Tracks go out through `public_track` only, under ids local to the run (`t1`, `t2`, ...): title, artist, genre tag and
analysis numbers. No content hashes, file paths, albums, audio or embeddings. A vibe search runs here, on our CLAP index,
and the model gets back a similarity."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from seedbox2.library.view import to_camelot
from seedbox2.sets import OrderRequest, load_features, order_tracks
from seedbox2.sets.arcs import TEMPLATES, ArcError, arc_from
from seedbox2.sets.explain import rating
from seedbox2.sets.features import VOCALS_ANALYSER, vocal_share
from seedbox2.sets.service import add_mix_plans
from seedbox2.sets.solver import SolverError

log = logging.getLogger(__name__)

DEFAULT_RESULTS, MAX_RESULTS = 30, 100
MAX_SET_TRACKS = 100  # the solver takes 3 s here, and 10 s at 200
LAST = -1  # a lock's position that follows the set's end as tracks come and go

TOOLS: list[dict] = [
    {
        "name": "search_library",
        "description": "Search the tracks the person allowed. Every filter is optional and they combine. Numeric filters skip tracks where that number isn't analysed yet. Returns tracks with an id for the other tools; `energy` is our 1-10 rating, `vocals` the share of beats that are sung (0-1), `vibe` the similarity to `text` (compare within one search only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "A sound or mood to rank by, matched against the audio analysis: 'warm deep house', 'female vocal', 'dark and driving'."},
                "query": {"type": "string", "description": "Part of a title or artist."},
                "genre": {"type": "string", "description": "Part of the genre tag. Many tracks have none, so prefer `text` for a style."},
                "bpm_min": {"type": "number"},
                "bpm_max": {"type": "number"},
                "keys": {"type": "array", "items": {"type": "string"}, "description": "Camelot codes, such as 8A."},
                "energy_min": {"type": "integer", "minimum": 1, "maximum": 10},
                "energy_max": {"type": "integer", "minimum": 1, "maximum": 10},
                "vocals_min": {"type": "number", "minimum": 0, "maximum": 1},
                "vocals_max": {"type": "number", "minimum": 0, "maximum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "description": f"Default {DEFAULT_RESULTS}."},
            },
        },
    },
    {
        "name": "choose_tracks",
        "description": "Add tracks to the set or take them out. Returns the set's size and total length; tracks overlap when mixed, so the set plays about a minute per track shorter than the total.",
        "input_schema": {
            "type": "object",
            "properties": {
                "add": {"type": "array", "items": {"type": "string"}},
                "remove": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "set_energy_arc",
        "description": "The shape the set's energy should follow. Give a template or points. A point is [time, level], both 0-1: time is the share of the set's running order (a peak at 60 minutes of 90 is time 0.67), and level is on the set's own range, 0 for its quietest track and 1 for its most intense.",
        "input_schema": {
            "type": "object",
            "properties": {
                "template": {"type": "string", "enum": list(TEMPLATES), "description": "; ".join(f"{a.name}: {a.description}" for a in TEMPLATES.values())},
                "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}, "minItems": 2, "maxItems": 32},
            },
        },
    },
    {
        "name": "set_tempo_journey",
        "description": "Drift the tempo from one BPM to another across the set. Leave both out to clear it: the set then follows its own tempos.",
        "input_schema": {"type": "object", "properties": {"start_bpm": {"type": "number"}, "end_bpm": {"type": "number"}}},
    },
    {
        "name": "lock_track",
        "description": "Hold a chosen track at a position in the running order, so the solver orders the others around it. Position 1 is the first; -1 is always the last. Leave position out to unlock.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}, "position": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "run_solver",
        "description": "Put the chosen tracks in the best order for the arc, the tempo journey and the locks. Returns the order, each track's energy against the arc's target, and each transition in words with flags for problems (key_clash, tempo_over_limit, ...). Change the tracks or the arc and run it again if a flag or the arc's fit matters.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "finish",
        "description": "End the run: this draft goes to the person to edit. The solver runs once more if anything changed since it last ran.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "A short name for the set."},
                "reasoning": {"type": "string", "description": "For the person, in 2 to 5 plain sentences: how you read the request, what you chose and why, and anything you couldn't do."},
            },
            "required": ["name", "reasoning"],
        },
    },
]  # fmt: skip


class ToolError(Exception):
    """Goes back to the model as a tool error, for it to correct."""


def _number(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


class Session:
    def __init__(self, lib: Any, scope: Sequence[str] | None = None, *,
                 vibe: Callable[[str], Mapping[str, float]] | None = None) -> None:  # fmt: skip
        """`scope`: the hashes the person allowed, or None for the whole library. `vibe(text)`: each track's similarity
        to the text; it raises when CLAP can't answer."""
        self.lib = lib
        self.vibe = vibe
        self.rows = {r["hash"]: r for r in lib.view_rows(hashes=scope)}
        self._ids: dict[str, str] = {}
        self._hashes: dict[str, str] = {}
        self._vocals: dict[str, float | None] = {}
        self._features: dict[str, Any] = {}
        self.chosen: list[str] = []
        self.arc: str | list[list[float]] | None = None
        self.tempo: tuple[float, float] | None = None
        self.locks: dict[str, int] = {}  # hash -> position, 0-based or LAST
        self.result: dict | None = None
        self.stale = True
        self.name = ""
        self.reasoning = ""
        self.finished = False

    # --- what the model sees ----------------------------------------------------------------------------------------

    def _id(self, hash_: str) -> str:
        if hash_ not in self._ids:
            self._ids[hash_] = f"t{len(self._ids) + 1}"
            self._hashes[self._ids[hash_]] = hash_
        return self._ids[hash_]

    def _hash(self, id_: Any) -> str:
        hash_ = self._hashes.get(id_) if isinstance(id_, str) else None
        if hash_ is None:
            raise ToolError(f"no track {id_}: use an id from search_library")
        return hash_

    def vocals(self, hash_: str) -> float | None:
        if hash_ not in self._vocals:
            row = self.lib.analysis(hash_, VOCALS_ANALYSER)
            self._vocals[hash_] = vocal_share(row["data"]) if row else None
        return self._vocals[hash_]

    def public_track(self, hash_: str, vibe: float | None = None) -> dict:
        """All the model ever learns about a track. Only our own analysis counts, as for the solver (`sets.features`)."""
        r = self.rows[hash_]
        vocals = self.vocals(hash_)
        out = {
            "id": self._id(hash_), "title": r.get("title") or "", "artist": r.get("artist") or "",
            "minutes": round(float(r["duration"]) / 60, 1) if r.get("duration") else None,
            "bpm": r["bpm"] if r.get("bpm_from") == "analysis" else None,
            "key": r["key"] if r.get("key_from") in ("analysis", "user") else None,
            "energy": rating(r["energy"]) if r.get("energy_from") == "analysis" else None,
            "vocals": round(vocals, 2) if vocals is not None else None,
            "genre": r.get("genre") or None,
            "vibe": round(vibe, 3) if vibe is not None else None,
        }  # fmt: skip
        return {k: v for k, v in out.items() if v is not None}

    def scope_summary(self) -> dict:
        """The numbers the first message gives: no titles yet, those come with a search."""
        rows = self.rows.values()
        bpms = sorted(r["bpm"] for r in rows if r.get("bpm_from") == "analysis" and r.get("bpm"))
        return {
            "tracks": len(self.rows), "hours": round(sum(r.get("duration") or 0 for r in rows) / 3600, 1),
            "analysed": {"bpm": len(bpms), "key": sum(r.get("key_from") in ("analysis", "user") for r in rows),
                         "energy": sum(r.get("energy_from") == "analysis" for r in rows)},
            "bpm_range": [bpms[0], bpms[-1]] if bpms else None,
        }  # fmt: skip

    # --- the tools --------------------------------------------------------------------------------------------------

    def search_library(self, args: dict) -> dict:
        note = None
        sims: Mapping[str, float] | None = None
        text = (args.get("text") or "").strip()
        if text:
            try:
                if self.vibe is None:
                    raise RuntimeError("no vibe search here")
                sims = self.vibe(text)
            except Exception as e:  # a missing model or a stuck worker shouldn't end the run. The error stays here: it may name a path
                log.info("no vibe search: %s", e)
                note = "The vibe search isn't available, so `text` was ignored. Use the other filters."
        query, genre = (args.get("query") or "").casefold(), (args.get("genre") or "").casefold()
        keys = {to_camelot(k) for k in args.get("keys") or []} - {None}
        out: list[tuple[float, str]] = []
        for h in self.rows:
            r = self.rows[h]
            if query and query not in f"{r.get('title') or ''} {r.get('artist') or ''}".casefold():
                continue
            if genre and genre not in (r.get("genre") or "").casefold():
                continue
            bpm = r["bpm"] if r.get("bpm_from") == "analysis" else None
            if not _in_range(bpm, args.get("bpm_min"), args.get("bpm_max")):
                continue
            if keys and not (r.get("key_from") in ("analysis", "user") and r.get("key") in keys):
                continue
            energy = rating(r["energy"]) if r.get("energy_from") == "analysis" else None
            if not _in_range(energy, args.get("energy_min"), args.get("energy_max")):
                continue
            if (args.get("vocals_min") is not None or args.get("vocals_max") is not None) and not _in_range(
                self.vocals(h), args.get("vocals_min"), args.get("vocals_max")
            ):
                continue
            if sims is not None and h not in sims:
                continue  # not embedded yet: nothing to rank it by
            out.append((-float(sims[h]) if sims is not None else 0.0, h))
        out.sort(key=lambda x: (x[0], (self.rows[x[1]].get("artist") or "").casefold(), self.rows[x[1]].get("title") or "", x[1]))
        limit = args.get("limit") if isinstance(args.get("limit"), int) else DEFAULT_RESULTS
        limit = max(1, min(limit, MAX_RESULTS))
        tracks = [self.public_track(h, None if sims is None else float(sims[h])) for _, h in out[:limit]]
        result: dict[str, Any] = {"matched": len(out), "tracks": tracks}
        if note:
            result["note"] = note
        return result

    def choose_tracks(self, args: dict) -> dict:
        add = [self._hash(i) for i in args.get("add") or []]
        remove = {self._hash(i) for i in args.get("remove") or []}
        chosen = [h for h in self.chosen if h not in remove]
        chosen += [h for h in dict.fromkeys(add) if h not in chosen]
        if len(chosen) > MAX_SET_TRACKS:
            raise ToolError(f"a set takes up to {MAX_SET_TRACKS} tracks here")
        self.chosen = chosen
        self.locks = {h: p for h, p in self.locks.items() if h in chosen}
        self.stale = True
        return {"tracks": len(chosen), "total_minutes": self._minutes()}

    def _minutes(self) -> float:
        return round(sum(self.rows[h].get("duration") or 0 for h in self.chosen) / 60, 1)

    def set_energy_arc(self, args: dict) -> dict:
        spec = args.get("template") or args.get("points")
        if spec is None:
            raise ToolError("give a template or points")
        try:
            arc = arc_from(spec)
        except (ArcError, TypeError, ValueError) as e:
            raise ToolError(str(e)) from None
        self.arc = spec
        self.stale = True
        return {"arc": arc.name, "points": [list(p) for p in arc.points]}

    def set_tempo_journey(self, args: dict) -> dict:
        start, end = _number(args.get("start_bpm")), _number(args.get("end_bpm"))
        if start is None and end is None:
            self.tempo = None
        elif start is None or end is None or not (40 <= start <= 250 and 40 <= end <= 250):
            raise ToolError("a tempo journey needs a start and an end between 40 and 250 BPM")
        else:
            self.tempo = (start, end)
        self.stale = True
        return {"tempo": self.tempo}

    def lock_track(self, args: dict) -> dict:
        h = self._hash(args.get("id"))
        if h not in self.chosen:
            raise ToolError(f"{args.get('id')} isn't in the set: choose it first")
        position = args.get("position")
        if position is None:
            self.locks.pop(h, None)
        elif not isinstance(position, int) or isinstance(position, bool) or (position != LAST and position < 1):
            raise ToolError("a position is 1 or more, or -1 for the last")
        else:
            self.locks[h] = LAST if position == LAST else position - 1
        self.stale = True
        return {"locks": [{"id": self._id(x), "position": LAST if p == LAST else p + 1} for x, p in self.locks.items()]}

    def request(self) -> OrderRequest:
        pins = [{"position": len(self.chosen) - 1 if p == LAST else p, "hash": h} for h, p in self.locks.items()]
        start, end = self.tempo or (None, None)
        return OrderRequest(arc=self.arc, tempo_start=start, tempo_end=end, pins=pins)

    def solve(self) -> dict:
        if len(self.chosen) < 2:
            raise ToolError("choose at least two tracks first")
        missing = set(self.chosen) - set(self._features)
        if missing:
            self._features.update(load_features(self.lib, missing))
        try:
            result = order_tracks([self._features[h] for h in self.chosen], self.request())
        except (ArcError, SolverError) as e:
            raise ToolError(str(e)) from None
        self.result = add_mix_plans(self.lib, result)
        self.stale = False
        return self.result

    def run_solver(self, _args: dict) -> dict:
        result = self.solve()
        order = [{
            "position": t["position"] + 1, **self.public_track(t["hash"]), "shift_semitones": t["shift"] or None,
            "target_energy": rating(t["target_energy"]),
            "target_bpm": t["target_bpm"],
        } for t in result["order"]]  # fmt: skip
        transitions = [{"after_position": i + 1, "text": t["text"], "flags": t["flags"]}
                       for i, t in enumerate(result["transitions"])]  # fmt: skip
        return {"total_minutes": self._minutes(), "cost": result["cost"], "order": [_compact(o) for o in order],
                "transitions": transitions, "not_analysed": {k: v for k, v in result["unknown"].items() if v}}  # fmt: skip

    def finish(self, args: dict) -> dict:
        if self.stale or self.result is None:
            self.solve()
        self.name = str(args.get("name") or "").strip()[:80]
        self.reasoning = str(args.get("reasoning") or "").strip()[:4000]
        self.finished = True
        return {"done": True}

    def call(self, name: str, args: Any) -> dict:
        """Runs a tool, for the model. ToolError is the model's to fix."""
        if name not in _NAMES:
            raise ToolError(f"no tool called {name}")
        if not isinstance(args, dict):
            raise ToolError("the input must be an object")
        return getattr(self, name)(args)


_NAMES = frozenset(t["name"] for t in TOOLS)


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _in_range(value: float | None, low: Any, high: Any) -> bool:
    low, high = _number(low), _number(high)
    if low is None and high is None:
        return True
    return value is not None and (low is None or value >= low) and (high is None or value <= high)
