"""Full set mixes (musicreco.setmix) as background jobs: one render at a time, progress as `setmix` events, results
in <root>/Mixes/<name>/ (MP3 + .cue + .txt) and an index (<root>/.seedbox/setmixes.json) so each crate's and
setlist's last mix survives restarts.

A job waits for every track's analysis, takes each transition's saved edits or a freshly generated plan, shortens
generated blends that leave a track too short for both of its transitions, and skips the render altogether when
nothing changed since the last mix of the same set (same plans, same files).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from musicreco import library

from . import engine, serializers
from .crates import ApiError, Conflict, CrateService, NotFound, SetFile, inside, resolve_crate
from .events import EventBus
from .mixes import MixService, Pair, _write_json_atomic

log = logging.getLogger(__name__)

ANALYSIS_WAIT_S = 900.0
PLAN_WAIT_S = 60.0
INDEX_KEEP = 100
PROGRESS_STEP = 0.01
_MIX_ID = re.compile(r"^[a-f0-9]{8,32}$")
_SHORTER = {32: 16, 16: 8, 8: 4}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SetMixJob:
    def __init__(self, target: str, title: str) -> None:
        self.mix_id = uuid.uuid4().hex[:12]
        self.target = target
        self.title = title
        self.state = "queued"
        self.progress = 0.0
        self.message = "Waiting for another mix to finish"
        self.error: str | None = None
        self.started_at = _now()
        self.finished_at: str | None = None
        self.result: dict | None = None
        self.track_ids: list[str] = []
        self.split: list[str] = []  # tracks whose vocals were split when it rendered (more later = worth re-rendering)
        self.hash: str | None = None
        self.cancel = threading.Event()

    def to_json(self) -> dict:
        return {
            "mix_id": self.mix_id, "target": self.target, "title": self.title, "state": self.state,
            "progress": round(self.progress, 4), "message": self.message, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at, "result": self.result,
        }  # fmt: skip

    @classmethod
    def from_json(cls, d: dict) -> SetMixJob:
        job = cls(str(d.get("target", "")), str(d.get("title", "")))
        job.mix_id = str(d.get("mix_id") or job.mix_id)
        job.state, job.progress, job.message = "done", 1.0, str(d.get("message") or "Done")
        job.started_at = str(d.get("started_at") or "")
        job.finished_at = d.get("finished_at")
        job.result = d.get("result") if isinstance(d.get("result"), dict) else None
        job.track_ids = [t for t in d.get("track_ids") or [] if isinstance(t, str)]
        job.split = [t for t in d.get("split") or [] if isinstance(t, str)]
        job.hash = d.get("hash")
        return job


def _safe(name: str) -> str:
    return library.sanitize_filename(name)[:80].strip() or "Set"


class SetMixService:
    def __init__(self, crates: CrateService, mixes: MixService, setlists: Any, bus: EventBus) -> None:
        self.crates = crates
        self.mixes = mixes
        self.setlists = setlists
        self.bus = bus
        self._lock = threading.RLock()
        self._jobs: dict[str, SetMixJob] = {}
        self._queue: queue.SimpleQueue[SetMixJob | None] = queue.SimpleQueue()
        self._worker: threading.Thread | None = None

    # --- targets ---------------------------------------------------------------------------------------------------

    def _resolve(self, target: str, chain: bool = False) -> tuple[str, list[tuple[Path, SetFile]], list[Pair]]:
        """Title, play order and transitions of a target; with `chain`, each transition knows the styles of the
        ones before it (what its generated plan needs)."""
        kind, _, key = target.partition(":")
        if kind == "crate":
            seed_dir = resolve_crate(self.crates.root, key)
            files = self.crates.files(seed_dir)
            seed = library.load_crate_seed(seed_dir)
            pairs = self.mixes.crate_pairs(seed_dir, files)
            if chain:
                self.mixes.with_chain(pairs)
            return (seed.display_name if seed else seed_dir.name), [(seed_dir, f) for f in files], pairs
        if kind == "setlist":
            seq, pairs = self.setlists.pairs_for_mix(key, chain=chain)
            return self.setlists.title(key), seq, pairs
        raise ApiError("unknown mix target", 422)

    def _sequence_ids(self, target: str) -> list[str]:
        try:
            _t, seq, _p = self._resolve(target)
        except ApiError:
            return []
        return [f.track.id for _, f in seq]

    @property
    def _index_path(self) -> Path:
        return self.crates.root / ".seedbox" / "setmixes.json"

    def _index(self) -> list[dict]:
        try:
            data = json.loads(self._index_path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in data.get("mixes", []) if isinstance(d, dict)] if isinstance(data, dict) else []

    def _remember(self, job: SetMixJob) -> None:
        entry = {**job.to_json(), "track_ids": job.track_ids, "split": job.split, "hash": job.hash}
        with self._lock:
            mixes = [m for m in self._index() if m.get("mix_id") != job.mix_id]
            mixes.insert(0, entry)
            try:
                _write_json_atomic(self._index_path, {"mixes": mixes[:INDEX_KEEP]})
            except OSError as e:
                log.warning("couldn't write the set mix index: %s", e)

    def _latest_done(self, target: str) -> SetMixJob | None:
        with self._lock:
            mem = [j for j in self._jobs.values() if j.target == target and j.state == "done"]
        cands = sorted(mem, key=lambda j: j.finished_at or "", reverse=True)
        cands += [SetMixJob.from_json(d) for d in self._index() if d.get("target") == target]
        for j in cands:
            p = Path((j.result or {}).get("path") or "")
            if p.is_file() and inside(p, self.crates.root):
                return j
        return None

    # --- public ----------------------------------------------------------------------------------------------------

    def start(self, target: str) -> dict:
        title, seq, _pairs = self._resolve(target)
        if len(seq) < 2:
            raise ApiError("a set mix needs at least two tracks", 422)
        if engine.optional("setmix") is None:
            raise ApiError("set mixes aren't available in this build", 503)
        with self._lock:
            for j in self._jobs.values():
                if j.target == target and j.state in ("queued", "preparing", "rendering"):
                    return j.to_json()
            job = SetMixJob(target, title)
            self._jobs[job.mix_id] = job
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="seedbox-setmix", daemon=True)
                self._worker.start()
        self._queue.put(job)
        self._publish(job)
        return job.to_json()

    def current(self, target: str) -> dict:
        """{mix, stale}: the running / queued mix of this set, else its last finished one; stale when the running
        order or a saved transition changed since it was rendered."""
        self._resolve(target)  # 404 for unknown crates / setlists
        with self._lock:
            live = [j for j in self._jobs.values() if j.target == target and j.state in ("queued", "preparing",
                                                                                       "rendering")]
            failed = sorted((j for j in self._jobs.values() if j.target == target and j.state in ("error", "cancelled")),
                            key=lambda j: j.started_at, reverse=True)
        if live:
            return {"mix": live[0].to_json(), "stale": False}
        done = self._latest_done(target)
        if failed and (done is None or (failed[0].started_at > (done.finished_at or ""))):
            return {"mix": failed[0].to_json(), "stale": False}
        if done is None:
            return {"mix": None, "stale": False}
        return {"mix": done.to_json(), "stale": self._stale(target, done)}

    def _stale(self, target: str, job: SetMixJob) -> bool:
        if job.track_ids and job.track_ids != self._sequence_ids(target):
            return True
        try:
            _t, seq, pairs = self._resolve(target)
        except ApiError:
            return True
        finished = job.finished_at or ""
        for p in pairs:
            try:
                mt = datetime.fromtimestamp(p.saved.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            except OSError:
                continue
            if mt > finished:
                return True
        # songs whose vocals got split since: their transitions now hand the vocals over cleanly
        return any(f.track.id not in job.split and self.crates.vocal_path(d, f) is not None for d, f in seq)

    def get(self, mix_id: str) -> dict:
        return self._job(mix_id).to_json()

    def _job(self, mix_id: str) -> SetMixJob:
        if not _MIX_ID.match(mix_id):
            raise NotFound("no such mix")
        with self._lock:
            if mix_id in self._jobs:
                return self._jobs[mix_id]
        for d in self._index():
            if d.get("mix_id") == mix_id:
                return SetMixJob.from_json(d)
        raise NotFound("no such mix")

    def cancel(self, mix_id: str) -> dict:
        job = self._job(mix_id)
        if job.state in ("queued", "preparing", "rendering"):
            job.cancel.set()
            if job.state == "queued":
                self._finish(job, "cancelled", "Cancelled")
        return job.to_json()

    def audio_path(self, mix_id: str) -> Path:
        job = self._job(mix_id)
        p = Path((job.result or {}).get("path") or "")
        if not p.is_file() or not inside(p, self.crates.root):
            raise NotFound("the mix file is gone (moved or deleted?)")
        return p

    def busy(self) -> bool:
        """A set mix is being prepared or rendered."""
        with self._lock:
            return any(j.state in ("preparing", "rendering") for j in self._jobs.values())

    def shutdown(self) -> None:
        with self._lock:
            for j in self._jobs.values():
                j.cancel.set()
        self._queue.put(None)

    # --- the worker ------------------------------------------------------------------------------------------------

    def _publish(self, job: SetMixJob) -> None:
        self.bus.publish("setmix", job.to_json())

    def _set(self, job: SetMixJob, state: str | None = None, progress: float | None = None,
             message: str | None = None) -> None:
        changed = False
        if state is not None and state != job.state:
            job.state, changed = state, True
        if message is not None and message != job.message:
            job.message, changed = message, True
        if progress is not None and (abs(progress - job.progress) >= PROGRESS_STEP or progress in (0.0, 1.0)):
            job.progress, changed = progress, True
        if changed:
            self._publish(job)

    def _finish(self, job: SetMixJob, state: str, message: str, error: str | None = None) -> None:
        job.state, job.message, job.error, job.finished_at = state, message, error, _now()
        if state == "done":
            job.progress = 1.0
        self._publish(job)

    def _loop(self) -> None:
        while (job := self._queue.get()) is not None:
            if job.state != "queued":
                continue
            try:
                self._run(job)
            except Exception as e:  # noqa: BLE001
                setmix = engine.optional("setmix")
                if setmix is not None and isinstance(e, setmix.Cancelled) or job.cancel.is_set():
                    self._finish(job, "cancelled", "Cancelled")
                else:
                    log.exception("set mix %s failed", job.mix_id)
                    detail = e.detail if isinstance(e, ApiError) else str(e) or type(e).__name__
                    self._finish(job, "error", "Failed", detail)

    def _check(self, job: SetMixJob) -> None:
        if job.cancel.is_set():
            raise engine.optional("setmix").Cancelled()

    def _shorter(self, pair: Pair, p: dict, mixplan: Any) -> dict | None:
        """A shorter generated plan for a transition that leaves its track too short: the same technique (and blend
        style) at the next length down, else a phrase blend of that length (a double drop is always 16 bars), else
        a quick cut. None when nothing comes out shorter."""
        shorter = _SHORTER.get(int(p["bars"]))
        tries: list[tuple[str, int, str | None]] = []
        if shorter:
            style = p.get("style") if p.get("style") in getattr(mixplan, "BLEND_STYLES", ()) else None
            tries.append((p["technique"], shorter, style))
            if p["technique"] not in ("long blend", "phrase blend", "bass swap"):
                tries.append(("phrase blend", shorter, None))
        tries.append(("quick cut", 4, None))
        for tech, bars, style in tries:
            q = self.mixes.plan_pair(pair, technique=tech, bars=bars, wait_s=PLAN_WAIT_S, style=style)
            if q["technique"] != p["technique"] or int(q["bars"]) < int(p["bars"]):
                return q
        return None

    def _run(self, job: SetMixJob) -> None:
        setmix = engine.optional("setmix")
        mixplan = engine.optional("mixplan")
        self._set(job, "preparing", 0.0, "Collecting the tracks")
        title, seq, pairs = self._resolve(job.target)  # the chain comes once every track is analysed
        job.title = title
        job.track_ids = [f.track.id for _, f in seq]
        n = len(seq)
        if n < 2:
            raise ApiError("a set mix needs at least two tracks")

        # every track analysed (front of the queue), waiting in short steps so a cancel lands quickly; their vocals
        # are split at the front of the stems queue too (the render uses the stems that are ready by then)
        for d in dict.fromkeys(d for d, _ in seq):
            self.crates.want_analyses(d, [f for dd, f in seq if dd == d])
        for d, f in reversed(seq):  # in play order at the very front
            self.crates.want_stems(d, [f], front=True)
        analyses: dict[str, dict] = {}
        deadline = time.monotonic() + ANALYSIS_WAIT_S
        for k, (d, f) in enumerate(seq):
            self._set(job, progress=0.02 * k / n, message=f"Analysing {k + 1}/{n} · {f.track.display_name}")
            while True:
                self._check(job)
                try:
                    analyses[f.track.id] = self.crates.ready_analysis(d, f, wait_s=3.0)
                    break
                except Conflict:
                    if time.monotonic() > deadline:
                        raise ApiError(f"{f.track.display_name} is still being analysed, try again shortly") from None

        # plans: saved edits or generated (each knowing the styles of the transitions before it)
        self._set(job, progress=0.02, message="Choosing the transitions")
        self.mixes.with_chain(pairs)
        plans = []
        for k, p in enumerate(pairs):
            self._check(job)
            self._set(job, progress=0.02 + 0.02 * k / max(1, len(pairs)),
                      message=f"Planning transition {k + 1}/{len(pairs)}")
            plans.append(self.mixes.plan_pair(p, wait_s=PLAN_WAIT_S))
        entries = [setmix.SetEntry(f.track, f.path, analyses.get(f.track.id), d.name, self.crates.vocal_path(d, f))
                   for d, f in seq]
        unsplit = sum(1 for e in entries if e.vocal_path is None)
        job.split = [e.track.id for e in entries if e.vocal_path is not None]
        notes: list[str] = []
        for _round in range(6):
            tl = setmix.build(entries, plans)
            if not tl.conflicts:
                break
            fixed = False
            for i in tl.conflicts:
                for j in (i, i - 1):  # shorten the track's out-transition first, then its in-transition
                    p = plans[j]
                    if j < 0 or p.get("edited") or p["technique"] in mixplan._CUTS:
                        continue
                    short = self._shorter(pairs[j], p, mixplan)
                    if short is None:
                        continue
                    plans[j] = short
                    notes.append(f"transition {j + 1} shortened to fit ({short['technique']}, {short['bars']} bars)")
                    fixed = True
                    break
            if not fixed:
                break

        job.hash = setmix.set_hash(entries, plans)
        prev = self._latest_done(job.target)
        if prev is not None and prev.hash == job.hash and prev.result:
            job.result = prev.result
            self._finish(job, "done", "Unchanged since the last render")
            self._remember(job)
            return

        out_dir = self.crates.root / library.MIXES_DIRNAME / _safe(title)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{_safe(title)} - {datetime.now().strftime('%Y-%m-%d %H%M')}"
        tmp = out_dir / f".{stem}.{job.mix_id}.part"
        self._set(job, "rendering", 0.05, "Rendering")

        def progress(f: float, msg: str) -> None:
            self._set(job, progress=0.05 + 0.95 * f, message=msg)

        try:
            r = setmix.render(entries, plans, tmp, title=title, progress=progress, cancel=job.cancel)
            final = out_dir / f"{stem}.{r['format']}"
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        cue, txt = setmix.write_sidecars(r, title, final)
        if unsplit and self.crates.stems is not None and self.crates.stems.available()[0]:
            notes.append(f"{unsplit} of {n} songs didn't have their vocals split yet, so their transitions handle "
                         "vocals with the EQ; render again once splitting is done for cleaner vocal handoffs")
        r["warnings"] = notes + list(r.get("warnings") or [])
        job.result = serializers.clean({**r, "audio_url": f"/api/setmix/{job.mix_id}/audio", "path": str(final),
                                        "cue_path": str(cue), "txt_path": str(txt)})
        self._finish(job, "done", "Done")
        self._remember(job)
