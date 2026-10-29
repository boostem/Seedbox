"""Find a track on YouTube Music / YouTube and download it as MP3 via the yt-dlp Python API.

Matching (`find_match`):
  1. Search YouTube Music "songs" for "<artists> - <title>"; only if nothing there is acceptable,
     search regular YouTube (`ytsearch`).
  2. A candidate is acceptable when its duration is within ±tolerance of the Spotify duration
     (hard gate), most of the base-title words (title minus "(feat. ...)" and the version suffix)
     appear in its title, and it isn't an unwanted variant ("live", "sped up", "slowed", "nightcore",
     "8D", "reverb", "cover", "karaoke", "instrumental", "acoustic", "[Mixed]" DJ-mix cuts, ...)
     unless that word is also in the Spotify title/album.
  3. Among acceptable candidates, rank by: fewest version mismatches (version words of the Spotify
     title such as remixer/"extended" missing, or extra version words like "remix"/"extended"/"edit"
     present), then smallest duration delta (+5 s handicap if no Spotify artist appears in the
     title/channel), then ytmusic before youtube.
  4. The other acceptable candidates (same gates, distinct videos, ytmusic first, max 4) are returned
     as `MatchResult.alternatives` for the caller to fall back on when a download fails. Regular YouTube
     is also searched for them when YT Music offered no alternative.

yt-dlp's YouTube Music search extractor only yields id + title in flat mode (no duration), so the
songs search is done through that extractor's own innertube helpers and the raw rows are parsed
("artists • album • m:ss"). If those private helpers change, it falls back to the public flat
search plus full metadata extraction of the top few results (slower).

`find_match` results are memoised per (track id, tolerance) for MATCH_TTL_S (thread-safe; concurrent callers
for the same key wait for one search), so preview clips and downloads share a single search. A no-match
caused partly by a failed search is not memoised.

`download` retries transient failures (HTTP 403/429/5xx, timeouts, connection resets, fragment errors)
up to 3 attempts in total, each with a fresh YoutubeDL and a 2-5 s random pause; `YouTubeError.transient`
tells callers whether a failure was of that kind. `fetch_clip` grabs a short audio section for auditioning.

ffmpeg and deno are located through `paths` (app bundle, Homebrew or imageio-ffmpeg). When there is no
ffprobe next to ffmpeg (the bundled build), yt-dlp detects the audio codec with `ffmpeg -i` instead.
"""

from __future__ import annotations

import contextvars
import http.client
import os
import random
import re
import shutil
import socket
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, download_range_func, traverse_obj

try:
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
except ImportError:  # a future yt-dlp moved it: _ydl then just skips the workaround
    FFmpegPostProcessor = None

from . import paths
from .models import MatchResult, Track, YTCandidate

YTMUSIC_SONGS_PARAMS = "EgWKAQIIAWoKEAoQAxAEEAkQBQ=="  # yt-dlp's YoutubeMusicSearchURLIE._SECTIONS["songs"]
YOUTUBE_RESULTS = 10
YTMUSIC_FALLBACK_RESULTS = 5
ARTIST_MISSING_PENALTY_S = 5.0
TITLE_COVERAGE = 0.6
MAX_ALTERNATIVES = 4
MIN_ALTERNATIVES = 1  # regular YouTube is also searched when YT Music gave fewer alternatives than this
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_JITTER_S = (2.0, 5.0)
CLIP_ATTEMPTS = 2  # an audition should not hang, so one quick retry only
CLIP_JITTER_S = (0.5, 1.5)
CLIP_EXT = ".m4a"
MATCH_TTL_S = 3600.0
MATCH_CACHE_MAX = 4096

_SOURCE_RANK = {"ytmusic": 0, "youtube": 1}
_UNWANTED = (
    "live", "sped up", "speed up", "slowed", "nightcore", "8d", "reverb", "karaoke", "instrumental",
    "acapella", "a cappella", "cover", "acoustic", "bass boosted", "mixed",
)
_VERSION_WORDS = {"remix", "extended", "edit", "rework", "vip", "bootleg", "flip", "mashup", "dub", "club", "radio"}
_VERSION_STOPWORDS = {"mix", "original", "version", "feat", "ft", "featuring", "with", "and", "the", "x", "vs"}
_FEAT_RE = re.compile(r"[(\[]\s*(?:feat|ft|featuring|with)\b[^)\]]*[)\]]", re.IGNORECASE)
_VERSION_SPLIT_RE = re.compile(r"\s[-–—]\s|[(\[]")
_CLOCK_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


