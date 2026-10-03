"""Beatport genre lookup (no API key).

Source (verified Oct 2026): ``GET https://www.beatport.com/search/tracks?q=<query>`` is a server-rendered
Next.js page. ``<script id="__NEXT_DATA__">`` holds up to 150 hits at
``props.pageProps.dehydratedState.queries[i].state.data.data`` (the query whose ``queryKey[0]`` is
``"search-tracks"``). Each hit is a search-index document, not the v4 API shape: ``track_name``,
``mix_name``, ``artists[{artist_name, artist_type_name: "Artist"|"Remixer"}]``, ``length`` (ms),
``genre`` (a *list* of ``{genre_id, genre_name}``), optional ``sub_genre`` (``{sub_genre_id,
sub_genre_name}``), ``downloads``/``plays`` and ``is_dj_edit``-style flags. Artist names may carry
disambiguators such as ``"FISHER (OZ)"``. The site is behind Cloudflare but currently serves plain
clients without a challenge; cloudscraper is used in case one appears. ``key_name`` is spelled
``"Eb Minor"`` / ``"A# Minor"`` (flats and sharps both occur); ``label`` is ``{label_id, label_name}``.

``beatport_lookup`` returns None only for a genuine "no confident match"; every transient failure
(timeout, connection error, 429/5xx after the retry, 403/challenge, the post-block pause) raises
``ProviderUnavailable`` so callers never cache or act on a miss that was really an outage.

The same recording is often listed several times (label release, major-label radio-edit feed,
compilations, Beatport "DJ edits") with *different* genres — feed copies are frequently the catch-all
"Dance / Pop". So after matching title, artist and version (remixer/rework), the most-downloaded
release is taken as the canonical DJ release rather than the copy whose mix name matches exactly.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

import cloudscraper
import requests

from .models import BeatportInfo, ProviderUnavailable, Track

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.beatport.com/search/tracks"
TIMEOUT_S = 20
MIN_INTERVAL_S = 1.0  # between any two Beatport requests, across threads
RETRY_STATUSES = {429, 500, 502, 503, 504}
DEFAULT_RETRY_S = 2.0
MAX_RETRY_AFTER_S = 10.0
BLOCKED_PAUSE_S = 300.0  # after a 403/challenge, leave Beatport alone for a while
TRACK_URL = "https://www.beatport.com/track/{slug}/{id}"
LENGTH_TOL_S = 5.0  # a copy only counts as "the same edit" if its length is this close to the Spotify track's

_lock = threading.Lock()
_session: requests.Session | None = None
_last_request = 0.0
_paused_until = 0.0

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_FEAT_BRACKET = re.compile(r"[(\[]\s*(?:feat|ft|featuring|with)\b[^)\]]*[)\]]", re.I)
_FEAT_TAIL = re.compile(r"(?:^|\s)(?:feat|ft|featuring)\b.*$", re.I)
_BRACKET = re.compile(r"[(\[][^)\]]*[)\]]")
_VERSION_HINT = re.compile(
    r"\b(?:remix|mix|edit|rework|version|dub|vip|bootleg|flip|extended|radio|rmx|remaster(?:ed)?"
    r"|instrumental|acapella|live|refix|reprise)\b",
    re.I,
)
_ARTIST_TAG = re.compile(r"\s*\([^)]*\)\s*$")
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\+|/|\bx\b|\band\b|\bvs\b\.?|\bfeat\b\.?|\bft\b\.?)\s*", re.I)
# Words that describe a cut rather than identify a version ("Extended Mix" ~ "Radio Edit" ~ "Original Mix").
_GENERIC = frozenset(
    "original extended mix edit radio short club version intro outro clean dirty explicit main remix "
    "rework remaster remastered re rmx single album full length long the and a of feat ft with".split()
)


@dataclass(frozen=True)
class _Title:
    text: str  # cleaned original-case title, for queries
    exact: str  # normalised title without feat./version parts
    loose: str  # same with every remaining bracketed part dropped
    version: tuple[str, ...]  # identifying version words, e.g. ("fisher",) for "FISHER Rework"


def _norm(s: str) -> str:
    """Casefold, strip accents/apostrophes/punctuation, '&' -> 'and'."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r"['’‘`´]", "", s).replace("&", " and ")
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def _parse_title(title: str, mix: str = "") -> _Title:
    """Split a Spotify title ("X - Y Remix", "X (Y Remix)") or Beatport name + mix_name."""
    t = _FEAT_BRACKET.sub(" ", title)
    parts = [mix]
    for group in _BRACKET.findall(t):
        if _VERSION_HINT.search(group):
            parts.append(group.strip("()[] "))
            t = t.replace(group, " ")
    if " - " in t:
        t, tail = t.split(" - ", 1)
        parts.append(tail)
    t = " ".join(_FEAT_TAIL.sub("", t).split())
    exact = _norm(t)
    skip = _GENERIC | set(exact.split())
    version: list[str] = []
    for p in parts:
        for w in _norm(_FEAT_TAIL.sub("", p)).split():
            if w not in skip and w not in version:
                version.append(w)
    return _Title(t, exact, _norm(_BRACKET.sub(" ", t)), tuple(version))


def _version_match(a: Iterable[str], b: Iterable[str]) -> bool:
    a, b = set(a), set(b)
    if a == b:
        return True
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.5


def _artist_keys(names: Iterable[str]) -> set[str]:
    """Normalised full names plus their parts ("Mr. Belt & Wezol" -> mr belt & wezol, mr belt, wezol)."""
    keys: set[str] = set()
    for name in names:
        name = _ARTIST_TAG.sub("", name or "")
        keys.add(_norm(name))
        keys.update(k for k in (_norm(p) for p in _ARTIST_SPLIT.split(name)) if len(k) >= 2)
    keys.discard("")
    return keys


def _first_dict(v: object) -> dict | None:
    if isinstance(v, list):
        v = v[0] if v else None
    return v if isinstance(v, dict) else None


def _genre_of(item: dict) -> str | None:
    """Sub-genre name if present, else genre name, as Beatport spells it."""
    sub = _first_dict(item.get("sub_genre")) or {}
    gen = _first_dict(item.get("genre")) or {}
    for name in (sub.get("sub_genre_name"), sub.get("name"), gen.get("genre_name"), gen.get("name")):
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


_KEY_RE = re.compile(r"^\s*([A-G])\s*([#b♯♭]?)\s*(major|minor|maj|min|m)?\s*$", re.I)
_NOTE_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def camelot_from_key(name: str | None) -> str | None:
    """Camelot code for a key name such as "Eb Minor", "A# Major" or "G♯ minor"; None if unparseable."""
    m = _KEY_RE.match(name or "")
    if not m or not m.group(3):
        return None
    letter, accidental, mode = m.group(1).upper(), m.group(2), m.group(3).lower()
    pc = _NOTE_PC[letter] + (1 if accidental in ("#", "♯") else -1 if accidental in ("b", "B", "♭") else 0)
    minor = mode.startswith("mi") or mode == "m"
    if minor:
        pc += 3  # relative major shares the Camelot number
    # Position on the circle of fifths (C=0, G=1, ...) shifted so C major = 8B, as on the Camelot wheel.
    number = (pc * 7 % 12 + 7) % 12 + 1
    return f"{number}{'A' if minor else 'B'}"


def _str(v: object) -> str | None:
    return v.strip() or None if isinstance(v, str) else None


def _slug(name: str) -> str:
    return _norm(name).replace(" ", "-") or "track"


def _info(genre_hit: dict, edit_hit: dict | None) -> BeatportInfo:
    """Map search-index hits to BeatportInfo, keeping only what the hits actually carry.

    ``genre_hit`` is the canonical (most-downloaded) copy of the matched version: it supplies genre/sub-genre and,
    failing an ``edit_hit``, the label, link and remixers. ``edit_hit`` is the copy with the same length as the
    Spotify track: it supplies everything specific to that edit (ISRC, mix name, BPM, key, catalog number, date).
    With no ``edit_hit`` those fields stay empty rather than describing a different edit.
    """
    sub = _first_dict(genre_hit.get("sub_genre")) or {}
    gen = _first_dict(genre_hit.get("genre")) or {}
    info = BeatportInfo(
        genre=_str(gen.get("genre_name")) or _str(gen.get("name")),
        sub_genre=_str(sub.get("sub_genre_name")) or _str(sub.get("name")),
    )
    link = edit_hit or genre_hit
    for hit in (edit_hit, genre_hit):  # label: the edit's own release, else the genre copy's
        label = (hit or {}).get("label")
        info.label = info.label or (_str(label.get("label_name")) if isinstance(label, dict) else None)
    track_id = link.get("track_id")
    info.track_id = track_id if isinstance(track_id, int) and not isinstance(track_id, bool) else None
    if info.track_id:
        info.url = TRACK_URL.format(slug=_slug(str(link.get("track_name") or "")), id=info.track_id)
    for a in link.get("artists") or []:  # remixers identify the version, so any copy of it will do
        if isinstance(a, dict) and a.get("artist_type_name") == "Remixer":
            name = _str(_ARTIST_TAG.sub("", a.get("artist_name") or "")) or _str(a.get("artist_name"))
            if name and name not in info.remixers:
                info.remixers.append(name)
    if edit_hit is None:
        return info
    bpm = edit_hit.get("bpm")
    info.catalog_number = _str(edit_hit.get("catalog_number"))
    info.isrc = _str(edit_hit.get("isrc"))
    info.mix_name = _str(edit_hit.get("mix_name"))
    info.bpm = float(bpm) if isinstance(bpm, (int, float)) and not isinstance(bpm, bool) and bpm > 0 else None
    info.key = _str(edit_hit.get("key_name"))
    info.camelot = camelot_from_key(info.key)
    info.publish_date = (_str(edit_hit.get("publish_date")) or "").split("T")[0] or None
    return info


def _query(*parts: str) -> str:
    q = re.sub(r"[’‘`´]", "'", " ".join(p for p in parts if p))
    return " ".join(re.sub(r"[()\[\],]", " ", q).split())


def _retry_delay(resp: requests.Response) -> float:
    try:
        return min(max(float(resp.headers.get("Retry-After", DEFAULT_RETRY_S)), 0.0), MAX_RETRY_AFTER_S)
    except ValueError:
        return DEFAULT_RETRY_S


def _block(reason: str) -> ProviderUnavailable:
    """Start the pause window and build the error to raise."""
    global _paused_until
    _paused_until = time.monotonic() + BLOCKED_PAUSE_S
    return ProviderUnavailable(f"Beatport blocked the request ({reason}); pausing lookups for {BLOCKED_PAUSE_S:.0f}s")


def _get(params: dict[str, str]) -> str:
    """Rate-limited, serialised GET of the search page. Raises ProviderUnavailable on any failure."""
    global _session, _last_request
    with _lock:
        remaining = _paused_until - time.monotonic()
        if remaining > 0:
            raise ProviderUnavailable(f"Beatport paused for another {remaining:.0f}s after being blocked")
        if _session is None:
            _session = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "darwin", "mobile": False})
        for attempt in range(2):
            wait = _last_request + MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                resp = _session.get(SEARCH_URL, params=params, timeout=TIMEOUT_S)
            except requests.Timeout as e:
                raise ProviderUnavailable(f"Beatport search timed out for {params.get('q')!r}") from e
            except requests.RequestException as e:
                if attempt == 0:
                    log.debug("Beatport search failed for %r (%s); retrying", params.get("q"), e)
                    continue
                raise ProviderUnavailable(f"Beatport connection failed: {e}") from e
            except Exception as e:  # cloudscraper challenge errors etc.
                raise _block(repr(e)) from e
            finally:
                _last_request = time.monotonic()
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in RETRY_STATUSES and attempt == 0:
                time.sleep(_retry_delay(resp))
                continue
            if resp.status_code == 403:
                raise _block("HTTP 403")
            raise ProviderUnavailable(f"Beatport search HTTP {resp.status_code} for {params.get('q')!r}")
    raise ProviderUnavailable("Beatport search failed")


