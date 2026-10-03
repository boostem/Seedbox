"""Tunebat API client: search tracks and fetch a seed track with its recommendations."""

from __future__ import annotations

import logging
import time
from typing import Any

import cloudscraper
import requests
from cloudscraper.exceptions import CloudflareException

from .models import SearchPage, Track, TrackDetail

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())  # skipped items are logged, but never dumped on the terminal by default

BASE_URL = "https://api.tunebat.com"
TIMEOUT_S = 30
MAX_TRIES = 3
BACKOFF_S = 3.0  # sleeps BACKOFF_S, then 2x, between tries (tunebat's 429 window lasts several seconds)
MAX_RETRY_AFTER_S = 30.0
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Browser-ish headers from the captured requests. The User-Agent is left to cloudscraper:
# plain `requests` (or an unrelated UA) gets a Cloudflare 403 challenge page.
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://tunebat.com",
    "Referer": "https://tunebat.com/",
}


_MALFORMED = (KeyError, TypeError, ValueError, AttributeError, OverflowError)  # what bad JSON shapes raise


class TunebatError(Exception):
    """Any failure talking to tunebat (network, HTTP status, unexpected payload)."""


class TunebatClient:
    def __init__(self, scraper: requests.Session | None = None):
        self.scraper = scraper or cloudscraper.create_scraper()

    def search(self, term: str, page: int = 1) -> SearchPage:
        """Search tracks by free text; 50 results per page, pages start at 1."""
        term = term.strip()
        if not term:
            raise TunebatError("empty search term")
        data = self._get("/api/tracks/search", {"term": term, "page": page})
        try:
            if not isinstance(data["items"], list):
                raise TypeError("items is not a list")
            return SearchPage(
                page=int(data.get("page") or page),
                total_count=int(data["totalCount"]),
                items=_parse_tracks(data["items"], "search result"),
            )
        except _MALFORMED as e:
            raise TunebatError(f"unexpected search response shape: {e!r}") from e

    def get_track(self, track_id: str) -> TrackDetail:
        """Fetch a track plus its recommended tracks (``data["r"]`` only, never ``"er"``)."""
        data = self._get("/api/tracks", {"trackId": track_id})
        try:
            seed = _parse_track(data)
        except _MALFORMED as e:
            raise TunebatError(f"unexpected track response shape (bad seed track): {e!r}") from e
        recommended = data.get("r")
        if recommended is None:
            recommended = []
        elif not isinstance(recommended, list):
            raise TunebatError(f"unexpected track response shape: r is {type(recommended).__name__}, not a list")
        return TrackDetail(seed=seed, recommended=_parse_tracks(recommended, "recommended track"))

    def _get(self, path: str, params: dict) -> dict:
        """GET ``path`` and return the ``data`` object, retrying transient failures."""
        url = BASE_URL + path
        last = "no attempts made"
        delay = BACKOFF_S
        for attempt in range(MAX_TRIES):
            if attempt:
                time.sleep(delay)
                delay = min(delay * 2, MAX_RETRY_AFTER_S)
            try:
                resp = self.scraper.get(url, params=params, headers=HEADERS, timeout=TIMEOUT_S)
            except (requests.RequestException, CloudflareException) as e:
                last = f"{type(e).__name__}: {e}"
                continue
            if resp.status_code in RETRY_STATUSES:
                last = f"HTTP {resp.status_code}: {_snippet(resp)}"
                delay = max(delay, _retry_after(resp))
                continue
            if resp.status_code != 200:
                hint = " (blocked by Cloudflare?)" if resp.status_code == 403 else ""
                raise TunebatError(f"tunebat returned HTTP {resp.status_code}{hint}: {_snippet(resp)}")
            return _unwrap(resp)
        raise TunebatError(f"tunebat request failed after {MAX_TRIES} tries ({last})")


_STR_FIELDS = ("n", "an", "rd", "k", "c")


def _parse_track(d: Any) -> Track:
    """`Track.from_api` plus type checks it lacks (it would turn a string artist list into letters)."""
    if not isinstance(d, dict):
        raise TypeError(f"track is {type(d).__name__}, not an object")
    if not isinstance(d.get("id"), str) or not d["id"]:
        raise ValueError("missing or invalid track id")
    for key in _STR_FIELDS:
        if d.get(key) is not None and not isinstance(d[key], str):
            raise TypeError(f"{key!r} is {type(d[key]).__name__}, not a string")
    artists = d.get("as")
    if artists is not None and not (isinstance(artists, list) and all(isinstance(a, str) for a in artists)):
        raise TypeError("'as' is not a list of strings")
    images = d.get("ci")
    if images is not None and not (isinstance(images, list) and all(isinstance(i, dict) for i in images)):
        raise TypeError("'ci' is not a list of image objects")
    return Track.from_api(d)


def _parse_tracks(items: list, what: str) -> list[Track]:
    """Parse every item, skipping (and logging) the malformed ones instead of failing the whole list."""
    tracks = []
    for i, d in enumerate(items):
        try:
            tracks.append(_parse_track(d))
        except _MALFORMED as e:
            ident = d.get("id") if isinstance(d, dict) else None
            log.warning("skipping malformed %s #%d%s: %s: %s", what, i, f" (id {ident})" if ident else "",
                        type(e).__name__, e)
    return tracks


def _unwrap(resp: requests.Response) -> dict:
    """Parse the JSON body and return its non-null ``data`` object."""
    try:
        body = resp.json()
    except ValueError as e:
        raise TunebatError(f"tunebat returned non-JSON response: {_snippet(resp)}") from e
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise TunebatError(f"tunebat returned no data (track not found?): {_snippet(resp)}")
    return data


def _retry_after(resp: requests.Response) -> float:
    """Seconds requested by a numeric ``Retry-After`` header (capped), else 0."""
    try:
        return min(float(resp.headers.get("Retry-After", 0)), MAX_RETRY_AFTER_S)
    except ValueError:
        return 0.0


def _snippet(resp: requests.Response, n: int = 160) -> str:
    return " ".join(resp.text[:n].split())