class YouTubeError(RuntimeError):
    """Search or download failure. `transient` is True when retrying later may well succeed."""

    def __init__(self, message: str = "", transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class _QuietLogger:
    """yt-dlp logger that swallows everything but remembers errors for exception messages."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        self.errors.append(msg)


@dataclass
class _Hit:
    cand: YTCandidate
    album: str = ""


def _base_opts(logger: _QuietLogger | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": logger or _QuietLogger(),
    }
    # Explicit paths: GUI apps start with a bare PATH, and the bundled ffmpeg has a non-standard name.
    if ffmpeg := paths.ffmpeg_path():
        opts["ffmpeg_location"] = ffmpeg
    if deno := paths.deno_path():
        opts["js_runtimes"] = {"deno": {"path": deno}}
    return opts


@contextmanager
def _ydl(opts: dict[str, Any]) -> Iterator[yt_dlp.YoutubeDL]:
    """`yt_dlp.YoutubeDL(opts)` whose ffmpeg is also seen by yt-dlp's option-less checks.

    FFmpegFD.available() (used to pick the downloader for `download_ranges` sections and HLS) ignores the
    ffmpeg_location option and reads a ContextVar that the yt-dlp CLI sets. Without this, clips fail with
    "ffmpeg is not installed" whenever ffmpeg isn't on PATH (the app). Set per thread, for this session only.
    """
    var = getattr(FFmpegPostProcessor, "_ffmpeg_location", None)
    location = opts.get("ffmpeg_location")
    token = var.set(location) if location and isinstance(var, contextvars.ContextVar) else None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        if token is not None:
            var.reset(token)


# ---------------------------------------------------------------- text helpers

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"['’‘`´]", "", s).replace("&", " and ")
    return " ".join(re.findall(r"\w+", s))


def _tokens(s: str) -> set[str]:
    return set(_norm(s).split())


def _has_phrase(phrase: str, text_norm: str) -> bool:
    return f" {phrase} " in f" {text_norm} "


def _parse_clock(s: str) -> float | None:
    m = _CLOCK_RE.match(s.strip())
    if not m:
        return None
    h, mi, se = (int(g) if g else 0 for g in m.groups())
    return float(h * 3600 + mi * 60 + se)


def _float(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def _fmt_clock(seconds: float | None) -> str:
    if seconds is None:
        return "?:??"
    m, s = divmod(round(seconds), 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------- searching

def _ytmusic_innertube(query: str) -> list[_Hit]:
    """YT Music songs search, parsing the raw rows (title / 'artists • album • m:ss')."""
    with _ydl(_base_opts()) as ydl:
        ie = ydl.get_info_extractor("YoutubeMusicSearchURL")
        ie.initialize()
        ytcfg = ie._download_ytcfg("web_music", query)
        resp = ie._extract_response(
            item_id=f'query "{query}"', query={"query": query, "params": YTMUSIC_SONGS_PARAMS}, ep="search",
            default_client="web_music", ytcfg=ytcfg, check_get_keys=("contents",),
            headers=ie.generate_api_headers(ytcfg=ytcfg, default_client="web_music"))
    rows = traverse_obj(resp, (
        "contents", "tabbedSearchResultsRenderer", "tabs", ..., "tabRenderer", "content", "sectionListRenderer",
        "contents", ..., "musicShelfRenderer", "contents", ..., "musicResponsiveListItemRenderer"))
    hits = []
    for row in rows or []:
        video_id = traverse_obj(row, ("playlistItemData", "videoId"))
        cols = ["".join(traverse_obj(col, ("musicResponsiveListItemFlexColumnRenderer", "text", "runs", ..., "text")))
                for col in row.get("flexColumns") or []]
        if not video_id or len(cols) < 2:
            continue
        meta = cols[1].split(" • ")
        if meta[0] in ("Song", "Video") and len(meta) > 1:
            meta = meta[1:]
        hits.append(_Hit(YTCandidate(
            url=f"https://music.youtube.com/watch?v={video_id}", title=cols[0], channel=meta[0],
            duration_s=_parse_clock(meta[-1]), source="ytmusic"), album=meta[1] if len(meta) > 2 else ""))
    return hits


def _ytmusic_public(query: str) -> list[_Hit]:
    """Fallback: public flat songs search (ids only) + full metadata extraction of the top results."""
    url = f"https://music.youtube.com/search?q={urllib.parse.quote_plus(query)}#songs"
    hits = []
    with _ydl({**_base_opts(), "extract_flat": "in_playlist",
                           "playlistend": YTMUSIC_FALLBACK_RESULTS}) as ydl:
        info = ydl.extract_info(url, download=False)
        for entry in (info or {}).get("entries") or []:
            try:
                full = ydl.extract_info(entry["url"], download=False, process=False)
            except DownloadError:
                continue
            hits.append(_Hit(YTCandidate(
                url=f"https://music.youtube.com/watch?v={full['id']}", title=full.get("track") or full.get("title") or "",
                channel=", ".join(full.get("artists") or []) or full.get("channel") or "",
                duration_s=_float(full.get("duration")), source="ytmusic"), album=full.get("album") or ""))
    return hits


def _search_ytmusic(query: str) -> list[_Hit]:
    try:
        hits = _ytmusic_innertube(query)
    except Exception:  # yt-dlp internals changed or request failed: use the public path
        hits = []
    return hits or _ytmusic_public(query)


def _search_youtube(query: str) -> list[_Hit]:
    with _ydl({**_base_opts(), "extract_flat": "in_playlist"}) as ydl:
        info = ydl.extract_info(f"ytsearch{YOUTUBE_RESULTS}:{query}", download=False)
    hits = []
    for e in (info or {}).get("entries") or []:
        if not e.get("id") or e.get("live_status") in ("is_live", "is_upcoming"):
            continue
        hits.append(_Hit(YTCandidate(
            url=f"https://www.youtube.com/watch?v={e['id']}", title=e.get("title") or "",
            channel=e.get("channel") or e.get("uploader") or "", duration_s=_float(e.get("duration")),
            source="youtube")))
    return hits


# ---------------------------------------------------------------- scoring

class _Scorer:
    def __init__(self, track: Track, tolerance_s: float) -> None:
        self.track = track
        self.tolerance_s = tolerance_s
        self.spotify_norm = _norm(f"{track.name} {track.album}")
        self.spotify_tokens = _tokens(track.name)
        name = _FEAT_RE.sub(" ", track.name)
        base = _VERSION_SPLIT_RE.split(name, maxsplit=1)[0]
        self.base_tokens = _tokens(base) or _tokens(name)
        self.version_tokens = _tokens(name) - self.base_tokens - _VERSION_STOPWORDS
        self.artists = [t for t in (_tokens(a) for a in track.artists) if t]

    def delta(self, hit: _Hit) -> float:
        return abs(hit.cand.duration_s - self.track.duration_s)

    def relevant(self, hit: _Hit) -> bool:
        """Title gate + unwanted-variant filter."""
        title_tokens = _tokens(hit.cand.title)
        if len(self.base_tokens & title_tokens) < TITLE_COVERAGE * len(self.base_tokens):
            return False
        variant_text = _norm(f"{hit.cand.title} {hit.album}")
        return not any(_has_phrase(p, variant_text) and not _has_phrase(p, self.spotify_norm) for p in _UNWANTED)

    def rank(self, hit: _Hit) -> tuple[int, float, int]:
        title_tokens = _tokens(hit.cand.title)
        all_tokens = title_tokens | _tokens(f"{hit.cand.channel} {hit.album}")
        mismatches = len(self.version_tokens - all_tokens) + len((_VERSION_WORDS & title_tokens) - self.spotify_tokens)
        artist_found = any(a <= all_tokens for a in self.artists)
        delta = self.delta(hit) + (0.0 if artist_found or not self.artists else ARTIST_MISSING_PENALTY_S)
        return mismatches, delta, _SOURCE_RANK.get(hit.cand.source, 9)

    def acceptable(self, hits: list[_Hit]) -> list[_Hit]:
        """Hits passing the duration, title and variant gates, best first."""
        ok = [h for h in hits if h.cand.duration_s and self.delta(h) <= self.tolerance_s and self.relevant(h)]
        return sorted(ok, key=self.rank)


def _video_id(url: str) -> str:
    """YouTube video id of a watch URL (so the ytmusic and youtube copies of one video compare equal)."""
    return (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("v") or [url])[0]


_match_lock = threading.Lock()
_match_cache: dict[tuple[str, float], tuple[float, MatchResult]] = {}  # key -> (monotonic time, result)
_match_inflight: dict[tuple[str, float], threading.Event] = {}


def find_match(track: Track, tolerance_s: float = 15.0) -> MatchResult:
    """YT Music first, regular YouTube as fallback; see module docstring for the matching rules.

    Memoised per (track.id, tolerance_s) for MATCH_TTL_S; a caller arriving while the same search is running
    waits for it. Raises YouTubeError (not memoised) when both searches fail.
    """
    key = (track.id, float(tolerance_s))
    while True:
        with _match_lock:
            hit = _match_cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < MATCH_TTL_S:
                return _for_track(hit[1], track)
            running = _match_inflight.get(key)
            if running is None:
                running = _match_inflight[key] = threading.Event()
                break
        running.wait()  # then re-check: cached, or the search failed and this caller tries itself
    try:
        result, cacheable = _search_match(track, tolerance_s)
        if cacheable:
            with _match_lock:
                _prune_matches()
                _match_cache[key] = (time.monotonic(), result)
        return _for_track(result, track)
    finally:
        with _match_lock:
            _match_inflight.pop(key, None)
        running.set()


def _prune_matches() -> None:
    """Drop expired entries (and the oldest ones beyond MATCH_CACHE_MAX). Caller holds _match_lock."""
    now = time.monotonic()
    for key in [k for k, (at, _) in _match_cache.items() if now - at >= MATCH_TTL_S]:
        del _match_cache[key]
    while len(_match_cache) >= MATCH_CACHE_MAX:
        del _match_cache[min(_match_cache, key=lambda k: _match_cache[k][0])]


def _for_track(result: MatchResult, track: Track) -> MatchResult:
    """A caller's own copy of a memoised result (its Track object, its own alternatives list)."""
    return replace(result, track=track, alternatives=list(result.alternatives))


def _search_match(track: Track, tolerance_s: float) -> tuple[MatchResult, bool]:
    """The uncached search: (result, whether it may be memoised)."""
    query = f"{track.artist_str} - {track.name}"
    scorer = _Scorer(track, tolerance_s)
    seen: list[_Hit] = []
    ranked: list[_Hit] = []  # acceptable hits: ytmusic best first, then youtube best first
    errors: list[str] = []
    transient = False
    for source, search in (("ytmusic", _search_ytmusic), ("youtube", _search_youtube)):
        if ranked and len({_video_id(h.cand.url) for h in ranked}) > MIN_ALTERNATIVES:
            break
        try:
            hits = [h for h in search(query) if h.cand.duration_s]
        except Exception as e:
            msg = _clean_error(str(e))
            errors.append(f"{source} search failed: {msg}")
            transient = transient or _is_transient(e, msg)
            continue
        seen.extend(hits)
        ranked.extend(scorer.acceptable(hits))
    if ranked:
        best, alternatives = ranked[0], []
        used = {_video_id(best.cand.url)}
        for h in ranked[1:]:
            if _video_id(h.cand.url) not in used and len(alternatives) < MAX_ALTERNATIVES:
                used.add(_video_id(h.cand.url))
                alternatives.append(h.cand)
        found = MatchResult(track=track, match=best.cand, closest=best.cand, reason=None, alternatives=alternatives)
        return found, True
    if len(errors) == 2:
        raise YouTubeError("; ".join(errors), transient=transient)
    if not seen:
        reason = "; ".join(["no YouTube results", *errors])
        return MatchResult(track=track, match=None, closest=None, reason=reason), not errors

    relevant = [h for h in seen if scorer.relevant(h)]
    pool = relevant or seen
    closest = min(pool, key=lambda h: (scorer.delta(h), _SOURCE_RANK.get(h.cand.source, 9)))
    where = f"closest {_fmt_clock(closest.cand.duration_s)} on {closest.cand.source}"
    if relevant:
        reason = f"no result within ±{tolerance_s:g}s ({where})"
    else:
        reason = f"no result matching the title ({where}: {closest.cand.title!r})"
    reason = "; ".join([reason, *errors])
    return MatchResult(track=track, match=None, closest=closest.cand, reason=reason), not errors


# ---------------------------------------------------------------- errors + retries

_PERMANENT_RE = re.compile(
    r"(?<!service )unavailable|not available|private|has been removed|was removed|removed by|no longer|"
    r"does not exist|copyright|blocked it|in your country|age[- ]restrict|confirm your age|sign in|log in|"
    r"members[- ]only|join this channel|premieres? in|live event will begin|account .*terminated|violat",
    re.IGNORECASE)
_HTTP_STATUS_RE = re.compile(r"\bHTTP(?: Error)? (\d{3})\b|\bstatus(?: code)?:? (\d{3})\b", re.IGNORECASE)
_TRANSIENT_RE = re.compile(
    r"timed? ?out|timeout|connection (?:reset|aborted|refused|error|closed)|reset by peer|remote end closed|"
    r"server disconnected|incomplete ?read|broken pipe|eof occurred|fragment|unable to download|"
    r"temporary failure|name resolution|network is unreachable|too many requests|bad gateway|"
    r"service unavailable|gateway time-?out|read error|try again|rate.?limit",
    re.IGNORECASE)
_T = TypeVar("_T")


def _exc_chain(exc: BaseException | None) -> list[BaseException]:
    """`exc` plus its causes, including the exception yt-dlp wrapped inside a DownloadError."""
    chain: list[BaseException] = []
    while exc is not None and exc not in chain and len(chain) < 8:
        chain.append(exc)
        inner = exc.exc_info[1] if isinstance(exc, DownloadError) and exc.exc_info else None
        exc = inner or exc.__cause__ or exc.__context__
    return chain


def _status_transient(status: int) -> bool:
    return status in (403, 429) or 500 <= status < 600


def _is_transient(exc: BaseException | None, msg: str = "") -> bool:
    """True for failures worth retrying (403/429/5xx, timeouts, resets, fragment errors); unknown means no.

    Permanent wording (unavailable, private, removed, copyright, age/sign-in gates, ...) always wins.
    """
    chain = _exc_chain(exc)
    text = " ".join([msg, *map(str, chain)])
    if _PERMANENT_RE.search(text):
        return False
    for e in chain:
        status = e.status if isinstance(e, HTTPError) else e.code if isinstance(e, urllib.error.HTTPError) else None
        if status is not None:
            return _status_transient(status)
    if m := _HTTP_STATUS_RE.search(text):
        return _status_transient(int(m.group(1) or m.group(2)))
    if any(isinstance(e, (TransportError, TimeoutError, ConnectionError, urllib.error.URLError, socket.gaierror,
                          http.client.HTTPException)) for e in chain):
        return True
    return bool(_TRANSIENT_RE.search(text))


def _failure(what: str, url: str, e: Exception, logger: _QuietLogger) -> YouTubeError:
    msg = _clean_error(logger.errors[-1] if logger.errors else str(e))
    return YouTubeError(f"{what} failed for {url}: {msg}", transient=_is_transient(e, f"{msg} {e}"))


def _with_retries(attempt: Callable[[], _T], attempts: int, jitter_s: tuple[float, float]) -> _T:
    """Run `attempt` until it succeeds, retrying transient YouTubeErrors after a random pause."""
    for n in range(1, attempts + 1):
        try:
            return attempt()
        except YouTubeError as e:
            if e.transient and n < attempts:
                time.sleep(random.uniform(*jitter_s))
            elif n > 1:
                raise YouTubeError(f"{e} (after {n} attempts)", transient=e.transient) from e
            else:
                raise
    raise AssertionError("unreachable")


# ---------------------------------------------------------------- download

def _clean_error(msg: str) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg).strip()
    return re.sub(r"^ERROR:\s*", "", msg)


