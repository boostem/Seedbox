"""v4 mix plans and rendered transitions: generate / save / render the MixPlan for one A -> B pair.

A pair is two tracks of one crate (saved edits: <crate>/.seedbox/mixplans/<from>__<to>.json) or, for a setlist's
crate-to-crate links, tracks of two crates (saved edits under the setlist, see setlists.py). Saves are atomic.
A transition in a set is planned knowing the styles of the transitions before it (chain / mixplan.plan_styles), so
the set's technical moves vary while each one is still the best fit for its two songs.
Renders: <cache>/renders/<hash>.m4a + .json, keyed by mixrender.plan_hash, at most RENDER_WORKERS at a time;
identical requests share one render; the folder is trimmed least-recently-used first to RENDER_CACHE_MB.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from musicreco import transitions

from . import engine, serializers
from .crates import ApiError, CrateService, NotFound, SetFile, resolve_crate
from .events import EventBus

log = logging.getLogger(__name__)

RENDER_WORKERS = 2
RENDER_CACHE_MB = 1024
CHAIN_CACHE = 32  # sets whose style chain is remembered
CHAIN_RECENT = 4  # styles before a transition that its plan sees
PROGRESS_STEP = 0.05  # publish render progress in steps of at least this much
TECHNIQUES = ("long blend", "phrase blend", "bass swap", "drop swap", "quick cut", "echo out", "filter sweep",
              "loop roll", "slam", "brake", "spinback", "double drop")  # fmt: skip
SWAPS = ("hard", "quick", "slow")
BARS = (4, 8, 16, 32)
_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_RENDER_ID = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


def _module(name: str) -> Any:
    mod = engine.optional(name)
    if mod is None:
        raise ApiError(f"mix {'rendering' if name == 'mixrender' else 'planning'} isn't available in this build", 503)
    return mod


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serializers.dumps(data))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class Pair:
    """A -> B for a mix plan: both tracks' crate folder + set file, where edits are saved, and the key that render
    events and plan["folder"] carry (the crate folder, or "setlist:<id>" for a crate-to-crate link)."""

    key: str
    a_dir: Path
    a: SetFile
    b_dir: Path
    b: SetFile
    saved: Path
    recent: list[str] | None = None  # styles of the transitions before this one in its set (most recent last)


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def check_ids(from_id: str, to_id: str) -> None:
    for tid in (from_id, to_id):
        if not _ID.match(tid):
            raise ApiError("invalid track id")
    if from_id == to_id:
        raise ApiError("a transition needs two different tracks")


class MixService:
    def __init__(self, crates: CrateService, bus: EventBus, cache_dir: Any) -> None:
        self.crates = crates
        self.bus = bus
        self._cache_dir = cache_dir  # () -> Path
        self._pool = ThreadPoolExecutor(max_workers=RENDER_WORKERS, thread_name_prefix="seedbox-render")
        self._lock = threading.Lock()
        self._inflight: dict[str, Future] = {}
        self._chains: dict[tuple, list[str | None]] = {}

    @property
    def renders_dir(self) -> Path:
        d = Path(self._cache_dir()) / "renders"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --- plans ----------------------------------------------------------------------------------------------------

    def pair(self, folder: str, from_id: str, to_id: str, chain: bool = True) -> Pair:
        """A pair of one crate's tracks; when it's consecutive in the crate's set order (and `chain`), it carries
        the styles of the transitions before it."""
        check_ids(from_id, to_id)
        seed_dir = resolve_crate(self.crates.root, folder)
        pair = Pair(seed_dir.name, seed_dir, self.crates.file(seed_dir, from_id), seed_dir,
                    self.crates.file(seed_dir, to_id), self.saved_path(seed_dir, from_id, to_id))
        if chain:
            files = self.crates.files(seed_dir)
            ids = [f.track.id for f in files]
            k = ids.index(from_id)
            if k + 1 < len(ids) and ids[k + 1] == to_id:
                pair.recent = self.with_chain(self.crate_pairs(seed_dir, files))[k].recent
        return pair

    def crate_pairs(self, seed_dir: Path, files: list[SetFile]) -> list[Pair]:
        """The Pair of every consecutive transition of a crate's set order (no chain yet: see with_chain)."""
        return [Pair(seed_dir.name, seed_dir, a, seed_dir, b, self.saved_path(seed_dir, a.track.id, b.track.id))
                for a, b in zip(files, files[1:])]

    @staticmethod
    def saved_path(seed_dir: Path, from_id: str, to_id: str) -> Path:
        return seed_dir / ".seedbox" / "mixplans" / f"{from_id}__{to_id}.json"

    def plan(self, folder: str, from_id: str, to_id: str, technique: str | None = None, bars: int | None = None,
             style: str | None = None, swap: str | None = None) -> dict:
        """Saved edits (when no technique/bars/style/swap are asked for), else a freshly generated MixPlan."""
        return self.plan_pair(self.pair(folder, from_id, to_id), technique, bars, style=style, swap=swap)

    def plan_pair(self, pair: Pair, technique: str | None = None, bars: int | None = None,
                  wait_s: float | None = None, style: str | None = None, swap: str | None = None) -> dict:
        if technique is None and bars is None and style is None and swap is None \
                and (saved := self._load_saved(pair.saved)) is not None:
            return saved
        return self._generate(pair, technique, bars, wait_s, style, swap)

    def _generate(self, pair: Pair, technique: str | None, bars: int | None, wait_s: float | None = None,
                  style: str | None = None, swap: str | None = None) -> dict:
        if technique is not None and technique not in TECHNIQUES:
            raise ApiError(f"unknown technique {technique!r}", 422)
        if bars is not None and bars not in BARS:
            raise ApiError("bars must be 4, 8, 16 or 32", 422)
        mixplan = _module("mixplan")
        if style is not None and style not in getattr(mixplan, "STYLES", ()):
            raise ApiError(f"unknown style {style!r}", 422)
        self.crates.want_analyses(pair.a_dir, [pair.a])  # both start now, so a 409 retry finds both done
        self.crates.want_analyses(pair.b_dir, [pair.b])
        self.crates.want_stems(pair.a_dir, [pair.a], front=True)  # songs in use split first
        self.crates.want_stems(pair.b_dir, [pair.b], front=True)
        a_an = self.crates.ready_analysis(pair.a_dir, pair.a, wait_s)
        b_an = self.crates.ready_analysis(pair.b_dir, pair.b, wait_s)
        a, b = pair.a, pair.b
        transition = transitions.suggest(a.track, a_an, b.track, b_an, bpm_tol=self.crates.bpm_tol)
        extra = {"style": style, "recent": pair.recent} if engine.accepts(mixplan.generate, "recent") else {}
        if swap is not None:
            if swap not in getattr(mixplan, "SWAP_KINDS", ()):
                raise ApiError(f"unknown bass swap {swap!r}", 422)
            extra["swap"] = swap
        try:
            plan = mixplan.generate(a.track, a_an, b.track, b_an, pair.key, technique=technique, bars=bars,
                                    transition=transition, **extra)  # fmt: skip
        except ValueError as e:
            raise ApiError(f"can't plan this transition: {e}", 422) from e
        return {**plan, "edited": False}

    # --- style chains ---------------------------------------------------------------------------------------------

    def crate_styles(self, folder: str) -> list[str | None]:
        """The planned style of each consecutive transition of a crate's set order (None: can't be planned)."""
        seed_dir = resolve_crate(self.crates.root, folder)
        return self.chain(self.crate_pairs(seed_dir, self.crates.files(seed_dir)))

    def with_chain(self, pairs: list[Pair]) -> list[Pair]:
        """Set each pair's `recent` (the styles of the transitions before it) from the set's chain; `pairs` are a
        set's consecutive transitions in play order."""
        styles = self.chain(pairs)
        for k, p in enumerate(pairs):
            p.recent = [s for s in styles[max(0, k - CHAIN_RECENT) : k] if s]
        return pairs

    def chain(self, pairs: list[Pair]) -> list[str | None]:
        """The style of every transition of a set (mixplan.plan_styles): a saved plan keeps its own, the others are
        each the best fit for their two songs given the styles before them. Uses the analyses known now (nothing
        waits; missing ones are queued) and is remembered until the order, an analysis or a saved plan changes."""
        mixplan = engine.optional("mixplan")
        if mixplan is None or not hasattr(mixplan, "plan_styles") or not pairs:
            return [None] * len(pairs)
        known: dict[tuple[str, str], dict | None] = {}
        for d in dict.fromkeys(x for p in pairs for x in (p.a_dir, p.b_dir)):
            files = list({f.track.id: f for p in pairs for dd, f in ((p.a_dir, p.a), (p.b_dir, p.b)) if dd == d}.values())
            for tid, an in self.crates.analyses_now(d, files).items():
                known[str(d), tid] = an

        def an_of(d: Path, f: SetFile) -> dict | None:
            return known.get((str(d), f.track.id))

        saved = [_mtime(p.saved) for p in pairs]
        key = (tuple((str(p.a_dir), p.a.track.id, str(p.b_dir), p.b.track.id, str(p.saved)) for p in pairs),
               tuple(saved), tuple(an_of(p.a_dir, p.a) is not None for p in pairs) + (an_of(pairs[-1].b_dir,
               pairs[-1].b) is not None,), self.crates.bpm_tol)  # fmt: skip
        with self._lock:
            hit = self._chains.get(key)
        if hit is not None:
            return hit
        fixed = [((self._load_saved(p.saved) or {}).get("style") if mt else None) for p, mt in zip(pairs, saved)]
        rows = []
        for p in pairs:
            a_an, b_an = an_of(p.a_dir, p.a), an_of(p.b_dir, p.b)
            tr = transitions.suggest(p.a.track, a_an, p.b.track, b_an, bpm_tol=self.crates.bpm_tol)
            rows.append((p.a.track, a_an, p.b.track, b_an, tr))
        try:
            out = list(mixplan.plan_styles(rows, fixed))
        except Exception:  # noqa: BLE001 - every plan still works without the chain
            log.exception("planning the style chain failed")
            out = [None] * len(pairs)
        with self._lock:
            self._chains[key] = out
            while len(self._chains) > CHAIN_CACHE:
                self._chains.pop(next(iter(self._chains)))
        return out

    def _load_saved(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable saved mix plan %s: %s", path, e)
            return None
        try:
            plan = _module("mixplan").validate(data)
        except ValueError as e:  # e.g. written by an older PLAN_VERSION
            log.warning("ignoring invalid saved mix plan %s: %s", path.name, e)
            return None
        return {**plan, "edited": True}

    def _validated(self, body: Any, folder: str, from_id: str, to_id: str) -> dict:
        if not isinstance(body, dict):
            raise ApiError("a MixPlan JSON object is required", 422)
        try:
            plan = _module("mixplan").validate(body)
        except ValueError as e:
            raise ApiError(f"invalid mix plan: {e}", 422) from e
        ids = (plan.get("from_id"), plan.get("to_id"), (plan.get("a") or {}).get("track_id"),
               (plan.get("b") or {}).get("track_id"))  # fmt: skip
        if ids != (from_id, to_id, from_id, to_id):
            raise ApiError("this mix plan is for a different pair of tracks", 422)
        return {**plan, "folder": folder}

    def save(self, folder: str, from_id: str, to_id: str, body: Any) -> dict:
        return self.save_pair(self.pair(folder, from_id, to_id), body)

    def save_pair(self, pair: Pair, body: Any) -> dict:
        plan = {**self._validated(body, pair.key, pair.a.track.id, pair.b.track.id), "edited": True}
        _write_json_atomic(pair.saved, plan)
        return plan

    def delete(self, folder: str, from_id: str, to_id: str) -> dict:
        return self.delete_pair(self.pair(folder, from_id, to_id))

    def delete_pair(self, pair: Pair) -> dict:
        pair.saved.unlink(missing_ok=True)
        return self._generate(pair, None, None)

    # --- renders --------------------------------------------------------------------------------------------------

    def render(self, folder: str, from_id: str, to_id: str, body: Any = None) -> dict:
        """MixRender for the saved/generated plan, or for `body` (unsaved edits). Blocks until rendered."""
        return self.render_pair(self.pair(folder, from_id, to_id), body)

    def render_pair(self, pair: Pair, body: Any = None) -> dict:
        a, b = pair.a, pair.b
        from_id, to_id = a.track.id, b.track.id
        if body:  # the client's post() sends {} when there is nothing to send
            plan = self._validated(body, pair.key, from_id, to_id)
        else:
            plan = self.plan_pair(pair)
        mixrender = _module("mixrender")
        stems = {"a": self.crates.vocal_path(pair.a_dir, a), "b": self.crates.vocal_path(pair.b_dir, b)}
        use_stems = engine.accepts(mixrender.render, "stems")
        render_id = str(mixrender.plan_hash(plan, a.path, b.path, stems) if use_stems
                        else mixrender.plan_hash(plan, a.path, b.path))
        if not _RENDER_ID.match(render_id):
            raise ApiError("renderer returned an unusable id", 500)
        if (hit := self._cached(render_id)) is not None:
            return hit
        with self._lock:
            fut = self._inflight.get(render_id)
            if fut is None:
                if (hit := self._cached(render_id)) is not None:  # finished between the check above and now
                    return hit
                fut = self._pool.submit(self._render, mixrender, render_id, plan, a.path, b.path, pair.key,
                                        from_id, to_id, stems if use_stems else None)  # fmt: skip
                self._inflight[render_id] = fut
        try:
            return fut.result()
        except ValueError as e:
            raise ApiError(f"can't render this plan: {e}", 422) from e
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"render failed: {e}", 500) from e

    def audio_path(self, render_id: str) -> Path:
        if not _RENDER_ID.match(render_id):
            raise NotFound("no such render")
        path = self.renders_dir / f"{render_id}.m4a"
        if not path.is_file():
            raise NotFound("no such render (expired from the cache?)")
        self._touch(path)
        return path

    def _cached(self, render_id: str) -> dict | None:
        audio, meta = self.renders_dir / f"{render_id}.m4a", self.renders_dir / f"{render_id}.json"
        if not (audio.is_file() and meta.is_file()):
            return None
        try:
            data = json.loads(meta.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        self._touch(audio, meta)
        return data

    @staticmethod
    def _touch(*paths: Path) -> None:
        now = time.time()
        for p in paths:
            try:
                os.utime(p, (now, now))
            except OSError:
                pass

    def busy(self) -> bool:
        """A transition is being rendered right now."""
        with self._lock:
            return bool(self._inflight)

    def _render(self, mixrender: Any, render_id: str, plan: dict, a_path: Path, b_path: Path, folder: str,
                from_id: str, to_id: str, stems: dict | None = None) -> dict:  # fmt: skip
        event = {"render_id": render_id, "folder": folder, "from_id": from_id, "to_id": to_id}
        last = [0.0]

        def progress(fraction: float) -> None:
            fraction = max(0.0, min(1.0, float(fraction)))
            if fraction - last[0] >= PROGRESS_STEP:
                last[0] = fraction
                self.bus.publish("render", {**event, "state": "running", "progress": round(fraction, 3)})

        self.bus.publish("render", {**event, "state": "running", "progress": 0.0})
        out_dir = self.renders_dir
        tmp = out_dir / f".{render_id}.{uuid.uuid4().hex[:8]}.part.m4a"
        try:
            kwargs: dict[str, Any] = {"stems": stems} if stems is not None else {}
            for name in ("progress", "on_progress"):
                if engine.accepts(mixrender.render, name):
                    kwargs[name] = progress
                    break
            result = dict(mixrender.render(plan, a_path, b_path, tmp, **kwargs))
            os.replace(tmp, out_dir / f"{render_id}.m4a")
            result.update(render_id=render_id, audio_url=f"/api/renders/{render_id}.m4a")
            result = serializers.clean(result)
            _write_json_atomic(out_dir / f"{render_id}.json", result)
            self.bus.publish("render", {**event, "state": "done", "progress": 1.0})
            return result
        except Exception as e:
            log.warning("render %s failed: %s", render_id, e)
            self.bus.publish("render", {**event, "state": "error", "progress": last[0], "error": str(e) or type(e).__name__})
            raise
        finally:
            tmp.unlink(missing_ok=True)
            with self._lock:
                self._inflight.pop(render_id, None)
            try:
                self._trim()
            except Exception:  # noqa: BLE001
                log.exception("render cache trim failed")

    def _trim(self, limit_mb: int = RENDER_CACHE_MB) -> None:
        """Delete least-recently-used renders until the folder is under `limit_mb` (never one being rendered)."""
        entries: dict[str, list[tuple[Path, os.stat_result]]] = {}
        for p in self.renders_dir.iterdir():
            if p.name.startswith("."):  # a .part file from a render that crashed / a killed app
                try:
                    if time.time() - p.stat().st_mtime > 3600:
                        p.unlink()
                except OSError:
                    pass
                continue
            if p.suffix not in (".m4a", ".json"):
                continue
            try:
                entries.setdefault(p.stem, []).append((p, p.stat()))
            except OSError:
                pass
        total = sum(st.st_size for files in entries.values() for _, st in files)
        limit = limit_mb * 1024 * 1024
        if total <= limit:
            return
        with self._lock:
            busy = set(self._inflight)
        for stem, files in sorted(entries.items(), key=lambda kv: max(st.st_mtime for _, st in kv[1])):
            if total <= limit:
                break
            if stem in busy:
                continue
            for p, st in files:
                p.unlink(missing_ok=True)
                total -= st.st_size

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
