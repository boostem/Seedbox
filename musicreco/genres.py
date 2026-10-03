"""Genre + Beatport metadata lookup: Beatport first, MusicBrainz fallback, Title Case output, in-memory cache.

A provider outage (``ProviderUnavailable``) or crash is never cached: the track is looked up again next time.
"""

from __future__ import annotations

import logging
import re
import threading

from .models import BeatportInfo, ProviderUnavailable, Track

log = logging.getLogger(__name__)

_cache: dict[str, tuple[str | None, str | None, BeatportInfo | None]] = {}
_cache_lock = threading.Lock()
_outage_logged: set[str] = set()  # providers already warned about, so a long outage logs once, not per track

_SMALL_WORDS = {"and", "of", "the", "in", "for", "a", "on", "to", "'n'"}
_CANON = {  # lowercase token (or hyphen/slash/& part) -> canonical spelling
    "uk": "UK", "us": "US", "usa": "USA", "edm": "EDM", "dnb": "DnB", "rnb": "R&B", "ukg": "UKG",
    "idm": "IDM", "ebm": "EBM", "dj": "DJ", "nrg": "NRG",
}  # fmt: skip
_WEAK = {"dance / pop", "dance/pop", "pop", "electronic", "dance"}  # catch-alls: only used if nothing better turns up
_PART_SPLIT = re.compile(r"([-/&])")
_DELIMS = {"-", "/", "&"}


def _title_part(part: str) -> str:
    low = part.lower()
    if low in _CANON:
        return _CANON[low]
    if part[1:] != part[1:].lower():  # already stylised (DnB, EDM, McX): keep as given
        return part
    return part[:1].upper() + part[1:].lower()


def normalize_genre(name: str) -> str:
    """Title Case a genre name, keeping '&', acronyms and stylised tokens ('tech house' -> 'Tech House')."""
    name = " ".join((name or "").split())
    if name.isupper():  # all-caps input carries no casing information
        name = name.lower()
    out: list[str] = []
    for i, token in enumerate(name.split(" ")):
        if i and token.lower() in _SMALL_WORDS:
            out.append(token.lower())
        else:
            out.append("".join(p if p in _DELIMS else _title_part(p) for p in _PART_SPLIT.split(token) if p))
    return " ".join(out)


def _beatport(track: Track) -> BeatportInfo | None:
    from musicreco.genres_beatport import beatport_lookup

    return beatport_lookup(track)


def _musicbrainz(track: Track) -> str | None:
    from musicreco.genres_musicbrainz import musicbrainz_genre

    return musicbrainz_genre(track)


def _clean(genre: object) -> str | None:
    return normalize_genre(genre) if isinstance(genre, str) and genre.strip() else None


def _note_outage(source: str, track: Track, exc: Exception) -> None:
    """WARNING the first time a provider is unavailable in this process, DEBUG afterwards."""
    with _cache_lock:
        first = source not in _outage_logged
        _outage_logged.add(source)
    log.log(
        logging.WARNING if first else logging.DEBUG,
        "%s unavailable (%s); skipping it for %r%s",
        source, exc, track.display_name, " (further outages are logged at DEBUG)" if first else "",
    )


def find_metadata(track: Track) -> tuple[str | None, str | None, BeatportInfo | None]:
    """Return (genre in Title Case, source 'beatport' | 'musicbrainz', Beatport info). Never raises.

    The Beatport info is returned whenever Beatport matched, even if the genre came from MusicBrainz.
    Results affected by a provider outage or crash are not cached.
    """
    try:
        with _cache_lock:
            if track.id in _cache:
                return _cache[track.id]
        genre: str | None = None
        source: str | None = None
        info: BeatportInfo | None = None
        errored = False
        for name in ("beatport", "musicbrainz"):
            try:
                if name == "beatport":
                    info = _beatport(track)
                    found = (info.sub_genre or info.genre) if info else None
                else:
                    found = _musicbrainz(track)
            except ProviderUnavailable as exc:
                errored = True
                _note_outage(name, track, exc)
                continue
            except Exception as exc:  # a broken provider must not block the other one
                errored = True
                log.warning("%s lookup failed for %r: %r", name, track.display_name, exc)
                continue
            candidate = _clean(found)
            if candidate and candidate.casefold() in _WEAK:
                if not genre:  # keep as last resort, try the next source
                    genre, source = candidate, name
            elif candidate:
                genre, source = candidate, name
                break
        result = (genre, source, info)
        if not errored:
            with _cache_lock:
                _cache[track.id] = result
        return result
    except Exception as exc:
        log.warning("find_metadata failed: %s", exc)
        return (None, None, None)


def find_genre(track: Track) -> tuple[str | None, str | None]:
    """Return (genre in Title Case, source 'beatport' | 'musicbrainz') or (None, None). Never raises."""
    genre, source, _ = find_metadata(track)
    return genre, source
