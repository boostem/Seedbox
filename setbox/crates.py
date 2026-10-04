"""Setbox's crate operations: CrateSet / Partner / TrackAnalysis JSON and set reordering, on the crate folders
(seedbox.crates: path containment, files, covers). Strict path containment."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from musicreco import library, mixing, tagging, transitions
from seedbox import engine, serializers
from seedbox.crates import (  # noqa: F401 - re-exported for the set modules
    AUDIO_SUFFIXES,
    ApiError,
    Conflict,
    NotFound,
    SetFile,
    _signature,
    audio_url,
    cover_url,
    crate_cover,
    embedded_art,
    file_of,
    inside,
    resolve_crate,
    resolve_crate_file,
    scan_crate,
    summary_seed,
    track_json,
    with_cover,
)
from seedbox.events import EventBus

from .analysis_queue import AnalysisQueue, AnalysisTask, disk_cache_path

log = logging.getLogger(__name__)

SYNC_LOAD_BUDGET_S = 3.0  # a set request may load this long from the analysis disk cache before deferring
PARTNER_LIMIT = 8
ANALYSIS_WAIT_S = 20.0  # a mix plan waits this long for a missing analysis before answering 409
SCAN_CACHE = 32  # crate folders whose scan is kept (invalidated by any file change)


class CrateService:
    """Set-builder operations on crate folders under the current output_root."""

    def __init__(
        self,
        get_settings: Callable[[], Any],
        analysis: AnalysisQueue,
        bus: EventBus,
        busy: Callable[[str], bool] | None = None,
    ) -> None:
        self.get_settings = get_settings
        self.analysis = analysis
        self.bus = bus
        # Seedbox downloads in its own process: its jobs mark the crate (library.crate_busy)
        self.busy = busy or (lambda folder: library.crate_downloading(self.root / folder))
        self._lock = threading.Lock()
        self._scans: dict[str, tuple[tuple, list[SetFile]]] = {}
        self._energy_done: set[str] = set()  # crates whose missing energy ratings were back-filled this session
        self._energy_exact: dict[str, dict[str, int]] = {}  # crate -> id -> exact 1-10 written (tags keep stars)
        self.stems: Any = None  # StemQueue (stems_queue.py), set by the server: vocal stems + stem-based vocals

    @property
    def root(self) -> Path:
        return Path(self.get_settings().output_root)

    @property
    def bpm_tol(self) -> float:
        return float(self.get_settings().bpm_tolerance)

    def files(self, seed_dir: Path) -> list[SetFile]:
        sig = _signature(seed_dir)
        key = str(seed_dir)
        with self._lock:
            hit = self._scans.get(key)
        if hit and hit[0] == sig:
            return hit[1]
        files = scan_crate(seed_dir)
        with self._lock:
            exact = self._energy_exact.get(key, {})
            for f in files:
                if f.track.id in exact:
                    f.energy_rating = exact[f.track.id]
            self._scans[key] = (sig, files)
            while len(self._scans) > SCAN_CACHE:
                self._scans.pop(next(iter(self._scans)))
        return files

    def invalidate(self, seed_dir: Path, rerated: bool = False) -> None:
        """Forget the crate's cached scan; needed after tag-only writes, which keep mtimes (and often sizes).

        `rerated`: the crate's energy ratings were rewritten (a job's finalize), so drop the back-fill's values.
        """
        with self._lock:
            self._scans.pop(str(seed_dir), None)
            if rerated:
                self._energy_exact.pop(str(seed_dir), None)

    def backfill_energy(self, seed_dir: Path, files: list[SetFile]) -> int:
        """Once per crate: rate files that have no energy rating (older crates), like finalize_crate would.

        Ratings come from mixing.energy_ratings over the crate's tracks that carry tunebat energy data and are
        written as POPM stars (tagging.set_energy) under library.crate_lock with mtimes preserved, so analysis
        caches stay valid. Existing ratings and track numbers are left alone. Returns how many files were rated.
        """
        key = str(seed_dir)
        with self._lock:
            if key in self._energy_done:
                return 0
        if self.busy(seed_dir.name):  # the running job's complete_batch rates the crate itself
            return 0
        missing = [f for f in files if f.energy_rating is None and f.track.energy > 0]
        written: dict[str, int] = {}
        if missing:
            try:
                ratings = mixing.energy_ratings([f.track for f in files if f.track.energy > 0])
            except Exception:  # noqa: BLE001
                log.exception("energy ratings failed for %s", seed_dir.name)
                ratings = {}
            with library.crate_lock(seed_dir):
                for f in missing:
                    if (rating := ratings.get(f.track.id)) is None:
                        continue
                    try:
                        with library.preserve_mtime(f.path):
                            tagging.set_energy(f.path, rating)
                    except Exception as e:  # noqa: BLE001 - a locked/odd file keeps no rating
                        log.warning("couldn't write energy to %s: %s", f.path.name, e)
                        continue
                    f.energy_rating = written[f.track.id] = rating
        with self._lock:
            self._energy_done.add(key)
            if written:
                self._energy_exact.setdefault(key, {}).update(written)
        if written:
            log.info("rated energy of %d file(s) in %s", len(written), seed_dir.name)
        return len(written)

    def task(self, seed_dir: Path, f: SetFile) -> AnalysisTask:
        return AnalysisTask(seed_dir.name, seed_dir, f.track.id, f.path, f.track.bpm or None)

    def queue_analysis(self, seed_dir: Path, front: bool = False) -> None:
        """Queue every file of the crate that hasn't been analysed this session."""
        try:
            files = self.files(seed_dir)
        except OSError:
            return
        self.analysis.enqueue([self.task(seed_dir, f) for f in files], front=front)

    def _analyses(
        self, seed_dir: Path, files: list[SetFile], budget_s: float = SYNC_LOAD_BUDGET_S
    ) -> tuple[dict[str, dict | None], dict[str, str]]:
        """Analyses known now (memory, or the disk cache within the time budget) + every track's state.

        Anything else is queued at the front of the analysis queue and reported "queued".
        """
        folder = seed_dir.name
        aq = self.analysis
        available = aq.available()
        deadline = time.monotonic() + budget_s
        analyses: dict[str, dict | None] = {}
        states: dict[str, str] = {}
        later: list[AnalysisTask] = []
        for f in files:
            tid = f.track.id
            an = aq.cached(folder, tid)
            st = aq.state(folder, tid)
            if an is None and available and st == "missing":
                task = self.task(seed_dir, f)
                if disk_cache_path(seed_dir, tid).is_file() and time.monotonic() < deadline:
                    try:
                        an = aq.load_now(task)
                    except Exception:  # noqa: BLE001 - recorded as state "error"
                        pass
                    st = aq.state(folder, tid)
                else:
                    later.append(task)
                    st = "queued"
            analyses[tid] = self.enrich(seed_dir, f, an)
            states[tid] = "ready" if an is not None else st
        aq.enqueue(later, front=True)
        return analyses, states

    # --- vocal stems --------------------------------------------------------------------------------------------

    def enrich(self, seed_dir: Path, f: SetFile, an: dict | None) -> dict | None:
        """The analysis with its vocal_per_bar from the track's vocal stem once it's split (else unchanged)."""
        if an is None or self.stems is None:
            return an
        return self.stems.enrich(seed_dir, f.track.id, f.path, an)

    def want_stems(self, seed_dir: Path, files: list[SetFile], front: bool = False) -> None:
        """Queue vocal splitting for `files` (front: songs in use, in order; else background, while idle)."""
        if self.stems is None or not files:
            return
        from .stems_queue import StemTask

        self.stems.enqueue([StemTask(seed_dir.name, seed_dir, f.track.id, f.path) for f in files
                            if self.stems.meta(seed_dir, f.track.id, f.path) is None], front=front)

    def vocal_path(self, seed_dir: Path, f: SetFile) -> Path | None:
        """The track's vocal stem file when it's split and current."""
        return None if self.stems is None else self.stems.vocal_path(seed_dir, f.track.id, f.path)

    def stems_status(self, folder: str) -> dict:
        """StemsStatus (types.ts) of a crate's tracks."""
        seed_dir = resolve_crate(self.root, folder)
        files = self.files(seed_dir)
        if self.stems is None:
            return {"available": False, "reason": "vocal stems aren't part of this build", "tracks": {}, "ready": 0,
                    "total": len(files)}
        return self.stems.status(seed_dir, files)

    def analyses_now(self, seed_dir: Path, files: list[SetFile]) -> dict[str, dict | None]:
        """Track id -> analysis for those known now (None for the rest, which are queued at the front)."""
        return self._analyses(seed_dir, files)[0]

    def crate_set(self, folder: str) -> dict:
        """CrateSet (types.ts). Transitions are partial (ready=false) until both tracks' analyses exist."""
        seed_dir = resolve_crate(self.root, folder)
        files = self.files(seed_dir)
        self.backfill_energy(seed_dir, files)  # values served below; no-op after the first time
        analyses, states = self._analyses(seed_dir, files)
        self.want_stems(seed_dir, files)  # the whole crate, in the background
        tracks = [f.track for f in files]
        return {
            "folder": seed_dir.name,
            "path": str(seed_dir),
            "seed_id": engine.crate_seed_id(seed_dir),
            "tracks": [
                {
                    "track": with_cover(serializers.track(f.track), seed_dir.name, f.path),
                    "path": str(f.path),
                    "audio_url": audio_url(seed_dir.name, f.path.name),
                    "order": n,
                    "energy_rating": f.energy_rating,
                    "genre": f.genre,
                    "analysis_state": states.get(f.track.id, "missing"),
                }
                for n, f in enumerate(files, 1)
            ],
            "transitions": transitions.suggest_set(tracks, analyses, bpm_tol=self.bpm_tol),
        }

    def _apply(self, seed_dir: Path, ordered_ids: list[str]) -> None:
        if self.busy(seed_dir.name):
            raise Conflict("Seedbox is still downloading into this crate; reorder once it has finished")
        library.apply_order(seed_dir, ordered_ids)  # holds library.crate_lock(seed_dir)
        self.invalidate(seed_dir)
        self.bus.publish("set_changed", {"folder": seed_dir.name})

    def set_order(self, folder: str, track_ids: list[str]) -> dict:
        """Write a new running order (tracks not listed keep their relative order at the end)."""
        seed_dir = resolve_crate(self.root, folder)
        current = [f.track.id for f in self.files(seed_dir)]
        if len(set(track_ids)) != len(track_ids):
            raise ApiError("track_ids contains duplicates")
        if unknown := [t for t in track_ids if t not in current]:
            raise ApiError(f"not in this crate: {', '.join(unknown[:5])}")
        self._apply(seed_dir, list(track_ids) + [t for t in current if t not in track_ids])
        return self.crate_set(folder)

    def auto_order(self, folder: str) -> dict:
        """transitions.order_set (seed first; outro->intro fit once analyses exist) -> apply_order."""
        seed_dir = resolve_crate(self.root, folder)
        files = self.files(seed_dir)
        if len(files) >= 2:
            tracks = [f.track for f in files]
            by_id = {t.id: t for t in tracks}
            seed = by_id.get(engine.crate_seed_id(seed_dir) or "") or tracks[0]
            analyses, _ = self._analyses(seed_dir, files)
            ordered = transitions.order_set(seed, tracks, analyses, bpm_tol=self.bpm_tol)
            ids = list(dict.fromkeys(t.id for t in ordered if t.id in by_id))
            self._apply(seed_dir, ids + [t.id for t in tracks if t.id not in ids])
        return self.crate_set(folder)

    def partners(self, folder: str, track_id: str, direction: str) -> list[dict]:
        """Partner[]: the crate's best tracks to play after this one ("out") or before it ("in")."""
        if direction not in ("out", "in"):
            raise ApiError('direction must be "out" or "in"')
        seed_dir = resolve_crate(self.root, folder)
        files = self.files(seed_dir)
        mine = next((f for f in files if f.track.id == track_id), None)
        if mine is None:
            raise NotFound("track not in this crate")
        analyses, _ = self._analyses(seed_dir, files)
        others = [(f.track, analyses.get(f.track.id)) for f in files if f.track.id != track_id]
        found = transitions.partners(mine.track, analyses.get(track_id), others, direction=direction,
                                     limit=PARTNER_LIMIT, bpm_tol=self.bpm_tol)  # fmt: skip
        paths = {f.track.id: f.path for f in files}
        out = []
        for p in found:
            t = track_json(p.get("track"))
            out.append({**p, "track": with_cover(t, seed_dir.name, paths.get(t["id"])) if t else t})
        return out

    def cover(self, folder: str, track_id: str) -> tuple[Path, str, bytes]:
        """(file, mime, bytes) of a crate track's embedded art (seedbox.crates.crate_cover)."""
        return crate_cover(self.root, folder, track_id)

    def file_of(self, seed_dir: Path, track_id: str) -> Path | None:
        return file_of(seed_dir, track_id)

    def summary_seed(self, seed_dir: Path, seed: dict | None) -> dict | None:
        return summary_seed(seed_dir, seed)

    def file(self, seed_dir: Path, track_id: str) -> SetFile:
        """The set file for a track id; NotFound if the crate has no such file."""
        mine = next((f for f in self.files(seed_dir) if f.track.id == track_id), None)
        if mine is None:
            raise NotFound(f"track {track_id} is not in this crate")
        return mine

    def want_analyses(self, seed_dir: Path, files: list[SetFile]) -> None:
        """Queue (front) the analyses of `files` that are neither known nor in the disk cache."""
        aq = self.analysis
        aq.enqueue([self.task(seed_dir, f) for f in files
                    if aq.cached(seed_dir.name, f.track.id) is None and aq.state(seed_dir.name, f.track.id) == "missing"
                    and not disk_cache_path(seed_dir, f.track.id).is_file()], front=True)  # fmt: skip

    def ready_analysis(self, seed_dir: Path, f: SetFile, wait_s: float | None = None) -> dict:
        """The track's analysis now: memory, disk cache, or analysed at the front of the queue within `wait_s`.

        Conflict (409 "analysis pending") if it takes longer (it keeps running; an `analysis` event follows),
        422 if analysing the file failed, 503 without the analysis module.
        """
        aq = self.analysis
        if not aq.available():
            raise ApiError("audio analysis isn't available in this build", 503)
        folder, tid = seed_dir.name, f.track.id
        task = self.task(seed_dir, f)
        deadline = time.monotonic() + (ANALYSIS_WAIT_S if wait_s is None else wait_s)
        queued = False
        while True:
            if (hit := aq.cached(folder, tid)) is not None:
                return self.enrich(seed_dir, f, hit)
            state = aq.state(folder, tid)
            if state == "error":
                raise ApiError(f"analysis failed: {aq.error(folder, tid)}", 422)
            if state == "missing":
                if disk_cache_path(seed_dir, tid).is_file():
                    try:
                        return self.enrich(seed_dir, f, aq.load_now(task))
                    except Exception as e:  # noqa: BLE001
                        raise ApiError(f"analysis failed: {e}", 422) from e
                if not queued:
                    aq.enqueue([task], front=True)
                    queued = True
            if time.monotonic() >= deadline:
                raise Conflict("analysis pending")
            time.sleep(0.05)

    def track_analysis(self, folder: str, track_id: str) -> dict:
        """TrackAnalysis; 404 until it's ready, 422 if it failed, 503 without the analysis module."""
        seed_dir = resolve_crate(self.root, folder)
        aq = self.analysis
        mine = next((f for f in self.files(seed_dir) if f.track.id == track_id), None)
        if hit := aq.cached(seed_dir.name, track_id):
            return self.enrich(seed_dir, mine, hit) if mine is not None else hit
        if mine is None:
            raise NotFound("track not in this crate")
        state = aq.state(seed_dir.name, track_id)
        if state == "error":
            raise ApiError(f"analysis failed: {aq.error(seed_dir.name, track_id)}", 422)
        if state in ("queued", "running"):
            raise NotFound(f"analysis {state}")
        if not aq.available():
            raise ApiError("audio analysis isn't available in this build", 503)
        task = self.task(seed_dir, mine)
        if disk_cache_path(seed_dir, track_id).is_file():
            try:
                return self.enrich(seed_dir, mine, aq.load_now(task))
            except Exception as e:  # noqa: BLE001
                raise ApiError(f"analysis failed: {e}", 422) from e
        aq.enqueue([task], front=True)
        raise NotFound("analysis queued")
