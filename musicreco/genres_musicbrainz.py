"""MusicBrainz genre lookup (WS/2 JSON API, no key). Fallback source behind Beatport.

``musicbrainz_genre`` returns None only for a genuine "no confident match"; an outage (503/429/5xx, timeout,
connection error, blocked, garbled reply) raises ``ProviderUnavailable``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata

import requests

from .models import ProviderUnavailable, Track

log = logging.getLogger(__name__)

_BASE = "https://musicbrainz.org/ws/2"
_HEADERS = {"User-Agent": "MusicReco/0.1 (personal DJ library tool)", "Accept": "application/json"}
_TIMEOUT = 20
_MIN_INTERVAL = 1.1  # seconds between request starts, across all threads
_DURATION_TOL_S = 20.0
_RETRY_S = 2.0  # before the single retry, unless the server sends a numeric Retry-After
_MAX_RETRY_AFTER_S = 10.0  # a longer Retry-After is an outage, not a blip
_UNAVAILABLE = {403, 429}  # plus every 5xx: blocked or throttled, worth retrying later

_rate_lock = threading.Lock()
_last_request = float("-inf")


def _throttle() -> None:
    """Block until >= _MIN_INTERVAL has passed since the previous request (any thread)."""
    global _last_request
    with _rate_lock:
        wait = _last_request + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _retry_delay(resp: requests.Response) -> float | None:
    """Seconds to wait before retrying a 503; None if the server asks for longer than we will wait."""
    try:
        delay = float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return _RETRY_S
    if delay != delay or delay > _MAX_RETRY_AFTER_S:  # NaN or too long
        return None
    return max(delay, 0.0)


def _get(path: str, params: dict) -> dict | None:
    """GET a WS/2 endpoint as JSON; one retry on 503 / connection error.

    Returns None for a definitive miss (404 or another client error); raises ProviderUnavailable for
    anything transient, so a failed request is never mistaken for "no results".
    """
    for attempt in (1, 2):
        _throttle()
        try:
            resp = requests.get(
                f"{_BASE}/{path}", params={**params, "fmt": "json"}, headers=_HEADERS, timeout=_TIMEOUT
            )
        except requests.Timeout as exc:
            raise ProviderUnavailable(f"MusicBrainz timed out ({path})") from exc
        except requests.RequestException as exc:
            if attempt == 1:
                log.debug("MusicBrainz request failed (%s): %s; retrying", path, exc)
                time.sleep(_RETRY_S)
                continue
            raise ProviderUnavailable(f"MusicBrainz connection failed ({path}): {exc}") from exc
        status = resp.status_code
        if status == 503 and attempt == 1:
            delay = _retry_delay(resp)
            if delay is not None:
                time.sleep(delay)
                continue
        if status >= 500 or status in _UNAVAILABLE:
            raise ProviderUnavailable(f"MusicBrainz {path} -> HTTP {status}")
        if status != 200:
            log.debug("MusicBrainz %s -> HTTP %s", path, status)
            return None
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderUnavailable(f"MusicBrainz {path} -> invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderUnavailable(f"MusicBrainz {path} -> unexpected JSON")
        return data
    raise ProviderUnavailable(f"MusicBrainz {path} failed")


# --- text helpers -----------------------------------------------------------------------------

_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
_PAREN_RE = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]\s*$")
_DASH_RE = re.compile(r"\s+[-–—]\s+(.+)$")
_FEAT_RE = re.compile(r"^(?:feat\b|ft\b|featuring\b)", re.I)
_GENERIC_RE = re.compile(
    r"^(?:(?:extended|original|radio|club|album|single|short|full[ -]length|clean|explicit)"
    r"(?:\s+(?:mix|edit|version))?|(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?)$",
    re.I,
)
_VARIANT_RE = re.compile(r"\b(?:remix|rework|re-?edit|edit|mix|dub|vip|flip|bootleg|version)\b", re.I)


def _esc(s: str) -> str:
    """Escape Lucene special characters."""
    return _LUCENE_SPECIAL.sub(r"\\\1", s)


def _norm(s: str) -> str:
    """Lowercase, accent-fold, drop apostrophes, other punctuation -> single spaces ('&' -> 'and')."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ")
    s = re.sub(r"['’‘`]", "", s)
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def _split_title(title: str) -> tuple[str, str]:
    """Split into (bare title, normalised remix/variant tag).

    Trailing '(feat. X)', '- Extended Mix', '(Original Mix)', 'Remastered' groups are dropped; a trailing
    '(X Remix)' / '- X Rework' group is returned as the variant so remixes don't match originals.
    """
    base, variant = title.strip(), ""
    while True:
        m = _PAREN_RE.search(base) or _DASH_RE.search(base)
        if not m:
            break
        inner = m.group(1).strip()
        if _FEAT_RE.match(inner) or _GENERIC_RE.match(inner):
            pass
        elif _VARIANT_RE.search(inner):
            variant = f"{inner} {variant}"
        else:
            break
        base = base[: m.start()].rstrip()
    return base, _norm(variant)


