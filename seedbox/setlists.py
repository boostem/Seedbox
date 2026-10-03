"""Setlists: several crates played back to back, each in its own set order, linked crate to crate.

Stored per crates folder: <root>/.seedbox/setlists/<id>.json ({id, name, crates, created_at, updated_at}); edits of a
link's mix plan (one crate's last track -> the next crate's first) go under <root>/.seedbox/setlists/<id>/mixplans/.
Crate links rank how well one crate's ending mixes into another's start (transitions.suggest on the last / first
tracks) and name a clearly better opening track for the next crate when there is one. A track that sits in more than
one of the setlist's crates plays once, at its first place.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from musicreco import library, transitions

from . import serializers
from .crates import ApiError, CrateService, NotFound, SetFile, audio_url, resolve_crate, track_json, with_cover
from .events import EventBus
from .mixes import MixService, Pair, _write_json_atomic, check_ids

log = logging.getLogger(__name__)

LINK_LIMIT = 6
BETTER_ENTRY_MARGIN = 8  # points a different opening track must win by to be suggested
MAX_CRATES = 24
EXHAUSTIVE_ORDER = 8  # up to this many crates auto-order tries every order
NAME_MAX = 120
_SETLIST_ID = re.compile(r"^[a-f0-9]{8,32}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SetlistService:
    def __init__(self, crates: CrateService, mixes: MixService, bus: EventBus) -> None:
        self.crates = crates
        self.mixes = mixes
        self.bus = bus
        self._lock = threading.RLock()

    @property
    def dir(self) -> Path:
        return self.crates.root / ".seedbox" / "setlists"

    def _path(self, sid: str) -> Path:
        if not _SETLIST_ID.match(sid):
            raise NotFound("no such setlist")
        return self.dir / f"{sid}.json"

    def _load(self, sid: str) -> dict:
        try:
            data = json.loads(self._path(sid).read_text("utf-8"))
        except FileNotFoundError:
            raise NotFound("no such setlist") from None
        except (OSError, ValueError) as e:
            raise ApiError(f"setlist unreadable: {e}", 500) from e
        if not isinstance(data, dict) or not isinstance(data.get("crates"), list):
            raise ApiError("setlist file is damaged", 500)
        return data

    def _write(self, data: dict) -> None:
        _write_json_atomic(self._path(data["id"]), data)
        self.bus.publish("setlist_changed", {"id": data["id"]})

    # --- crates and their files -------------------------------------------------------------------------------------

    def _crate(self, folder: str) -> tuple[Path, list[SetFile]] | None:
        try:
            seed_dir = resolve_crate(self.crates.root, folder)
        except ApiError:
            return None
        try:
            return seed_dir, self.crates.files(seed_dir)
        except OSError:
            return None

    def _clean_crates(self, crates: Any) -> list[str]:
        if not isinstance(crates, list) or not all(isinstance(c, str) for c in crates):
            raise ApiError("crates must be a list of crate folder names", 422)
        out = list(dict.fromkeys(c for c in crates if c))
        if len(out) > MAX_CRATES:
            raise ApiError(f"a setlist holds at most {MAX_CRATES} crates", 422)
        for c in out:
            resolve_crate(self.crates.root, c)  # 404 / 403 for unknown or unsafe names
        return out

    def sequence(self, sid: str) -> list[tuple[Path, SetFile]]:
        """The setlist's tracks in play order: (crate folder, file), repeats dropped."""
        seen: set[str] = set()
        out = []
        for folder in self._load(sid)["crates"]:
            found = self._crate(folder)
            if found is None:
                continue
            seed_dir, files = found
            for f in files:
                if f.track.id not in seen:
                    seen.add(f.track.id)
                    out.append((seed_dir, f))
        return out

    # --- links ---------------------------------------------------------------------------------------------------

    def _link(self, a_dir: Path, a: SetFile, b_dir: Path, b_files: list[SetFile], analyses: dict) -> dict:
        tol = self.crates.bpm_tol
        b = b_files[0]
        tr = transitions.suggest(a.track, analyses.get(a.track.id), b.track, analyses.get(b.track.id), bpm_tol=tol)
        better = None
        for f in b_files[1:]:
            t2 = transitions.suggest(a.track, analyses.get(a.track.id), f.track, analyses.get(f.track.id),
                                     bpm_tol=tol)  # fmt: skip
            if t2["score"] >= tr["score"] + BETTER_ENTRY_MARGIN and (better is None or t2["score"] > better[1]["score"]):
                better = (f, t2)
        seed = lambda d: track_json(library.load_crate_seed(d))  # noqa: E731
        return {
            "from_folder": a_dir.name, "to_folder": b_dir.name,
            "from_track": with_cover(serializers.track(a.track), a_dir.name, a.path),
            "to_track": with_cover(serializers.track(b.track), b_dir.name, b.path),
            "transition": tr,
            "better_entry": None if better is None else {
                "track": with_cover(serializers.track(better[0].track), b_dir.name, better[0].path),
                "transition": better[1]},
            "from_seed": seed(a_dir), "to_seed": seed(b_dir),
        }  # fmt: skip

    def _analyses_of(self, seed_dir: Path, files: list[SetFile]) -> dict:
        return self.crates._analyses(seed_dir, files, budget_s=1.5)[0]

    def links(self, folder: str) -> dict:
        """{out, in}: crates this crate's ending mixes into best, and crates whose ending mixes into its start."""
        found = self._crate(folder)
        if found is None:
            raise NotFound(f"no crate folder named {folder!r}")
        me_dir, me_files = found
        if not me_files:
            return {"out": [], "in": []}
        an = self._analyses_of(me_dir, [me_files[0], me_files[-1]])
        out, inn = [], []
        deadline = time.monotonic() + 6.0
        for c in library.list_crates(self.crates.root):
            if c["folder"] == me_dir.name or c["folder"] == library.MIXES_DIRNAME:
                continue
            other = self._crate(c["folder"])
            if other is None or not other[1]:
                continue
            o_dir, o_files = other
            if time.monotonic() < deadline:
                an.update(self._analyses_of(o_dir, [o_files[0], o_files[-1]]))
            out.append(self._link(me_dir, me_files[-1], o_dir, o_files[:1], an))
            inn.append(self._link(o_dir, o_files[-1], me_dir, me_files[:1], an))
        key = lambda link: (-link["transition"]["score"], not link["transition"]["ready"])  # noqa: E731
        return {"out": sorted(out, key=key)[:LINK_LIMIT], "in": sorted(inn, key=key)[:LINK_LIMIT]}

    # --- setlists ------------------------------------------------------------------------------------------------

    def list(self) -> list[dict]:
        out = []
        try:
            paths = sorted(self.dir.glob("*.json"))
        except OSError:
            paths = []
        for p in paths:
            try:
                data = self._load(p.stem)
            except ApiError:
                continue
            seeds, count = [], 0
            for folder in data["crates"]:
                found = self._crate(folder)
                seeds.append(track_json(library.load_crate_seed(found[0])) if found else None)
                count += len(found[1]) if found else 0
            out.append({"id": data["id"], "name": data.get("name") or "Setlist", "crates": data["crates"],
                        "seeds": seeds, "track_count": count, "updated_at": data.get("updated_at") or ""})
        return sorted(out, key=lambda s: s["updated_at"], reverse=True)

    def get(self, sid: str) -> dict:
        """Setlist (types.ts): its crates as blocks of set tracks, and the crate-to-crate links between them."""
        data = self._load(sid)
        blocks, seen, total_s = [], set(), 0.0
        wanted: list[tuple[Path, list[SetFile]]] = []  # an open setlist's songs are split first, in play order
        for folder in data["crates"]:
            found = self._crate(folder)
            if found is None:
                blocks.append({"folder": folder, "seed": None, "missing": True, "tracks": []})
                continue
            seed_dir, files = found
            files = [f for f in files if f.track.id not in seen]
            seen.update(f.track.id for f in files)
            _an, states = self.crates._analyses(seed_dir, files, budget_s=1.0)
            wanted.append((seed_dir, files))
            tracks = []
            for n, f in enumerate(files, 1):
                tracks.append({
                    "track": with_cover(serializers.track(f.track), seed_dir.name, f.path), "path": str(f.path),
                    "audio_url": audio_url(seed_dir.name, f.path.name), "order": n,
                    "energy_rating": f.energy_rating, "genre": f.genre,
                    "analysis_state": states.get(f.track.id, "missing"),
                })  # fmt: skip
                total_s += f.track.duration_s or 0
            blocks.append({"folder": seed_dir.name, "seed": track_json(library.load_crate_seed(seed_dir)),
                           "missing": False, "tracks": tracks, "_files": files, "_dir": seed_dir})
        for seed_dir, files in reversed(wanted):
            self.crates.want_stems(seed_dir, files, front=True)
        links = []
        live = [b for b in blocks if b["tracks"]]
        for x, y in zip(live, live[1:]):
            an = self._analyses_of(x["_dir"], [x["_files"][-1]])
            an.update(self._analyses_of(y["_dir"], y["_files"]))
            links.append(self._link(x["_dir"], x["_files"][-1], y["_dir"], y["_files"], an))
        for b in blocks:
            b.pop("_files", None)
            b.pop("_dir", None)
        return {"id": data["id"], "name": data.get("name") or "Setlist", "created_at": data.get("created_at") or "",
                "updated_at": data.get("updated_at") or "", "blocks": blocks, "links": links,
                "track_count": sum(len(b["tracks"]) for b in blocks), "duration_s": round(total_s, 1)}

    def create(self, name: Any, crates: Any) -> dict:
        crates = self._clean_crates(crates)
        if not crates:
            raise ApiError("pick at least one crate", 422)
        nm = name.strip()[:NAME_MAX] if isinstance(name, str) and name.strip() else None
        if nm is None:
            seeds = [library.load_crate_seed(self.crates.root / c) for c in crates[:2]]
            nm = " → ".join(s.name if s else c for s, c in zip(seeds, crates)) + (" …" if len(crates) > 2 else "")
        now = _now()
        data = {"id": uuid.uuid4().hex[:12], "name": nm, "crates": crates, "created_at": now, "updated_at": now}
        with self._lock:
            self._write(data)
        return self.get(data["id"])

    def update(self, sid: str, patch: Any) -> dict:
        if not isinstance(patch, dict):
            raise ApiError("a JSON object is required", 422)
        with self._lock:
            data = self._load(sid)
            if "name" in patch:
                if not isinstance(patch["name"], str) or not patch["name"].strip():
                    raise ApiError("name must be a non-empty string", 422)
                data["name"] = patch["name"].strip()[:NAME_MAX]
            if "crates" in patch:
                data["crates"] = self._clean_crates(patch["crates"])
            data["updated_at"] = _now()
            self._write(data)
        return self.get(sid)

    def delete(self, sid: str) -> dict:
        with self._lock:
            path = self._path(sid)
            if not path.is_file():
                raise NotFound("no such setlist")
            path.unlink()
            for p in (self.dir / sid / "mixplans").glob("*.json") if (self.dir / sid).is_dir() else []:
                p.unlink(missing_ok=True)
        self.bus.publish("setlist_changed", {"id": sid})
        return {"ok": True}

    def auto_order(self, sid: str) -> dict:
        """Order the crates (the first one stays the opener) for the best crate-to-crate links."""
        with self._lock:
            data = self._load(sid)
            crates = data["crates"]
            found = {c: self._crate(c) for c in crates}
            live = [c for c in crates if found[c] and found[c][1]]
            if len(live) >= 3:
                an: dict = {}
                for c in live:
                    d, files = found[c]
                    an.update(self._analyses_of(d, [files[0], files[-1]]))
                tol = self.crates.bpm_tol
                score = {}
                for x, y in itertools.permutations(live, 2):
                    a, b = found[x][1][-1], found[y][1][0]
                    score[x, y] = transitions.suggest(a.track, an.get(a.track.id), b.track, an.get(b.track.id),
                                                      bpm_tol=tol)["score"]  # fmt: skip
                order = _best_path(live, score)
                data["crates"] = order + [c for c in crates if c not in order]
                data["updated_at"] = _now()
                self._write(data)
        return self.get(sid)

    # --- link mix plans ------------------------------------------------------------------------------------------

    def pair(self, sid: str, from_id: str, to_id: str) -> Pair:
        """A -> B for a link mix plan: both tracks in this setlist's crates (normally one crate's last track and
        the next crate's first)."""
        check_ids(from_id, to_id)
        seq, pairs = self.pairs_for_mix(sid, chain=False)
        where = {f.track.id: (d, f) for d, f in seq}
        if from_id not in where or to_id not in where:
            raise NotFound("track not in this setlist")
        k = next((k for k, p in enumerate(pairs) if (p.a.track.id, p.b.track.id) == (from_id, to_id)), None)
        if k is not None and pairs[k].key == f"setlist:{sid}":  # a link in the play order: knows the styles before it
            return self.mixes.with_chain(pairs)[k]
        (ad, af), (bd, bf) = where[from_id], where[to_id]
        return Pair(f"setlist:{sid}", ad, af, bd, bf, self.dir / sid / "mixplans" / f"{from_id}__{to_id}.json")

    def pairs_for_mix(self, sid: str, chain: bool = True) -> tuple[list[tuple[Path, SetFile]], list[Pair]]:
        """The play order and the Pair of every consecutive transition: a crate's own pair inside a crate (its
        saved edits apply), the setlist's link pair across crates; with `chain`, each knows the styles of the
        transitions before it in the whole setlist."""
        seq = self.sequence(sid)
        pairs = []
        for (ad, af), (bd, bf) in zip(seq, seq[1:]):
            if ad == bd:
                pairs.append(self.mixes.pair(ad.name, af.track.id, bf.track.id, chain=False))
            else:
                pairs.append(Pair(f"setlist:{sid}", ad, af, bd, bf,
                                  self.dir / sid / "mixplans" / f"{af.track.id}__{bf.track.id}.json"))
        return seq, (self.mixes.with_chain(pairs) if chain else pairs)

    def title(self, sid: str) -> str:
        return self._load(sid).get("name") or "Setlist"


def _best_path(nodes: list[str], score: dict[tuple[str, str], float]) -> list[str]:
    """Order with nodes[0] fixed that maximises the summed link scores: every order for small sets, else greedy
    nearest neighbour refined by 2-opt moves."""
    first, rest = nodes[0], nodes[1:]

    def total(order: list[str]) -> float:
        return sum(score[x, y] for x, y in zip(order, order[1:]))

    if len(nodes) <= EXHAUSTIVE_ORDER:
        return max(([first, *p] for p in itertools.permutations(rest)), key=total)
    order, left = [first], set(rest)
    while left:
        nxt = max(sorted(left), key=lambda y: score[order[-1], y])
        order.append(nxt)
        left.remove(nxt)
    improved = True
    while improved:
        improved = False
        for i in range(1, len(order) - 1):
            for j in range(i + 1, len(order)):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if total(cand) > total(order) + 1e-9:
                    order, improved = cand, True
    return order

