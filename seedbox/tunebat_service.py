"""One shared, throttled, caching tunebat client for the whole server, plus Spotify/tunebat link parsing."""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TypeVar

from musicreco.models import SearchPage, Track, TrackDetail
from musicreco.tunebat import TunebatClient, TunebatError

MIN_INTERVAL_S = 1.2  # tunebat 429s after ~4 quick calls
SEARCH_TTL_S = 600.0
SEARCH_CACHE_SIZE = 200
_T = TypeVar("_T")

_ID = r"([0-9A-Za-z]{22})"
_SPOTIFY_URL = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?track/" + _ID + r"(?![0-9A-Za-z])")
_SPOTIFY_URI = re.compile(r"spotify:track:" + _ID + r"$")
_TUNEBAT_URL = re.compile(r"tunebat\.com/Info/(?:[^?#\s]*/)?" + _ID + r"(?:[/?#]|$)", re.IGNORECASE)
_BARE_ID = re.compile(_ID)


def parse_track_ref(term: str) -> tuple[str, bool] | None:
    """(Spotify track ID, is_bare_id) for a Spotify URL/URI, a tunebat Info URL or a bare 22-char ID; else None."""
    term = term.strip()
    for pattern in (_SPOTIFY_URL, _SPOTIFY_URI, _TUNEBAT_URL):
        if m := pattern.search(term):
            return m.group(1), False
    if _BARE_ID.fullmatch(term) and (
        any(c.isdigit() for c in term) or (any(c.isupper() for c in term) and any(c.islower() for c in term))
    ):
        return term, True
    return None


class TunebatService:
    """Serialises every tunebat call (>= MIN_INTERVAL_S apart) and caches results for the session.

    get_track results are kept for the whole session (a crate re-opened or downloaded later costs no call);
    search pages for SEARCH_TTL_S.
    """

    def __init__(self, client: TunebatClient | None = None, min_interval_s: float = MIN_INTERVAL_S) -> None:
        self._client = client
        self.min_interval_s = min_interval_s
        self._call_lock = threading.Lock()
        self._last_call = 0.0
        self._cache_lock = threading.Lock()
        self._details: dict[str, TrackDetail] = {}
        self._id_locks: dict[str, threading.Lock] = {}
        self._searches: OrderedDict[tuple[str, int], tuple[float, SearchPage]] = OrderedDict()
        self._tracks: dict[str, Track] = {}
        self.calls = 0

    @property
    def client(self) -> TunebatClient:
        if self._client is None:
            self._client = TunebatClient()
        return self._client

    def _call(self, fn: Callable[[], _T]) -> _T:
        with self._call_lock:
            wait = self._last_call + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                self.calls += 1
                return fn()
            finally:
                self._last_call = time.monotonic()

    def _remember(self, tracks: list[Track]) -> None:
        with self._cache_lock:
            if len(self._tracks) > 50_000:
                self._tracks.clear()
            for t in tracks:
                self._tracks[t.id] = t

    def get_track(self, track_id: str) -> TrackDetail:
        """Seed track + its "r" recommendations (cached for the session)."""
        with self._cache_lock:
            if hit := self._details.get(track_id):
                return hit
            id_lock = self._id_locks.setdefault(track_id, threading.Lock())
        with id_lock:  # a concurrent request for the same ID waits here, then hits the cache
            with self._cache_lock:
                if hit := self._details.get(track_id):
                    return hit
            detail = self._call(lambda: self.client.get_track(track_id))
            with self._cache_lock:
                self._details[track_id] = detail
                if detail.seed.id != track_id:  # tunebat may answer with a relinked ID
                    self._details.setdefault(detail.seed.id, detail)
                self._id_locks.pop(track_id, None)
        self._remember([detail.seed, *detail.recommended])
        return detail

    def search(self, term: str, page: int = 1) -> SearchPage:
        """One page of tunebat's text search (cached briefly)."""
        key = (" ".join(term.split()).casefold(), page)
        now = time.monotonic()
        with self._cache_lock:
            hit = self._searches.get(key)
            if hit and now - hit[0] < SEARCH_TTL_S:
                self._searches.move_to_end(key)
                return hit[1]
        result = self._call(lambda: self.client.search(term, page))
        with self._cache_lock:
            self._searches[key] = (time.monotonic(), result)
            while len(self._searches) > SEARCH_CACHE_SIZE:
                self._searches.popitem(last=False)
        self._remember(result.items)
        return result

    def lookup(self, term: str, page: int = 1) -> SearchPage:
        """Search, except that a Spotify/tunebat link or bare track ID returns just that track."""
        term = term.strip()
        if not term:
            raise TunebatError("empty search term")
        ref = parse_track_ref(term)
        if ref is not None:
            track_id, bare = ref
            try:
                seed = self.get_track(track_id).seed
                return SearchPage(page=1, total_count=1, items=[seed])
            except TunebatError:
                if not bare:
                    raise
        return self.search(term, page)

    def track(self, track_id: str) -> Track | None:
        """Any track seen in this session's searches or recommendations."""
        with self._cache_lock:
            return self._tracks.get(track_id)
