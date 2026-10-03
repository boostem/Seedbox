"""Audition a short YouTube clip of a track from inside the checklist, without ever blocking the UI.

`play(track)` returns instantly (it is meant to be called from a prompt_toolkit key-binding handler); the
slow parts run on a daemon thread: `youtube.find_match` (cached per track id, "no match" included), then
`youtube.fetch_clip` of CLIP_LENGTH_S seconds starting ~35% into the track (cached on disk in a session
temp dir), then the player (`afplay`, else `ffplay`) is started in the background. The latest request wins:
a fetch that finishes after a newer `play`/`stop` caches its clip but never starts playing.

`status()` is a one-line state for a toolbar; register `set_on_change(cb)` to be told when it changes
(called from arbitrary threads, never while holding the module lock). Nothing here prints.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from . import youtube
from .models import Track, YTCandidate

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())  # failures are logged at INFO/DEBUG only: never dumped on the terminal mid-UI

CLIP_LENGTH_S = 20
START_FRACTION = 0.35
MAX_CANDIDATES = 2  # best match + one alternative if its clip can't be fetched
_MAX_ERROR_CHARS = 90
_BREW_BIN = "/opt/homebrew/bin"


def _short(msg: object) -> str:
    """Single-line, toolbar-sized rendering of an error."""
    text = " ".join(str(msg).split())
    return text if len(text) <= _MAX_ERROR_CHARS else text[: _MAX_ERROR_CHARS - 1] + "…"


def _player_argv(clip: Path) -> list[str] | None:
    """Command that plays `clip` and exits: afplay on macOS, ffplay as fallback, None if neither exists."""
    search_path = os.pathsep.join(p for p in (os.environ.get("PATH"), _BREW_BIN) if p)
    if afplay := shutil.which("afplay", path=search_path):
        return [afplay, str(clip)]
    if ffplay := shutil.which("ffplay", path=search_path):
        return [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(clip)]
    return None


def _clip_start(track: Track, candidate: YTCandidate) -> float:
    """~35% into the track (fetch_clip itself pulls the start back if the video is too short)."""
    return START_FRACTION * (track.duration_s or candidate.duration_s or 0.0)


class Auditioner:
    """All audition state behind one lock; the module-level functions drive a shared instance."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._on_change: Callable[[], None] | None = None
        self._generation = 0  # bumped by every play/stop/cleanup; only the current one may start the player
        self._wanted_id: str | None = None  # track id of the latest play request
        self._status = ""
        self._proc: subprocess.Popen | None = None
        self._tmpdir: Path | None = None
        self._clips: dict[str, Path] = {}
        self._matches: dict[str, list[YTCandidate]] = {}  # empty list = cached "no match"
        self._track_locks: dict[str, threading.Lock] = {}  # one fetch per track at a time

    # ------------------------------------------------------------ public API

    def set_on_change(self, callback: Callable[[], None] | None) -> None:
        self._on_change = callback

    def status(self) -> str:
        with self._lock:
            return self._status

    def play(self, track: Track) -> str:
        """Start auditioning `track`; returns the immediate status without blocking."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._wanted_id = track.id
            self._kill_player()
            outcome = self._cached(track)
            if outcome is None:
                self._status = f"⏳ loading {track.display_name}…"
                threading.Thread(target=self._work, args=(track, generation), name="audition", daemon=True).start()
            else:
                self._finish(track, generation, outcome)
            status = self._status
        self._notify()
        return status

    def stop(self) -> None:
        """Stop the player and cancel any pending start."""
        with self._lock:
            self._generation += 1
            self._wanted_id = None
            self._kill_player()
            self._status = ""
        self._notify()

    def cleanup(self) -> None:
        """Stop everything and delete the session's clips (also registered with atexit)."""
        with self._lock:
            self._generation += 1
            self._wanted_id = None
            proc, self._proc = self._proc, None
            tmpdir, self._tmpdir = self._tmpdir, None
            self._clips.clear()
            self._matches.clear()
            self._status = ""
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                pass
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ------------------------------------------------------------ internals (`_lock` held where noted)

    def _notify(self) -> None:
        callback = self._on_change
        if callback is None:
            return
        try:
            callback()
        except Exception:
            log.debug("audition on_change callback failed", exc_info=True)

    def _cached(self, track: Track) -> Path | str | None:
        """Playable clip, or a failure status, if the answer is already known (lock held)."""
        clip = self._clips.get(track.id)
        if clip is not None:
            if clip.is_file() and clip.stat().st_size > 0:
                return clip
            del self._clips[track.id]
        if self._matches.get(track.id) == []:
            return f"✗ no YouTube match for {track.display_name}"
        return None

    def _session_dir(self) -> Path:
        """Temp dir that holds this session's clips, created on first use (lock held)."""
        if self._tmpdir is None or not self._tmpdir.is_dir():
            self._tmpdir = Path(tempfile.mkdtemp(prefix="musicreco-audition-"))
        return self._tmpdir

    def _track_lock(self, track_id: str) -> threading.Lock:
        with self._lock:
            return self._track_locks.setdefault(track_id, threading.Lock())

    def _kill_player(self) -> None:
        """Terminate the current player without waiting; its watcher thread reaps it (lock held)."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _finish(self, track: Track, generation: int, outcome: Path | str) -> None:
        """Apply a result if its request is still the latest: start the player or show the failure (lock held)."""
        if generation != self._generation:
            return
        if isinstance(outcome, str):
            self._status = outcome
            return
        argv = _player_argv(outcome)
        if argv is None:
            self._status = "✗ no audio player found (afplay / ffplay)"
            return
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError as e:
            log.info("could not start %s: %s", argv[0], e)
            self._status = f"✗ player failed: {_short(e)}"
            return
        self._proc = proc
        self._status = f"▶ {track.display_name} ({CLIP_LENGTH_S} s)"
        threading.Thread(target=self._watch, args=(proc,), name="audition-player", daemon=True).start()

    def _watch(self, proc: subprocess.Popen) -> None:
        """Reap the player; if it ended on its own (not killed by us) clear or flag the status."""
        code = proc.wait()
        with self._lock:
            if self._proc is not proc:
                return
            self._proc = None
            self._status = "" if code == 0 else f"✗ player exited with code {code}"
        self._notify()

    def _work(self, track: Track, generation: int) -> None:
        try:
            outcome = self._prepare(track)
        except Exception as e:
            log.info("audition failed for %s: %s", track.display_name, e, exc_info=True)
            outcome = f"✗ clip failed: {_short(e)}"
        if outcome is None:  # superseded or cleaned up before a clip was produced
            return
        with self._lock:
            self._finish(track, generation, outcome)
        self._notify()

    def _wants(self, track: Track) -> bool:
        with self._lock:
            return self._wanted_id == track.id

    def _prepare(self, track: Track) -> Path | str | None:
        """Slow part (match + clip fetch, both cached). Returns a clip, a failure status, or None if abandoned."""
        with self._track_lock(track.id):
            with self._lock:
                if (cached := self._cached(track)) is not None:
                    return cached
                candidates = self._matches.get(track.id)
            if candidates is None:
                result = youtube.find_match(track)
                candidates = ([result.match, *result.alternatives] if result.match else [])[:MAX_CANDIDATES]
                with self._lock:
                    self._matches[track.id] = candidates
            if not candidates:
                return f"✗ no YouTube match for {track.display_name}"

            error: Exception | None = None
            for candidate in candidates:
                with self._lock:
                    if self._wanted_id != track.id:  # user moved on: don't spend bandwidth on this clip
                        return None
                    tmpdir = self._session_dir()
                dest = tmpdir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', track.id)}.m4a"
                try:
                    clip = Path(youtube.fetch_clip(candidate, _clip_start(track, candidate), CLIP_LENGTH_S, dest))
                except youtube.YouTubeError as e:
                    log.info("clip fetch failed for %s via %s: %s", track.display_name, candidate.url, e)
                    error = e
                    continue
                with self._lock:
                    if self._tmpdir == tmpdir:
                        self._clips[track.id] = clip
                        return clip
                shutil.rmtree(tmpdir, ignore_errors=True)  # cleanup() ran while we were fetching
                return None
            return f"✗ clip failed: {_short(error)}"


_default = Auditioner()
atexit.register(_default.cleanup)


def play(track: Track) -> str:
    """Audition `track` in the background (latest request wins); returns the immediate status string."""
    return _default.play(track)


def stop() -> None:
    """Stop playback and cancel any pending start."""
    _default.stop()


def status() -> str:
    """'' | '⏳ loading A - T…' | '▶ A - T (20 s)' | '✗ no YouTube match for …' | '✗ clip failed: …'."""
    return _default.status()


def cleanup() -> None:
    """Stop the player and delete the session's clips."""
    _default.cleanup()


def set_on_change(callback: Callable[[], None] | None) -> None:
    """Call `callback()` whenever status() changes (from any thread); e.g. `lambda: app.invalidate()`."""
    _default.set_on_change(callback)
