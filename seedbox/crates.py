"""Crate folders on disk -> CrateSet / Partner / TrackAnalysis JSON; set reordering. Strict path containment."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mutagen.id3 import ID3

from musicreco import library, mixing, tagging, transitions
from musicreco.models import Track

from . import engine, serializers
from .analysis_queue import AnalysisQueue, AnalysisTask, disk_cache_path
from .events import EventBus

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff", ".flac", ".ogg", ".opus"}
SYNC_LOAD_BUDGET_S = 3.0  # a set request may load this long from the analysis disk cache before deferring
PARTNER_LIMIT = 8
ANALYSIS_WAIT_S = 20.0  # a mix plan waits this long for a missing analysis before answering 409
SCAN_CACHE = 32  # crate folders whose scan is kept (invalidated by any file change)


class ApiError(Exception):
    """An error with an HTTP status; the server turns it into `{detail}`."""

    status = 400

    def __init__(self, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if status is not None:
            self.status = status


class NotFound(ApiError):
    status = 404


class Conflict(ApiError):
    status = 409


# --- path containment ---------------------------------------------------------------------------------------------


def _single_name(name: str, what: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ApiError(f"invalid {what} name")


def inside(path: Path, root: Path) -> bool:
    """True if `path` (symlinks resolved) is `root` or below it."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def resolve_crate(root: Path, folder: str) -> Path:
    """The crate folder `root/folder`, refusing anything that resolves outside root."""
    _single_name(folder, "crate folder")
    path = root / folder
    if not inside(path, root) or path.resolve() == root.resolve():
        raise ApiError("crate folder outside the crates root", 403)
    if not path.is_dir():
        raise NotFound(f"no crate folder named {folder!r}")
    return path


def resolve_crate_file(root: Path, folder: str, filename: str) -> Path:
    """An audio file directly inside a crate folder."""
    crate = resolve_crate(root, folder)
    _single_name(filename, "file")
    path = crate / filename
    if filename.startswith(".") or path.suffix.lower() not in AUDIO_SUFFIXES:
        raise NotFound("no such audio file")
    if not inside(path, crate) or path.resolve().parent != crate.resolve() or not path.is_file():
        raise NotFound("no such audio file")
    return path


def audio_url(folder: str, filename: str) -> str:
    return f"/api/files/{quote(folder, safe='')}/{quote(filename, safe='')}"


# --- embedded cover art --------------------------------------------------------------------------------------------

_art_lock = threading.Lock()
_has_art: dict[str, tuple[tuple[int, int], bool]] = {}  # path -> ((size, mtime_ns), has APIC)


def embedded_art(path: Path) -> tuple[str, bytes] | None:
    """(mime, bytes) of the MP3's APIC art (front cover preferred), or None."""
    try:
        frames = ID3(path).getall("APIC")
    except Exception:  # noqa: BLE001 - no tag / unreadable
        return None
    frames = [f for f in frames if f.data]
    if not frames:
        return None
    best = next((f for f in frames if f.type == 3), frames[0])
    mime = (best.mime or "").lower()
    if not mime.startswith("image/"):
        mime = "image/png" if best.data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return mime, best.data


def has_art(path: Path) -> bool:
    """Whether the file carries embedded art (cached until the file changes)."""
    try:
        st = path.stat()
    except OSError:
        return False
    sig = (st.st_size, st.st_mtime_ns)
    key = str(path)
    with _art_lock:
        hit = _has_art.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    found = embedded_art(path) is not None
    with _art_lock:
        if len(_has_art) > 20_000:
            _has_art.clear()
        _has_art[key] = (sig, found)
    return found


def cover_url(folder: str, track_id: str) -> str:
    return f"/api/covers/{quote(folder, safe='')}/{quote(track_id, safe='')}"


def with_cover(track: dict | None, folder: str, path: Path | None) -> dict | None:
    """Track JSON whose missing cover_url points at the file's embedded art (old crates rebuilt from tracks.csv)."""
    if track is None or track.get("cover_url") or path is None or not has_art(path):
        return track
    return {**track, "cover_url": cover_url(folder, track["id"])}


def track_json(t: Any) -> Any:
    """A Track (or raw tunebat dict) as Track JSON; JSON-shaped dicts and None pass through."""
    if isinstance(t, Track):
        return serializers.track(t)
    if isinstance(t, dict) and "display_name" not in t and "id" in t and "n" in t:
        return serializers.track(Track.from_api(t))
    return t


# --- the crate's files ---------------------------------------------------------------------------------------------


@dataclass
class SetFile:
    """One MP3 of the set: its file, its Track and the tag-derived extras."""

    path: Path
    track: Track
    energy_rating: int | None
    genre: str | None


def _signature(seed_dir: Path) -> tuple:
    """Cheap fingerprint of a crate folder: MP3 names/sizes/mtimes + the metadata files."""
    sig = []
    try:
        for p in sorted(seed_dir.iterdir()):
            if p.suffix.lower() == ".mp3" or p.name == "tracks.csv":
                st = p.stat()
                sig.append((p.name, st.st_size, st.st_mtime_ns))
        cj = library.crate_json_path(seed_dir)
        if cj.is_file():
            st = cj.stat()
            sig.append((".crate.json", st.st_size, st.st_mtime_ns))
    except OSError:
        pass
    return tuple(sig)


