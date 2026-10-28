"""v4 mix plans and rendered transitions: generate / save / render the MixPlan for one A -> B pair of a crate.

Saved edits: <crate>/.seedbox/mixplans/<from>__<to>.json (atomic). Renders: <cache>/renders/<hash>.m4a + .json,
keyed by mixrender.plan_hash, at most RENDER_WORKERS at a time; identical requests share one render; the folder
is trimmed least-recently-used first to RENDER_CACHE_MB.
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
from pathlib import Path
from typing import Any

from musicreco import mixplan, mixrender, transitions

from . import serializers
from .crates import ApiError, CrateService, NotFound, SetFile, resolve_crate
from .events import EventBus

log = logging.getLogger(__name__)

RENDER_WORKERS = 2
RENDER_CACHE_MB = 1024
PROGRESS_STEP = 0.05  # publish render progress in steps of at least this much
TECHNIQUES = ("long blend", "phrase blend", "bass swap", "drop swap", "quick cut", "echo out", "filter sweep",
              "loop roll", "slam", "reverb wash")  # fmt: skip
BARS = (4, 8, 16, 32)
_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_RENDER_ID = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


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


class MixService:
    def __init__(self, crates: CrateService, bus: EventBus, cache_dir: Any) -> None:
        self.crates = crates
        self.bus = bus
        self._cache_dir = cache_dir  # () -> Path
        self._pool = ThreadPoolExecutor(max_workers=RENDER_WORKERS, thread_name_prefix="seedbox-render")
        self._lock = threading.Lock()
        self._inflight: dict[str, Future] = {}

    @property
    def renders_dir(self) -> Path:
        d = Path(self._cache_dir()) / "renders"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --- plans ----------------------------------------------------------------------------------------------------

    def _pair(self, folder: str, from_id: str, to_id: str) -> tuple[Path, SetFile, SetFile]:
        for tid in (from_id, to_id):
            if not _ID.match(tid):
                raise ApiError("invalid track id")
        if from_id == to_id:
            raise ApiError("a transition needs two different tracks")
        seed_dir = resolve_crate(self.crates.root, folder)
        return seed_dir, self.crates.file(seed_dir, from_id), self.crates.file(seed_dir, to_id)

    @staticmethod
    def saved_path(seed_dir: Path, from_id: str, to_id: str) -> Path:
        return seed_dir / ".seedbox" / "mixplans" / f"{from_id}__{to_id}.json"

    def plan(self, folder: str, from_id: str, to_id: str, technique: str | None = None, bars: int | None = None) -> dict:
        """Saved edits (when no technique/bars are asked for), else a freshly generated MixPlan."""
        seed_dir, a, b = self._pair(folder, from_id, to_id)
        if technique is None and bars is None and (saved := self._load_saved(seed_dir, from_id, to_id)) is not None:
            return saved
        return self._generate(seed_dir, a, b, technique, bars)

    def _generate(self, seed_dir: Path, a: SetFile, b: SetFile, technique: str | None, bars: int | None) -> dict:
        if technique is not None and technique not in TECHNIQUES:
            raise ApiError(f"unknown technique {technique!r}", 422)
        if bars is not None and bars not in BARS:
            raise ApiError("bars must be 4, 8, 16 or 32", 422)
        self.crates.want_analyses(seed_dir, [a, b])  # both start now, so a 409 retry finds both done
        a_an = self.crates.ready_analysis(seed_dir, a)
        b_an = self.crates.ready_analysis(seed_dir, b)
        mix_length = self.crates.mix_length
        transition = transitions.suggest(a.track, a_an, b.track, b_an, bpm_tol=self.crates.bpm_tol,
                                         mix_length=mix_length)  # fmt: skip
        if technique is None:  # the set plan's pick when B follows A in the set, so the set builder's card matches
            technique = self.crates.set_technique(seed_dir, a.track.id, b.track.id)
        try:
            plan = mixplan.generate(a.track, a_an, b.track, b_an, seed_dir.name, technique=technique, bars=bars,
                                    transition=transition, mix_length=mix_length)  # fmt: skip
        except ValueError as e:
            raise ApiError(f"can't plan this transition: {e}", 422) from e
        return {**plan, "edited": False}

    def _load_saved(self, seed_dir: Path, from_id: str, to_id: str) -> dict | None:
        path = self.saved_path(seed_dir, from_id, to_id)
        try:
            data = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable saved mix plan %s: %s", path, e)
            return None
        try:
            plan = mixplan.validate(data)
        except ValueError as e:  # e.g. written by an older PLAN_VERSION
            log.warning("ignoring invalid saved mix plan %s: %s", path.name, e)
            return None
        return {**plan, "edited": True}

    def _validated(self, body: Any, folder: str, from_id: str, to_id: str) -> dict:
        if not isinstance(body, dict):
            raise ApiError("a MixPlan JSON object is required", 422)
        try:
            plan = mixplan.validate(body)
        except ValueError as e:
            raise ApiError(f"invalid mix plan: {e}", 422) from e
        ids = (plan.get("from_id"), plan.get("to_id"), (plan.get("a") or {}).get("track_id"),
               (plan.get("b") or {}).get("track_id"))  # fmt: skip
        if ids != (from_id, to_id, from_id, to_id):
            raise ApiError("this mix plan is for a different pair of tracks", 422)
        return {**plan, "folder": folder}

    def save(self, folder: str, from_id: str, to_id: str, body: Any) -> dict:
        seed_dir, _a, _b = self._pair(folder, from_id, to_id)
        plan = {**self._validated(body, seed_dir.name, from_id, to_id), "edited": True}
        _write_json_atomic(self.saved_path(seed_dir, from_id, to_id), plan)
        return plan

    def delete(self, folder: str, from_id: str, to_id: str) -> dict:
        seed_dir, a, b = self._pair(folder, from_id, to_id)
        self.saved_path(seed_dir, from_id, to_id).unlink(missing_ok=True)
        return self._generate(seed_dir, a, b, None, None)

    # --- renders --------------------------------------------------------------------------------------------------

    def render(self, folder: str, from_id: str, to_id: str, body: Any = None) -> dict:
        """MixRender for the saved/generated plan, or for `body` (unsaved edits). Blocks until rendered."""
        seed_dir, a, b = self._pair(folder, from_id, to_id)
        if body:  # the client's post() sends {} when there is nothing to send
            plan = self._validated(body, seed_dir.name, from_id, to_id)
        else:
            plan = self.plan(folder, from_id, to_id)
        render_id = str(mixrender.plan_hash(plan, a.path, b.path))
        if not _RENDER_ID.match(render_id):
            raise ApiError("renderer returned an unusable id", 500)
        if (hit := self._cached(render_id)) is not None:
            return hit
        with self._lock:
            fut = self._inflight.get(render_id)
            if fut is None:
                if (hit := self._cached(render_id)) is not None:  # finished between the check above and now
                    return hit
                fut = self._pool.submit(self._render, render_id, plan, a.path, b.path, seed_dir.name,
                                        from_id, to_id)  # fmt: skip
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

    def _render(self, render_id: str, plan: dict, a_path: Path, b_path: Path, folder: str,
                from_id: str, to_id: str) -> dict:  # fmt: skip
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
            result = dict(mixrender.render(plan, a_path, b_path, tmp, progress=progress))
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