def _variant_ok(a: str, b: str) -> bool:
    if a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    return bool(ta and tb and (ta <= tb or tb <= ta))


# --- candidate selection ------------------------------------------------------------------------


def _credit_names(rec: dict) -> tuple[set[str], str]:
    """(normalised individual credit names, normalised full credit string)."""
    names: set[str] = set()
    full = ""
    for c in rec.get("artist-credit") or []:
        if not isinstance(c, dict):
            continue
        for n in (c.get("name"), (c.get("artist") or {}).get("name")):
            if n:
                names.add(_norm(n))
        full += (c.get("name") or "") + (c.get("joinphrase") or "")
    return names, _norm(full)


def _artist_in_credit(artist: str, names: set[str], full: str) -> bool:
    a = _norm(artist)
    return bool(a) and (a in names or f" {a} " in f" {full} ")


def _rank_candidates(track: Track, base: str, variant: str, recs: list) -> list[dict]:
    """Recordings matching title + artist (+ duration when MB has a length), best first."""
    want_base = _norm(base)
    scored: list[tuple[tuple, dict]] = []
    for rec in recs:
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        rbase, rvariant = _split_title(rec.get("title") or "")
        if _norm(rbase) != want_base or not _variant_ok(variant, rvariant):
            continue
        names, full = _credit_names(rec)
        if not any(_artist_in_credit(a, names, full) for a in track.artists):
            continue
        length = rec.get("length")
        if length and track.duration_ms:
            diff = abs(length - track.duration_ms) / 1000
            if diff > _DURATION_TOL_S:
                continue
            dur = 2 - diff / (_DURATION_TOL_S + 1)  # known and close: always above "unknown" (1)
        else:
            dur = 1
        key = (variant == rvariant, dur, bool(rec.get("tags")), rec.get("score") or 0)
        scored.append((key, rec))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [rec for _, rec in scored]


# --- genre extraction ----------------------------------------------------------------------------


def _merge(into: dict[str, int], genres: object) -> None:
    for g in genres or []:
        if isinstance(g, dict) and g.get("name"):
            into[g["name"]] = into.get(g["name"], 0) + int(g.get("count") or 0)


def _pick(votes: dict[str, int]) -> str | None:
    """Highest vote count; ties go to the longer (more specific) name."""
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


def _genre_from_recording(rec: dict, track: Track) -> str | None:
    """Recording genres, else matching release / release-group genres, else credited-artist genres."""
    votes: dict[str, int] = {}
    _merge(votes, rec.get("genres"))
    if votes:
        return _pick(votes)

    wanted = {_norm(_split_title(x)[0]) for x in (track.album, track.name) if x}
    seen: set[str] = set()
    for rel in rec.get("releases") or []:
        for ent in (rel, rel.get("release-group") or {}):
            eid = ent.get("id")
            if eid in seen or _norm(_split_title(ent.get("title") or "")[0]) not in wanted:
                continue
            seen.add(eid)
            _merge(votes, ent.get("genres"))
    if votes:
        return _pick(votes)

    by_name = {}
    for c in rec.get("artist-credit") or []:
        art = c.get("artist") or {}
        by_name[_norm(art.get("name") or c.get("name") or "")] = art
    for a in track.artists:  # the primary artist only (first one MB credits), never a guest
        art = by_name.get(_norm(a))
        if art:
            _merge(votes, art.get("genres"))
            return _pick(votes)
    return None


def _genre_from_artist_search(track: Track) -> str | None:
    """No recording match: look up the primary artist by exact name (must be unambiguous)."""
    name = track.artists[0]
    data = _get("artist", {"query": f'artist:"{_esc(name)}"', "limit": 5})
    hits = [
        a
        for a in (data or {}).get("artists") or []
        if isinstance(a, dict) and (a.get("score") or 0) >= 90 and _norm(a.get("name") or "") == _norm(name)
    ]
    if len(hits) != 1:
        return None
    art = _get(f"artist/{hits[0]['id']}", {"inc": "genres"})
    votes: dict[str, int] = {}
    _merge(votes, (art or {}).get("genres"))
    return _pick(votes)


def _lookup(track: Track) -> str | None:
    if not track.name or not track.artists:
        return None
    base, variant = _split_title(track.name)
    query = f'recording:"{_esc(base)}" AND artist:"{_esc(track.artists[0])}"'
    data = _get("recording", {"query": query, "limit": 15})
    if data is None:  # definitive client error (not an outage, that raises): don't fall back to artist lookups
        return None
    cands = _rank_candidates(track, base, variant, data.get("recordings") or [])
    if not cands:
        return _genre_from_artist_search(track)
    for cand in cands[:2]:
        rec = _get(f"recording/{cand['id']}", {"inc": "genres+artists+releases+release-groups"})
        genre = _genre_from_recording(rec, track) if rec else None
        if genre:
            return genre
    return None


def musicbrainz_genre(track: Track) -> str | None:
    """Most-voted MusicBrainz genre (recording, else release group, else artist); None if not confident.

    Raises ProviderUnavailable if MusicBrainz could not be queried (503, timeout, connection error, blocked).
    """
    return _lookup(track)