def scan_crate(seed_dir: Path) -> list[SetFile]:
    """The crate's MP3s that carry a Spotify ID, in set order (library.crate_files: TRCK, unnumbered last)."""
    known = library.load_crate_tracks(seed_dir)
    out: list[SetFile] = []
    seen: set[str] = set()
    for f in library.crate_files(seed_dir):
        tid = f.track_id
        if not tid or tid in seen or tid not in known:  # hand-dropped files without an ID can't be placed
            continue
        seen.add(tid)
        out.append(SetFile(f.path, known[tid], f.energy_rating, f.genre or None))
    return out


class CrateService:
    """Set-builder operations on crate folders under the current output_root."""

    def __init__(
        self,
        get_settings: Callable[[], Any],
        analysis: AnalysisQueue,
        bus: EventBus,
        busy: Callable[[str], bool] = lambda folder: False,
    ) -> None:
        self.get_settings = get_settings
        self.analysis = analysis
        self.bus = bus
        self.busy = busy
        self._lock = threading.Lock()
        self._scans: dict[str, tuple[tuple, list[SetFile]]] = {}
        self._energy_done: set[str] = set()  # crates whose missing energy ratings were back-filled this session
        self._energy_exact: dict[str, dict[str, int]] = {}  # crate -> id -> exact 1-10 written (tags keep stars)

    @property
    def root(self) -> Path:
        return Path(self.get_settings().output_root)

    @property
    def bpm_tol(self) -> float:
        return float(self.get_settings().bpm_tolerance)

    @property
    def mix_length(self) -> str:
        """Preferred blend length (settings.mix_length): short / normal / long."""
        return str(getattr(self.get_settings(), "mix_length", "normal"))

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
            analyses[tid] = an
            states[tid] = "ready" if an is not None else st
        aq.enqueue(later, front=True)
        return analyses, states

    def crate_set(self, folder: str) -> dict:
        """CrateSet (types.ts). Transitions are partial (ready=false) until both tracks' analyses exist."""
        seed_dir = resolve_crate(self.root, folder)
        files = self.files(seed_dir)
        self.backfill_energy(seed_dir, files)  # values served below; no-op after the first time
        analyses, states = self._analyses(seed_dir, files)
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
            "transitions": transitions.suggest_set(tracks, analyses, bpm_tol=self.bpm_tol, mix_length=self.mix_length),
        }

    def _apply(self, seed_dir: Path, ordered_ids: list[str]) -> None:
        if self.busy(seed_dir.name):
            raise Conflict("a download is still running for this crate; reorder once it has finished")
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
                                     limit=PARTNER_LIMIT, bpm_tol=self.bpm_tol, mix_length=self.mix_length)  # fmt: skip
        paths = {f.track.id: f.path for f in files}
        out = []
        for p in found:
            t = track_json(p.get("track"))
            out.append({**p, "track": with_cover(t, seed_dir.name, paths.get(t["id"])) if t else t})
        return out

    def cover(self, folder: str, track_id: str) -> tuple[Path, str, bytes]:
        """(file, mime, bytes) of a crate track's embedded art; NotFound if the track or its art is missing."""
        seed_dir = resolve_crate(self.root, folder)
        path = self.file_of(seed_dir, track_id)
        art = embedded_art(path) if path is not None else None
        if path is None or art is None:
            raise NotFound("no embedded cover art for this track")
        return path, *art

    def file_of(self, seed_dir: Path, track_id: str) -> Path | None:
        """The MP3 directly in the crate folder tagged with this Spotify ID (ID reads are cached by mtime)."""
        found = library.LibraryIndex(seed_dir).find(track_id)
        return next((p for p in found if p.parent == seed_dir), None)

    def summary_seed(self, seed_dir: Path, seed: dict | None) -> dict | None:
        """CrateSummary.seed with cover_url filled from the seed file's art when tunebat's is unknown."""
        if seed is None or seed.get("cover_url"):
            return seed
        return with_cover(seed, seed_dir.name, self.file_of(seed_dir, seed["id"]))

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
                return hit
            state = aq.state(folder, tid)
            if state == "error":
                raise ApiError(f"analysis failed: {aq.error(folder, tid)}", 422)
            if state == "missing":
                if disk_cache_path(seed_dir, tid).is_file():
                    try:
                        return aq.load_now(task)
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
        if hit := aq.cached(seed_dir.name, track_id):
            return hit
        mine = next((f for f in self.files(seed_dir) if f.track.id == track_id), None)
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
                return aq.load_now(task)
            except Exception as e:  # noqa: BLE001
                raise ApiError(f"analysis failed: {e}", 422) from e
        aq.enqueue([task], front=True)
        raise NotFound("analysis queued")