def _search(query: str) -> list[dict]:
    """Track hits for a query ([] if none). Raises ProviderUnavailable if the page isn't a usable results page."""
    html = _get({"q": query})
    m = _NEXT_DATA.search(html)
    if not m:
        raise _block("no __NEXT_DATA__: challenge page or layout change")
    try:
        queries = json.loads(m.group(1))["props"]["pageProps"]["dehydratedState"]["queries"]
        for q in queries:
            if (q.get("queryKey") or [None])[0] == "search-tracks":
                items = q["state"]["data"]["data"]
                return [i for i in items if isinstance(i, dict)]
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise ProviderUnavailable(f"unexpected Beatport page shape: {e!r}") from e
    raise ProviderUnavailable("Beatport page has no search-tracks query (layout change?)")


def _candidates(want: _Title, artists: set[str], items: list[dict]) -> list[tuple[bool, dict]]:
    """Hits with matching title, version and >=1 shared artist, flagged exact vs loose title match.

    Hits credited to a remixer the Spotify track doesn't list are dropped: Beatport sometimes leaves
    ``mix_name`` generic on remix releases.
    """
    out = []
    for it in items:
        got = _parse_title(str(it.get("track_name") or ""), str(it.get("mix_name") or ""))
        exact = got.exact == want.exact
        if not (exact or (got.loose and got.loose == want.loose)):
            continue
        if not _version_match(want.version, got.version):
            continue
        credits = [a for a in it.get("artists") or [] if isinstance(a, dict)]
        if not artists & _artist_keys(a.get("artist_name") or "" for a in credits):
            continue
        remixers = [a.get("artist_name") or "" for a in credits if a.get("artist_type_name") == "Remixer"]
        if remixers and not artists & _artist_keys(remixers):
            continue
        if _genre_of(it):
            out.append((exact, it))
    return out


