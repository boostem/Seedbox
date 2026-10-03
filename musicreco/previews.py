"""Background preview clips for the crate screen: 20 s YouTube snippets preloaded so clicking play is instant.

`PreviewManager.preload(tracks)` queues tracks in order; `concurrency` worker threads each run
`youtube.find_match` → `youtube.fetch_clip` (20 s from ~35% into the track, falling back to the match's
alternatives) → a ~200-point loudness envelope of the clip. Results live in `cache_dir/previews/<id>.m4a` +
`<id>.json` and survive restarts; "no match" is cached for NO_MATCH_TTL_S, errors only in memory (the next
preload retries them). The folder is kept under `max_cache_mb` by evicting the least recently used clips.

`prioritize(id)` moves a queued track to the front (and starts it at once on one extra short-lived thread when
every worker is busy); `clear_queue()` drops work that hasn't started; in-flight work always finishes and is
cached. Every state change reaches `on_event(PreviewStatus dict)` in order, from one dispatcher thread, never
while a lock is held. Nothing here prints.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from . import paths, youtube
from .models import Track

log = logging.getLogger(__name__)

CLIP_LENGTH_S = 20.0
START_FRACTION = 0.35
PEAK_POINTS = 200
PEAK_SAMPLE_RATE = 8000
MAX_CANDIDATES = 3  # best match + up to two alternatives when a clip can't be fetched
NO_MATCH_TTL_S = 7 * 86400
TOUCH_PERSIST_S = 300.0  # a clip's last_used is rewritten on disk at most this often
EVICT_TARGET = 0.9  # once over max_cache_mb, evict down to this fraction of it
STALE_TEMP_S = 3600.0
SHUTDOWN_WAIT_S = 0.5  # busy fetches can't be interrupted; workers are daemon threads
FFMPEG_TIMEOUT_S = 30
CACHE_VERSION = 1

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_ERROR_CHARS = 200
_STOP = object()


def _short(msg: object) -> str:
    text = " ".join(str(msg).split())
    return text if len(text) <= _MAX_ERROR_CHARS else text[: _MAX_ERROR_CHARS - 1] + "…"


def clip_peaks(path: Path, points: int = PEAK_POINTS) -> list[float]:
    """`points` loudness values 0-1 (RMS per window, normalised to the loudest window) of an audio file."""
    ffmpeg = paths.ffmpeg_path()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-ac", "1",
           "-ar", str(PEAK_SAMPLE_RATE), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=FFMPEG_TIMEOUT_S)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"ffmpeg exit {proc.returncode}: {tail[0]}")
    samples = np.frombuffer(proc.stdout, dtype="<f4").astype(np.float64)
    n = min(points, samples.size)
    if n == 0:
        raise RuntimeError("no audio decoded")
    windows = samples[: samples.size - samples.size % n].reshape(n, -1)
    rms = np.sqrt(np.mean(windows * windows, axis=1))
    top = float(rms.max())
    if top <= 0:
        return [0.0] * n
    return [round(float(v), 3) for v in rms / top]


@dataclass
class _Entry:
    """Settled state of one track; "queued"/"loading" are derived from the manager's queue/in-flight sets."""

    state: str = "idle"  # idle | ready | no_match | error
    peaks: list[float] | None = None
    youtube_title: str | None = None
    youtube_url: str | None = None
    error: str | None = None
    created_at: float = 0.0
    last_used: float = 0.0
    persisted_used: float = 0.0  # last_used as currently written on disk


def _status_dict(track_id: str, state: str, e: _Entry) -> dict:
    ready = state == "ready"
    return {
        "track_id": track_id,
        "state": state,
        "audio_url": f"/api/previews/{track_id}/audio" if ready else None,
        "peaks": list(e.peaks) if ready and e.peaks is not None else None,
        "youtube_title": e.youtube_title if state in ("ready", "error") else None,
        "error": e.error if state in ("no_match", "error") else None,
    }