def download(candidate: YTCandidate, dest: Path) -> Path:
    """Download best audio of `candidate` as MP3 192 kbps to exactly `dest` (replacing it); returns `dest`.

    Works in a private temp dir (yt-dlp expands %, $VARS and ~ in output paths, and partial files
    never land next to dest), then moves the finished MP3 into place. No tags/thumbnails are added.
    Transient failures (see `_is_transient`) are retried, up to 3 attempts in total, each with a fresh
    YoutubeDL after a 2-5 s pause. Raises YouTubeError on failure (`.transient` says what kind).
    """
    dest = Path(dest)
    if dest.suffix.lower() != ".mp3":
        raise ValueError(f"dest must end in .mp3: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    return _with_retries(lambda: _download_once(candidate, dest), DOWNLOAD_ATTEMPTS, DOWNLOAD_JITTER_S)


def _download_once(candidate: YTCandidate, dest: Path) -> Path:
    logger = _QuietLogger()
    with tempfile.TemporaryDirectory(prefix="musicreco-yt-") as tmp:
        opts = {
            **_base_opts(logger),
            "format": "bestaudio/best",
            "outtmpl": {"default": os.path.join(tmp.replace("%", "%%"), "audio.%(ext)s")},
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            "noplaylist": True,
            "updatetime": False,  # mtime = download time, not the YouTube upload date
        }
        try:
            with _ydl(opts) as ydl:
                info = ydl.extract_info(candidate.url, download=True)
        except Exception as e:
            raise _failure("download", candidate.url, e, logger) from e
        produced = Path(traverse_obj(info, ("requested_downloads", 0, "filepath")) or os.path.join(tmp, "audio.mp3"))
        if produced.suffix != ".mp3" or not produced.is_file() or produced.stat().st_size == 0:
            raise YouTubeError(f"download failed for {candidate.url}: no MP3 produced (got {produced.name})")
        shutil.move(produced, dest)
    return dest


def fetch_clip(candidate: YTCandidate, start_s: float, length_s: float, dest: Path) -> Path:
    """Download only `length_s` seconds of audio from `start_s` into an m4a file that afplay can play.

    The container is always m4a: `dest`'s suffix is replaced by ".m4a" and the returned path is the file
    actually written (an existing file there is replaced). Only the requested section is fetched (yt-dlp
    `download_ranges`, ffmpeg stream copy, no re-encode when YouTube serves AAC; anything else is converted).
    If the section would run past the end of the track (known duration) it is moved back to end there.
    One quick retry on transient failures. Quiet and thread-safe (own YoutubeDL and temp dir per call).
    Raises YouTubeError on failure.
    """
    if length_s <= 0 or start_s < 0:
        raise ValueError(f"need start_s >= 0 and length_s > 0, got {start_s!r}, {length_s!r}")
    if candidate.duration_s and start_s + length_s > candidate.duration_s:
        start_s = max(0.0, candidate.duration_s - length_s)
    final = Path(dest).with_suffix(CLIP_EXT)
    final.parent.mkdir(parents=True, exist_ok=True)
    return _with_retries(lambda: _fetch_clip_once(candidate, start_s, length_s, final), CLIP_ATTEMPTS, CLIP_JITTER_S)


def _fetch_clip_once(candidate: YTCandidate, start_s: float, length_s: float, final: Path) -> Path:
    logger = _QuietLogger()
    # temp dir next to the target: the finished clip is renamed into place (atomic, same filesystem)
    with tempfile.TemporaryDirectory(prefix=".musicreco-clip-", dir=final.parent) as tmp:
        opts = {
            **_base_opts(logger),
            "format": f"bestaudio[ext={CLIP_EXT[1:]}]/bestaudio/best",
            "outtmpl": {"default": os.path.join(tmp.replace("%", "%%"), "clip.%(ext)s")},
            "download_ranges": download_range_func(None, [(start_s, start_s + length_s)]),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": CLIP_EXT[1:],
                                "preferredquality": "128"}],
            "noplaylist": True,
            "updatetime": False,
        }
        try:
            with _ydl(opts) as ydl:
                info = ydl.extract_info(candidate.url, download=True)
        except Exception as e:
            raise _failure("clip download", candidate.url, e, logger) from e
        produced = Path(traverse_obj(info, ("requested_downloads", 0, "filepath")) or os.path.join(tmp, "clip.m4a"))
        if produced.suffix != CLIP_EXT or not produced.is_file() or produced.stat().st_size == 0:
            raise YouTubeError(f"clip download failed for {candidate.url}: no clip produced (got {produced.name})")
        os.replace(produced, final)
    return final