def _is_derived(it: dict) -> bool:
    return bool(it.get("is_dj_edit") or it.get("is_dj_version") or it.get("is_ugc_remix"))


def _pool(cands: list[tuple[bool, dict]]) -> list[dict]:
    """Hits to choose from: exact title matches if there are any, else loose ones."""
    if any(exact for exact, _ in cands):
        cands = [c for c in cands if c[0]]
    return [it for _, it in cands]


def _pick(cands: list[tuple[bool, dict]]) -> dict:
    """Canonical release: exact title over loose, real release over DJ edit, then most downloaded."""

    def rank(it: dict) -> tuple:
        derived = _is_derived(it)
        return (not derived, int(it.get("downloads") or 0), int(it.get("plays") or 0), float(it.get("score") or 0))

    return max(_pool(cands), key=rank)


def _pick_edit(cands: list[tuple[bool, dict]], duration_s: float) -> dict | None:
    """The copy that is the same edit as the Spotify track: closest Beatport length within LENGTH_TOL_S, else None.

    Real releases beat DJ edits (which carry their own ISRC); then the closest length, then most downloaded.
    """
    if duration_s <= 0:
        return None
    scored = []
    for it in _pool(cands):
        length = it.get("length")
        if isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0:
            diff = abs(length / 1000 - duration_s)
            if diff <= LENGTH_TOL_S:
                scored.append((not _is_derived(it), -diff, int(it.get("downloads") or 0), it))
    return max(scored, key=lambda t: t[:3])[3] if scored else None


