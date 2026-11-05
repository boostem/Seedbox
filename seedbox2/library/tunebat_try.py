"""First run's "no music to hand? try one track" (#80): search tunebat, then fetch one result from YouTube into the
music folder's Imported folder and import it. musicreco's tunebat and YouTube code is reused, not forked; the full
search → crate → download flow is not part of this. A gap in a set (#64) gets its online suggestions here too:
`recommendations()` of each neighbour, which `download()` can then fetch.

`download()` runs in the background; each step is a `tunebat.download` event (`{id, state, error}`, the state being
`queued`, `finding`, `downloading`, `done` or `failed`) with an error saying what to do about it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 1.2  # tunebat answers 429 after about 4 quick calls (seedbox/tunebat_service.py)
MAX_RESULTS = 12


class UnknownTrack(Exception):
    """A track that no search returned."""


class TunebatUnavailable(Exception):
    """Tunebat can't be reached or sent something unexpected."""


def _plain(text: str | None) -> str:
    """A title or artist to compare: lower case, no brackets and no punctuation."""
    return re.sub(r"[^\w]+", " ", re.sub(r"[(\[].*?[)\]]", " ", (text or "").casefold())).strip()


def same_track(name_a: str, artist_a: str, name_b: str, artist_b: str) -> bool:
    """The same title, and an artist word in common if both have an artist. "(Extended Mix)" and the like don't count."""
    title_a, title_b = _plain(name_a), _plain(name_b)
    if not title_a or title_a != title_b:
        return False
    words_a, words_b = set(_plain(artist_a).split()), set(_plain(artist_b).split())
    return not words_a or not words_b or bool(words_a & words_b)


def _row(track: Any) -> dict:
    return {"id": track.id, "name": track.name, "artists": track.artists, "album": track.album,
            "duration": track.duration_ms / 1000, "bpm": track.bpm, "camelot": track.camelot,
            "cover_url": track.cover_url}  # fmt: skip


def _write_tags(path: Any, track: Any) -> None:
    """The title and artist, which the download doesn't carry, and tunebat's tempo and key as hints."""
    from mutagen.id3 import ID3, TALB, TBPM, TIT2, TKEY, TPE1, ID3NoHeaderError

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TIT2(encoding=3, text=track.name))
    tags.add(TPE1(encoding=3, text=", ".join(track.artists)))
    if track.album:
        tags.add(TALB(encoding=3, text=track.album))
    if track.bpm:
        tags.add(TBPM(encoding=3, text=str(round(track.bpm))))
    if track.camelot:
        tags.add(TKEY(encoding=3, text=track.camelot))
    tags.save(path)


class TunebatTry:
    def __init__(self, music: Any, publish: Callable[[str, dict], None], client: Any = None) -> None:
        self.music = music
        self.publish = publish
        self._client = client
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._tracks: dict[str, Any] = {}
        self._states: dict[str, str] = {}
        self._recommended: dict[str, list[Any]] = {}  # a seed's id: it asks once per run, a track's list hardly changes

    def _call(self, method: str, *args: Any) -> Any:
        """One client call. The caller holds the lock, which keeps them one at a time, MIN_INTERVAL_S apart."""
        from musicreco.tunebat import TunebatClient, TunebatError

        wait = self._last_call + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            if self._client is None:
                self._client = TunebatClient()
            return getattr(self._client, method)(*args)
        except TunebatError as e:
            log.warning("tunebat %s failed: %s", method, e)
            raise TunebatUnavailable(str(e)) from e
        finally:
            self._last_call = time.monotonic()

    def search(self, term: str) -> dict:
        """The first page's best results for a free-text search. TunebatUnavailable when that can't be done."""
        with self._lock:
            page = self._call("search", term)
            items = page.items[:MAX_RESULTS]
            self._tracks.update((t.id, t) for t in items)
        return {"total": page.total_count, "items": [_row(t) for t in items]}

    def recommendations(self, title: str, artist: str, track_id: str | None = None) -> tuple[str | None, list[Any]]:
        """(The track's tunebat id, the tracks tunebat recommends from it), which `download()` then knows. Without an id it
        searches for the title and artist; (None, []) if nothing there is this track."""
        with self._lock:
            if track_id is None:
                if not title.strip():
                    return None, []
                page = self._call("search", f"{artist} {title}".strip())
                found = next((t for t in page.items if same_track(t.name, t.artist_str, title, artist)), None)
                if found is None:
                    return None, []
                track_id = found.id
            items = self._recommended.get(track_id)
            if items is None:
                items = self._call("get_track", track_id).recommended
                self._recommended[track_id] = items
                self._tracks.update((t.id, t) for t in items)
        return track_id, items

    def download(self, track_id: str) -> dict:
        """Start fetching a track from the last searches. Asking again while it runs or once it is done does nothing."""
        with self._lock:
            track = self._tracks.get(track_id)
            if track is None:
                raise UnknownTrack(track_id)
            state = self._states.get(track_id)
            if state in ("queued", "finding", "downloading", "done"):
                return {"id": track_id, "state": state, "error": None}
            self._states[track_id] = "queued"
        threading.Thread(target=self._run, args=(track,), name="seedbox2-tunebat-try", daemon=True).start()
        return {"id": track_id, "state": "queued", "error": None}

    def _emit(self, track_id: str, state: str, error: str | None = None) -> None:
        with self._lock:
            self._states[track_id] = state
        self.publish("tunebat.download", {"id": track_id, "state": state, "error": error})

    def _run(self, track: Any) -> None:
        from musicreco import paths as binaries
        from musicreco import youtube

        try:
            if binaries.ffmpeg_path() is None:
                self._emit(track.id, "failed", "ffmpeg is missing, and the download needs it. Install it, then try again.")
                return
            self._emit(track.id, "finding")
            found = youtube.find_match(track)
            if found.match is None:
                self._emit(track.id, "failed", "No matching audio on YouTube for this one. Try another track.")
                return
            self._emit(track.id, "downloading")
            dest = self.music.upload_path(f"{', '.join(track.artists)} - {track.name}.mp3")
            youtube.download(found.match, dest)
            _write_tags(dest, track)
            self.music.import_files([dest])
            self._emit(track.id, "done")
        except Exception as e:  # noqa: BLE001 - the thread's last stop: whatever went wrong goes to the screen
            log.warning("couldn't fetch %s: %s", track.id, e)
            self._emit(track.id, "failed", f"The download failed ({e}). Check the connection and try again.")