class PreviewManager:
    """Preloads, caches and serves preview clips. Thread-safe; every public method returns quickly."""

    def __init__(self, cache_dir: Path, concurrency: int = 3, max_cache_mb: int = 500,
                 on_event: Callable[[dict], None] | None = None, tolerance_s: float = 15.0) -> None:
        self.previews_dir = Path(cache_dir) / "previews"
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        self.concurrency = max(1, int(concurrency))
        self.max_cache_bytes = max(0, int(max_cache_mb)) * 1024 * 1024
        self.tolerance_s = tolerance_s
        self._on_event = on_event
        self._cond = threading.Condition(threading.Lock())
        self._entries: dict[str, _Entry] = {}
        self._tracks: dict[str, Track] = {}
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._inflight: set[str] = set()
        self._workers: list[threading.Thread] = []
        self._busy = 0  # regular workers currently processing a track
        self._express: threading.Thread | None = None
        self._closed = False
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._dispatcher: threading.Thread | None = None
        self._evict_lock = threading.Lock()
        self._cache_bytes: int | None = None  # running estimate of the folder size, None until first scan
        self._sweep_temp()

    # ------------------------------------------------------------ public API

    def preload(self, tracks: Iterable[Track]) -> None:
        """Queue `tracks` in the given order behind anything already queued; ready / no_match ones are skipped."""
        tracks = [t for t in tracks if _ID_RE.match(t.id)]
        for t in tracks:
            self._load_entry(t.id)
        now = time.time()
        touched: list[str] = []
        with self._cond:
            if self._closed:
                return
            added = 0
            for t in tracks:
                self._tracks[t.id] = t
                if t.id in self._queued or t.id in self._inflight:
                    continue
                e = self._entries[t.id]
                if self._cached(e, now):
                    if self._touch(e, now):
                        touched.append(t.id)
                    continue
                self._enqueue(t.id, front=False)
                added += 1
            if added:
                self._ensure_workers()
                self._cond.notify_all()
            snapshots = [(tid, replace(self._entries[tid])) for tid in touched]
        log.debug("preload: %d tracks, %d queued", len(tracks), added)
        for tid, snap in snapshots:
            self._write_record(tid, snap)

    def prioritize(self, track_id: str, track: Track | None = None) -> dict:
        """Play click: move `track_id` to the front of the queue (starting it now if every worker is busy).

        Idle/error tracks are queued if known (from an earlier preload, or passed as `track`). Returns the
        PreviewStatus right after the change.
        """
        if not _ID_RE.match(track_id):
            return _status_dict(track_id, "idle", _Entry())
        self._load_entry(track_id)
        now = time.time()
        express: threading.Thread | None = None
        snapshot: _Entry | None = None
        with self._cond:
            if track is not None and track.id == track_id:
                self._tracks[track_id] = track
            e = self._entries[track_id]
            if self._closed or track_id in self._inflight:
                pass
            elif self._cached(e, now):
                if self._touch(e, now):
                    snapshot = replace(e)
            elif track_id in self._queued:
                self._queue.remove(track_id)
                self._queue.appendleft(track_id)
            elif track_id in self._tracks:
                self._enqueue(track_id, front=True)
            if track_id in self._queued:
                self._ensure_workers()
                if self._busy >= self.concurrency and self._express is None:
                    express = self._express = threading.Thread(
                        target=self._run_express, args=(track_id,), name="preview-express", daemon=True)
                else:
                    self._cond.notify_all()
            status = self._status(track_id)
        if express is not None:
            log.debug("prioritize %s: all workers busy, starting express fetch", track_id)
            express.start()
        if snapshot is not None:
            self._write_record(track_id, snapshot)
        return status

    def status(self, track_id: str) -> dict:
        """PreviewStatus dict (frontend/src/api/types.ts); unknown tracks are "idle"."""
        if not _ID_RE.match(track_id):
            return _status_dict(track_id, "idle", _Entry())
        self._load_entry(track_id)
        with self._cond:
            return self._status(track_id)

    def audio_path(self, track_id: str) -> Path | None:
        """The ready clip (an .m4a, serve as audio/mp4) and mark it recently used; None if not ready."""
        if not _ID_RE.match(track_id):
            return None
        self._load_entry(track_id)
        path = self._clip_path(track_id)
        exists = path.is_file()
        now = time.time()
        snapshot: _Entry | None = None
        with self._cond:
            e = self._entries[track_id]
            if e.state != "ready" or track_id in self._inflight:
                return None
            if not exists:  # deleted behind our back
                self._entries[track_id] = _Entry()
                self._emit(track_id)
                return None
            if self._touch(e, now):
                snapshot = replace(e)
        if snapshot is not None:
            self._write_record(track_id, snapshot)
        return path

    def clear_queue(self) -> None:
        """Drop queued-but-not-started work (another crate was opened); in-flight fetches still finish."""
        with self._cond:
            dropped = list(self._queue)
            self._queue.clear()
            self._queued.clear()
            for tid in dropped:
                self._emit(tid)
        if dropped:
            log.debug("cleared %d queued previews", len(dropped))

    def shutdown(self) -> None:
        """Stop the workers: idle ones exit now, busy ones after their current fetch (no events after this)."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
            self._queued.clear()
            self._cond.notify_all()
            threads = [*self._workers, self._express, self._dispatcher]
        self._events.put(_STOP)
        deadline = time.monotonic() + SHUTDOWN_WAIT_S
        for t in threads:
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))
        log.debug("preview manager shut down")

    # ------------------------------------------------------------ queue + workers (lock held unless noted)

    def _status(self, track_id: str) -> dict:
        e = self._entries.get(track_id) or _Entry()
        if track_id in self._inflight:
            state = "loading"
        elif track_id in self._queued:
            state = "queued"
        else:
            state = e.state
        return _status_dict(track_id, state, e)

    def _emit(self, track_id: str) -> None:
        """Snapshot the status now (so events keep state-change order) and hand it to the dispatcher."""
        if self._on_event is None or self._closed:
            return
        self._events.put(self._status(track_id))
        if self._dispatcher is None:
            self._dispatcher = threading.Thread(target=self._dispatch, name="preview-events", daemon=True)
            self._dispatcher.start()

    def _dispatch(self) -> None:
        """Dispatcher thread: deliver events in order, outside every lock, swallowing callback errors."""
        while (event := self._events.get()) is not _STOP:
            try:
                self._on_event(event)
            except Exception:
                log.debug("preview on_event callback failed", exc_info=True)

    def _cached(self, e: _Entry, now: float) -> bool:
        return e.state == "ready" or (e.state == "no_match" and now - e.created_at < NO_MATCH_TTL_S)

    def _touch(self, e: _Entry, now: float) -> bool:
        """Mark a ready clip used; True when the on-disk last_used is stale enough to rewrite."""
        if e.state != "ready":
            return False
        e.last_used = now
        if now - e.persisted_used < TOUCH_PERSIST_S:
            return False
        e.persisted_used = now
        return True

    def _enqueue(self, track_id: str, front: bool) -> None:
        if front:
            self._queue.appendleft(track_id)
        else:
            self._queue.append(track_id)
        self._queued.add(track_id)
        self._emit(track_id)

    def _ensure_workers(self) -> None:
        if self._workers or self._closed:
            return
        for n in range(self.concurrency):
            t = threading.Thread(target=self._work, name=f"preview-{n + 1}", daemon=True)
            self._workers.append(t)
            t.start()

    def _take(self, track_id: str) -> Track:
        """Move a dequeued id to in-flight ("loading")."""
        self._queued.discard(track_id)
        self._inflight.add(track_id)
        self._emit(track_id)
        return self._tracks[track_id]

    def _work(self) -> None:
        """Worker thread (no lock held on entry)."""
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                track_id = self._queue.popleft()
                track = self._take(track_id)
                self._busy += 1
            try:
                self._process(track)
            finally:
                with self._cond:
                    self._busy -= 1

    def _run_express(self, track_id: str) -> None:
        """One-off thread for a prioritized track while all workers are busy (no lock held on entry)."""
        try:
            with self._cond:
                if self._closed or track_id not in self._queued:
                    return  # a worker got there first
                self._queue.remove(track_id)
                track = self._take(track_id)
            self._process(track)
        finally:
            with self._cond:
                self._express = None

    # ------------------------------------------------------------ fetching (no lock held)

    def _process(self, track: Track) -> None:
        started = time.monotonic()
        try:
            outcome = self._fetch(track)
        except Exception as e:
            log.info("preview failed for %s: %s", track.display_name, e, exc_info=log.isEnabledFor(logging.DEBUG))
            outcome = _Entry(state="error", error=_short(e))
        if outcome is None:  # shut down mid-way
            with self._cond:
                self._inflight.discard(track.id)
            return
        if outcome.state in ("ready", "no_match"):
            self._write_record(track.id, outcome)
        with self._cond:
            self._inflight.discard(track.id)
            self._entries[track.id] = outcome
            self._emit(track.id)
        if outcome.state == "ready":
            log.info("preview ready for %s in %.1fs", track.display_name, time.monotonic() - started)
            self._maybe_evict(track.id)
        elif outcome.state == "no_match":
            log.info("no preview match for %s: %s", track.display_name, outcome.error)

    def _fetch(self, track: Track) -> _Entry | None:
        """find_match → fetch_clip (alternatives on failure) → peaks. None if shut down mid-way."""
        result = youtube.find_match(track, tolerance_s=self.tolerance_s)
        now = time.time()
        if result.match is None:
            return _Entry(state="no_match", error=_short(result.reason or "no YouTube match"), created_at=now)
        candidates = [result.match, *result.alternatives][:MAX_CANDIDATES]
        dest = self._clip_path(track.id)
        error: Exception | None = None
        for cand in candidates:
            if self._closed:
                return None
            start = START_FRACTION * (track.duration_s or cand.duration_s or 0.0)
            try:
                clip = Path(youtube.fetch_clip(cand, start, CLIP_LENGTH_S, dest))
            except Exception as e:
                log.info("clip fetch failed for %s via %s: %s", track.display_name, cand.url, e)
                error = e
                continue
            try:
                peaks = clip_peaks(clip)
            except Exception as e:
                log.info("peaks failed for %s: %s", track.display_name, e)
                peaks = None
            now = time.time()
            return _Entry(state="ready", peaks=peaks, youtube_title=cand.title, youtube_url=cand.url,
                          created_at=now, last_used=now, persisted_used=now)
        tried = f" (tried {len(candidates)} videos)" if len(candidates) > 1 else ""
        return _Entry(state="error", error=_short(f"{error}{tried}"), youtube_title=result.match.title)

    # ------------------------------------------------------------ disk cache (no lock held)

    def _clip_path(self, track_id: str) -> Path:
        return self.previews_dir / f"{track_id}{youtube.CLIP_EXT}"

    def _record_path(self, track_id: str) -> Path:
        return self.previews_dir / f"{track_id}.json"

    def _load_entry(self, track_id: str) -> None:
        """Make sure `track_id` has an in-memory entry, reading its sidecar JSON outside the lock."""
        with self._cond:
            if track_id in self._entries:
                return
        e = self._read_record(track_id) or _Entry()
        with self._cond:
            self._entries.setdefault(track_id, e)

    def _read_record(self, track_id: str) -> _Entry | None:
        try:
            data = json.loads(self._record_path(track_id).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.debug("unreadable preview record %s: %s", track_id, e)
            return None
        state = data.get("state")
        e = _Entry(state=state, peaks=data.get("peaks"), youtube_title=data.get("youtube_title"),
                   youtube_url=data.get("youtube_url"), error=data.get("error"),
                   created_at=float(data.get("created_at") or 0), last_used=float(data.get("last_used") or 0))
        e.persisted_used = e.last_used
        if state == "ready" and self._clip_path(track_id).is_file():
            return e
        if state == "no_match" and time.time() - e.created_at < NO_MATCH_TTL_S:
            return e
        return None  # stale (expired no_match, clip gone): the eviction sweep deletes it

    def _write_record(self, track_id: str, e: _Entry) -> None:
        data = {
            "version": CACHE_VERSION, "track_id": track_id, "state": e.state, "peaks": e.peaks,
            "youtube_title": e.youtube_title, "youtube_url": e.youtube_url, "error": e.error,
            "created_at": e.created_at, "last_used": e.last_used,
        }
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=f".{track_id}.", suffix=".tmp", dir=self.previews_dir)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._record_path(track_id))
        except OSError as err:
            log.info("could not write preview record %s: %s", track_id, err)
            if tmp:
                Path(tmp).unlink(missing_ok=True)

    def _sweep_temp(self) -> None:
        """Remove partial downloads / records left behind by a crash."""
        cutoff = time.time() - STALE_TEMP_S
        for p in self.previews_dir.glob(".*"):
            try:
                if p.stat().st_mtime > cutoff:
                    continue
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            except OSError:
                pass

    def _cache_files(self) -> dict[str, list[tuple[Path, os.stat_result]]]:
        groups: dict[str, list[tuple[Path, os.stat_result]]] = {}
        for p in self.previews_dir.iterdir():
            if p.name.startswith(".") or p.suffix not in (youtube.CLIP_EXT, ".json"):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            groups.setdefault(p.stem, []).append((p, st))
        return groups

    def _maybe_evict(self, keep: str) -> None:
        """Keep the folder under max_cache_mb: cheap running total, full LRU scan only when over the limit."""
        with self._evict_lock:
            if self._cache_bytes is None:
                self._cache_bytes = sum(st.st_size for files in self._cache_files().values() for _, st in files)
            else:
                for p in (self._clip_path(keep), self._record_path(keep)):
                    try:
                        self._cache_bytes += p.stat().st_size
                    except OSError:
                        pass
            if self._cache_bytes > self.max_cache_bytes:
                self._cache_bytes = self._evict(keep)

    def _evict(self, keep: str) -> int:
        """Delete stale records, then least recently used clips until under EVICT_TARGET; returns the new total."""
        now = time.time()
        groups = self._cache_files()
        with self._cond:
            memory = {tid: (e.state, e.last_used) for tid, e in self._entries.items()}
        total = 0
        candidates: list[tuple[float, str, list[Path], int]] = []
        for tid, files in groups.items():
            size = sum(st.st_size for _, st in files)
            total += size
            if tid == keep:
                continue
            paths = [p for p, _ in files]
            has_record = any(p.suffix == ".json" for p in paths)
            if not any(p.suffix == youtube.CLIP_EXT for p in paths):
                if self._read_record(tid) is None and not self._busy_id(tid):  # expired no_match or orphan record
                    self._delete(tid, paths)
                    total -= size
                continue
            state, last_used = memory.get(tid, ("", 0.0))
            if state != "ready":
                record = self._read_record(tid) if has_record else None
                last_used = record.last_used if record else 0.0
            last_used = last_used or max(st.st_mtime for _, st in files)
            candidates.append((last_used, tid, paths, size))
        target = self.max_cache_bytes * EVICT_TARGET
        evicted: list[str] = []
        freed = 0
        for _, tid, paths, size in sorted(candidates):
            if total <= target:
                break
            if self._busy_id(tid):
                continue
            self._delete(tid, paths)
            total -= size
            freed += size
            evicted.append(tid)
        if evicted:
            with self._cond:
                for tid in evicted:
                    e = self._entries.get(tid)
                    if e is not None and e.state == "ready" and tid not in self._inflight:
                        self._entries[tid] = _Entry()
                        self._emit(tid)
            log.info("evicted %d preview clips (%.1f MB), cache now %.1f MB",
                     len(evicted), freed / 1048576, total / 1048576)
        return total

    def _busy_id(self, track_id: str) -> bool:
        with self._cond:
            return track_id in self._inflight or track_id in self._queued

    def _delete(self, track_id: str, paths: list[Path]) -> None:
        # record first, so a clip without a record is never mistaken for a ready preview
        for p in sorted(paths, key=lambda p: p.suffix != ".json"):
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                log.debug("could not delete %s: %s", p, e)