def _lookup(track: Track) -> BeatportInfo | None:
    want = _parse_title(track.name)
    artists = _artist_keys(track.artists)
    if not want.exact or not artists:
        return None
    queries = [_query(_ARTIST_TAG.sub("", track.artists[0]), want.text, *want.version)]
    fallback = _query(want.text, *want.version, *track.artists[1:3])
    if fallback not in queries:
        queries.append(fallback)
    for q in queries:
        cands = _candidates(want, artists, _search(q))
        if cands:
            best = _pick(cands)
            edit = _pick_edit(cands, track.duration_s)
            info = _info(best, edit)
            log.debug(
                "Beatport: %s -> %s (%s) by %s [id %s, %s dl] = %s; edit copy: %s",
                track.display_name, best.get("track_name"), best.get("mix_name"),
                ", ".join(a.get("artist_name", "") for a in best.get("artists") or []),
                best.get("track_id"), best.get("downloads"), info.sub_genre or info.genre,
                f"{edit.get('mix_name')} [id {edit.get('track_id')}]" if edit else f"none within {LENGTH_TOL_S:g}s",
            )
            return info
    log.debug("Beatport: no confident match for %s", track.display_name)
    return None


def beatport_lookup(track: Track) -> BeatportInfo | None:
    """Metadata of the best-matching Beatport track; None if no confident match.

    Genre comes from the canonical copy of the matched version; ISRC, mix name, BPM, key, catalog number and date
    only from a copy within LENGTH_TOL_S of ``track.duration_s`` (the same edit), else they are left empty.

    Raises ProviderUnavailable if Beatport could not be queried (timeout, 429/5xx, blocked, paused).
    """
    return _lookup(track)


def beatport_genre(track: Track) -> str | None:
    """Beatport sub-genre (else genre) of the best-matching track; None if no confident match.

    Raises ProviderUnavailable like ``beatport_lookup``.
    """
    info = beatport_lookup(track)
    return (info.sub_genre or info.genre) if info else None
