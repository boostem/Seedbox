"""Shared data types. Every module imports from here; do not redefine these elsewhere."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Track:
    """One tunebat track. Field names map from tunebat's compact JSON keys."""

    id: str  # "id"  Spotify track ID
    name: str  # "n"
    artists: list[str]  # "as"
    album: str  # "an"
    release_date: str  # "rd"  "YYYY-MM-DD" or "YYYY"
    duration_ms: int  # "d"
    popularity: int  # "p"
    key: str  # "k"   e.g. "G♯ minor"
    camelot: str  # "c"   e.g. "1A"
    bpm: float  # "b"
    energy: float  # "e"
    danceability: float  # "da"
    cover_url: str | None  # largest image in "ci"
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> Track:
        images = sorted(d.get("ci") or [], key=lambda i: i.get("iw") or 0, reverse=True)
        return cls(
            id=d["id"],
            name=d.get("n") or "",
            artists=list(d.get("as") or []),
            album=d.get("an") or "",
            release_date=d.get("rd") or "",
            duration_ms=int(d.get("d") or 0),
            popularity=int(d.get("p") or 0),
            key=d.get("k") or "",
            camelot=d.get("c") or "",
            bpm=float(d.get("b") or 0),
            energy=float(d.get("e") or 0),
            danceability=float(d.get("da") or 0),
            cover_url=images[0]["iu"] if images else None,
            raw=d,
        )

    @property
    def artist_str(self) -> str:
        return ", ".join(self.artists)

    @property
    def display_name(self) -> str:
        """'Artists - Title' — used for filenames, seed folder names and the Grouping tag."""
        return f"{self.artist_str} - {self.name}"

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000

    @property
    def duration_str(self) -> str:
        m, s = divmod(round(self.duration_s), 60)
        return f"{m}:{s:02d}"

    @property
    def year(self) -> str:
        return self.release_date[:4]

    @property
    def spotify_url(self) -> str:
        return f"https://open.spotify.com/track/{self.id}"

    def to_raw(self) -> dict:
        """tunebat-style dict that `from_api` turns back into this Track (fields win over a stale `raw`).

        The seed's nested "r"/"er" recommendation lists are left out.
        """
        d = {k: v for k, v in (self.raw or {}).items() if k not in ("r", "er")}
        d.update({
            "id": self.id, "n": self.name, "as": list(self.artists), "an": self.album, "rd": self.release_date,
            "d": self.duration_ms, "p": self.popularity, "k": self.key, "c": self.camelot, "b": self.bpm,
            "e": self.energy, "da": self.danceability,
        })  # fmt: skip
        images = sorted(d.get("ci") or [], key=lambda i: i.get("iw") or 0, reverse=True)
        if (images[0].get("iu") if images else None) != self.cover_url:
            d["ci"] = [{"iu": self.cover_url, "iw": 640, "ih": 640}] if self.cover_url else []
        return d

    def to_json(self) -> dict:
        """The API shape (frontend/src/api/types.ts Track)."""
        return {
            "id": self.id, "name": self.name, "artists": list(self.artists), "display_name": self.display_name,
            "album": self.album, "release_date": self.release_date, "duration_s": self.duration_s,
            "popularity": self.popularity, "key": self.key, "camelot": self.camelot, "bpm": self.bpm,
            "energy": self.energy, "danceability": self.danceability, "cover_url": self.cover_url,
            "spotify_url": self.spotify_url,
        }  # fmt: skip


@dataclass
class SearchPage:
    page: int
    total_count: int
    items: list[Track]

    @property
    def has_next(self) -> bool:
        # tunebat's totalCount drifts between pages, so a full page also means "maybe more"
        return len(self.items) >= 50 or self.page * 50 < self.total_count


@dataclass
class TrackDetail:
    seed: Track
    recommended: list[Track]  # tunebat "r" only — "er" is deliberately ignored


@dataclass
class YTCandidate:
    url: str
    title: str
    channel: str
    duration_s: float | None
    source: str  # "ytmusic" | "youtube"


@dataclass
class MatchResult:
    track: Track
    match: YTCandidate | None  # within tolerance, else None
    closest: YTCandidate | None  # best candidate seen even if out of tolerance (for skipped.txt)
    reason: str | None  # why match is None
    alternatives: list[YTCandidate] = field(default_factory=list)  # other in-tolerance candidates, best first


class ProviderUnavailable(Exception):
    """A metadata source is down, blocked or rate-limited: a transient miss that must not be cached."""


@dataclass
class BeatportInfo:
    """Everything a Beatport search hit carries that we keep. Key/BPM are informational only (tags use tunebat's)."""

    track_id: int | None = None
    url: str | None = None
    genre: str | None = None
    sub_genre: str | None = None
    label: str | None = None
    catalog_number: str | None = None
    isrc: str | None = None
    mix_name: str | None = None
    remixers: list[str] = field(default_factory=list)
    bpm: float | None = None
    key: str | None = None  # Beatport's key name, e.g. "Eb Minor"
    camelot: str | None = None  # converted, e.g. "2A"
    publish_date: str | None = None


@dataclass
class Score:
    """Compatibility of a track with the seed. `passed` is the user's pre-tick rule; `value` only ranks/colours."""

    value: int  # 0-100
    passed: bool  # == mixing.passes_rule(...)[0]
    move: str  # Camelot move: "same" | "+1" | "-1" | "relative" | "+2 boost" | "+7 boost" | "diagonal" | "clash" | "?"
    chips: list[str] = field(default_factory=list)  # short reasons, e.g. ["relative", "-1.5% pitch", "energy +0.05"]


@dataclass
class Loudness:
    lufs: float  # integrated loudness
    true_peak_dbtp: float
    replaygain_db: float  # ReplayGain 2.0 track gain (-18 LUFS reference)
    peak: float  # linear peak for REPLAYGAIN_TRACK_PEAK


@dataclass
class DownloadResult:
    track: Track
    status: str  # "downloaded" | "copied" | "exists" | "skipped" | "failed"
    path: Path | None = None
    youtube: YTCandidate | None = None
    closest: YTCandidate | None = None
    reason: str | None = None
    genre: str | None = None
    genre_source: str | None = None
    beatport: BeatportInfo | None = None
    loudness: Loudness | None = None
    energy_rating: int | None = None  # 1-10, crate-relative (written as POPM stars)
    order: int | None = None  # position in the suggested set order (1 = first)


@dataclass
class CompletedBatch:
    """What pipeline.complete_batch did after a download batch (paths are None when not written)."""

    results: list[DownloadResult]  # this run's results (energy_rating/order filled in)
    crate: list[DownloadResult]  # results + files from earlier runs: everything finalize_crate rated/numbered
    csv_path: Path | None = None
    playlist_path: Path | None = None  # <folder>.m3u8
    xml_path: Path | None = None  # <folder>.xml (rekordbox XML)
    skipped_path: Path | None = None
    crate_json_path: Path | None = None
    errors: list[str] = field(default_factory=list)  # non-fatal problems ("tracks.csv: Permission denied")
