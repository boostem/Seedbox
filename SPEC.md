# MusicReco — tunebat → yt-dlp DJ crate builder (spec + module contracts)

Personal archival / DJ-practice tool. Interactive terminal app:

1. Prompt for a search term → `GET https://api.tunebat.com/api/tracks/search?term=<q>&page=<n>` (50/page).
2. Arrow-key picker of results (type-to-filter), with "Next page" / "Previous page" / "New search" entries. Rows must
   disambiguate versions: title, artists, album, release date, duration, Camelot, BPM.
3. Picked track = **seed** → `GET https://api.tunebat.com/api/tracks?trackId=<id>` → use **only `data.r`** (ignore `data.er` entirely).
4. Checklist (opt-out): seed first, **ticked**. Every `r` track shown; **ticked iff it passes the rule**
   (Camelot classic-adjacent AND BPM ±10% incl. half/double-time), otherwise **unticked** (user may opt in). Show why.
5. Download ticked tracks, **2 in parallel**: YouTube Music search first, fallback regular YouTube; accept the candidate
   whose duration is within **±15 s** of the Spotify duration (closest wins, prefer ytmusic). No candidate within
   tolerance → **skip** and log to `skipped.txt`. Output **MP3 192 kbps** (re-encoded from YouTube's lossy stream).
6. Tag each MP3 (ID3v2.3) from tunebat data + genre, write `tracks.csv` + `skipped.txt`, then back to step 1.
   Empty search or Ctrl-C quits.

Captured sample responses: `search.txt`, `tracks.txt` (HTTP request headers on top, JSON body starts at `{"data"`).
Reference cloudscraper usage: `example_cloudscraper.py`.

## Environment
- venv: `.venv` (Homebrew Python 3.14), deps in `requirements.txt` (already installed). Run things with `.venv/bin/python`.
- `ffmpeg` and `deno` on PATH (`/opt/homebrew/bin`). yt-dlp is the pip package in the venv (used as a library).
- Scratch/test files go in the scratchpad dir given to you, never in the project folder.
- Do NOT put the user's email anywhere (User-Agents etc.).

## Output layout
```
downloads/
  <Artists - Title of seed>/          # sanitized seed.display_name
    <Artists - Title>.mp3              # sanitized track.display_name
    tracks.csv
    skipped.txt
```
- **Skip existing**: target file already in this seed folder (same Spotify ID) → status `exists`, no download.
- **Cross-seed duplicate**: track already downloaded under another seed folder → **copy** that file in (no YouTube hit),
  retag Grouping with the new seed → status `copied`.

## Tags (ID3v2.3, via mutagen) — strip any tags yt-dlp/YouTube put there first
| Frame | Value |
|---|---|
| TIT2 | track.name |
| TPE1 | track.artist_str |
| TALB | track.album |
| TYER (v2.3) | track.year |
| TBPM | str(round(track.bpm)) |
| TKEY | track.camelot (e.g. `1A`) |
| TIT1 (Grouping) | seed.display_name |
| TCON | genre (single most specific, Title Case) if found |
| APIC | front cover JPEG from track.cover_url (640px Spotify art) |
| TXXX:SPOTIFY_TRACK_ID | track.id (used for skip-existing / cross-seed copy detection) |

No comment tag, no M3U, no rekordbox XML.

## Shared types — `musicreco/models.py` (DONE, do not change without telling the lead)
`Track` (with `.from_api(dict)`, `.artist_str`, `.display_name`, `.duration_s`, `.duration_str`, `.year`, `.spotify_url`),
`SearchPage`, `TrackDetail`, `YTCandidate`, `MatchResult`, `DownloadResult`.

## Module contracts (each module owned by exactly one agent)

### `musicreco/tunebat.py`
```python
class TunebatError(Exception): ...
class TunebatClient:
    def __init__(self, scraper=None): ...               # cloudscraper.create_scraper(); headers Origin/Referer https://tunebat.com/
    def search(self, term: str, page: int = 1) -> SearchPage: ...
    def get_track(self, track_id: str) -> TrackDetail: ...   # recommended = data["r"] only
```
Retry 429/5xx with backoff (3 tries). Raise `TunebatError` with a readable message otherwise.

### `musicreco/mixing.py`
```python
def camelot_adjacent(seed: str, other: str) -> bool        # same key; ±1 same letter (12↔1 wraps); same number other letter
def bpm_compatible(seed_bpm: float, other_bpm: float, tol: float = 0.10) -> bool   # also other*2 and other/2
def passes_rule(seed: Track, other: Track) -> tuple[bool, str]   # (ok, short human reason e.g. "1B ✓ 128 ✓" / "5A ✗ key")
```

### `musicreco/youtube.py`
```python
def find_match(track: Track, tolerance_s: float = 15.0) -> MatchResult
def download(candidate: YTCandidate, dest: Path) -> Path   # dest = full target path ending in .mp3; returns final path
```
YouTube Music first (`https://music.youtube.com/search?q=...` songs), fallback `ytsearch` — query `"<artists> - <title>"`.
Thread-safe (new `YoutubeDL` per call), quiet (no console spam), MP3 192 via FFmpegExtractAudio.

### `musicreco/genres_beatport.py`
```python
def beatport_genre(track: Track) -> str | None   # sub-genre if present else genre; None if no confident match
```

### `musicreco/genres_musicbrainz.py`
```python
def musicbrainz_genre(track: Track) -> str | None  # recording genres, else artist genres; highest vote count
```
Global 1 req/s lock (thread-safe), User-Agent `MusicReco/0.1 (personal DJ library tool)`.

### `musicreco/genres.py`
```python
def find_genre(track: Track) -> tuple[str | None, str | None]   # (genre Title Case, source "beatport"|"musicbrainz"|None)
```
Beatport first, MusicBrainz fallback. In-memory cache by track.id. Never raises (log + return (None, None)).
Catch-all genres ("Dance / Pop", "Electronic", …) are only kept if no other source has something more specific.

### `musicreco/tagging.py`
```python
def write_tags(path: Path, track: Track, seed: Track, genre: str | None) -> None   # full tag set above; fetches cover
def set_grouping(path: Path, seed: Track) -> None
def read_spotify_id(path: Path) -> str | None
```

### `musicreco/library.py`
```python
DOWNLOADS_ROOT: Path   # <project>/downloads
def sanitize_filename(s: str) -> str
def seed_dir(seed: Track, root: Path = DOWNLOADS_ROOT) -> Path            # creates it
def track_path(seed_dir: Path, track: Track) -> Path                      # unique .mp3 path (suffix [<id6>] on name clash w/ other ID)
def find_existing(track: Track, root: Path = DOWNLOADS_ROOT) -> list[Path]   # all mp3s whose TXXX:SPOTIFY_TRACK_ID == track.id
def write_tracks_csv(seed_dir: Path, seed: Track, rows: list["CsvRow"]) -> Path
def append_skipped(seed_dir: Path, seed: Track, results: list[DownloadResult]) -> Path | None   # skipped + failed only
```
`CsvRow` dataclass (define in library.py): track, role ("seed"|"r"), passed_rule, rule_reason, ticked, result (DownloadResult|None).
CSV columns: role, ticked, passed_rule, rule_reason, spotify_id, artists, title, album, release_date, duration, key, camelot,
bpm, energy, danceability, popularity, genre, genre_source, status, youtube_url, file, reason.
`skipped.txt`: append a timestamped block per run; per entry: artists, title, Camelot, BPM, Spotify length, Spotify link,
status/reason, closest YouTube candidate (title, length, URL) if any.

### `musicreco/pipeline.py`
```python
def process_track(track: Track, seed: Track, seed_dir: Path) -> DownloadResult
def run_batch(tracks: list[Track], seed: Track, seed_dir: Path, workers: int = 2,
              on_update: Callable[[Track, str], None] | None = None) -> list[DownloadResult]
```
Order: exists in seed_dir → `exists`; exists elsewhere → copy + `set_grouping` → `copied`; else `find_match` → none →
`skipped`; `download` → `find_genre` → `write_tags` → `downloaded`. Any exception → `failed` with reason (never crash the batch).

### `tunebat_dl.py` (project root, entrypoint) — questionary + rich UI loop described at top. Run: `.venv/bin/python tunebat_dl.py`.

---

# v2 — features + fixes (user-approved 2026-10-02)

Approved: (1) reliability bug fixes, (2) ranked + colour-coded checklist, (3) audition 20 s clips from the checklist,
(4) order the crate into a set, (5) energy 1–10 as **POPM star rating** + ReplayGain loudness tags, (6) Beatport extra tags.
Rejected (do not build): tunebat advanced search, using ~/Music/DJ, disk cache, tooling/packaging, SoundCloud,
download verification, comment tag, rekordbox/M3U, `er`, Spotify API. **Key/BPM tags always come from tunebat** —
Beatport key/BPM go to tracks.csv only (with mismatch flags). The pre-tick gate stays `mixing.passes_rule` exactly.

New shared types are in `musicreco/models.py` (lead-owned): `MatchResult.alternatives`, `ProviderUnavailable`,
`BeatportInfo`, `Score`, `Loudness`, and `DownloadResult.beatport/loudness/energy_rating/order`.

Never read outside the project folder and your scratch dir. Never modify `downloads/` (copy files out to scratch to test).
No audible playback in tests (`afplay -v 0` or mock). Run with `.venv/bin/python`.

## v2 contracts (one owner per file)

### `youtube.py` + `tunebat.py` (reliability)
- `find_match` fills `MatchResult.alternatives` (other in-tolerance candidates, best first, max 4).
- `download(candidate, dest)`: retry transient failures (HTTP 403/429/5xx, timeouts, "fragment"/"unable to download")
  up to 3 attempts with a fresh `YoutubeDL` and 2–5 s jitter. `YouTubeError` gets attribute `transient: bool`.
- New `fetch_clip(candidate: YTCandidate, start_s: float, length_s: float, dest: Path) -> Path` — short audio clip
  (yt-dlp `download_ranges`), any audio container afplay can play (m4a/mp3). Used by audition.
- `tunebat.py`: a malformed item in search/r is skipped + logged, not fatal for the page.

### `genres_beatport.py` + `genres.py` + `genres_musicbrainz.py`
- `beatport_lookup(track) -> BeatportInfo | None` (keep `beatport_genre` as a thin wrapper). Fill every field available
  from the hit (label, catalog_number, isrc, mix_name, remixers, bpm, key + camelot conversion, url, track_id, publish_date).
- Transient failures (timeouts, 429/5xx, Cloudflare 403/pause) raise `ProviderUnavailable` instead of returning None
  — in both providers.
- `genres.find_metadata(track) -> tuple[str | None, str | None, BeatportInfo | None]` (genre, source, beatport).
  `find_genre` stays (returns first two). Never raises. Never caches a result affected by `ProviderUnavailable`.
  Genre choice logic unchanged (weak catch-alls fall through).

### `mixing.py`
- `score(seed, other) -> Score` — 0–100; Camelot move label; BPM delta as pitch % (half/double aware); energy &
  danceability deltas; `passed == passes_rule(seed, other)[0]`. `passes_rule` unchanged.
- `energy_ratings(tracks: list[Track]) -> dict[str, int]` — 1–10 per track id, crate-relative (spread e/da/lo/h).
- `order_set(seed: Track, tracks: list[Track]) -> list[Track]` — playable running order, seed first; minimise Camelot
  distance + BPM drift, gentle energy build to a peak ~2/3 through then ease off.

### `analysis.py` (new) + `tagging.py`
- `analysis.measure_loudness(path) -> Loudness | None` — one ffmpeg `ebur128=peak=true` pass; RG2 gain = -18 - LUFS.
- `tagging.write_tags(path, track, seed, genre, beatport: BeatportInfo | None = None, loudness: Loudness | None = None)`
  adds: TPUB label, TSRC isrc, TIT3 mix name, TPE4 remixers, WOAF beatport url, TXXX:BEATPORT_TRACK_ID,
  TXXX:CATALOGNUMBER, TXXX:REPLAYGAIN_TRACK_GAIN ("-3.21 dB") / REPLAYGAIN_TRACK_PEAK ("0.987654").
  TKEY/TBPM unchanged (tunebat). Existing callers with 4 args must keep working.
- `tagging.set_energy(path, rating: int)` — 1–10 → 1–5 stars as POPM (research which POPM email/byte mapping
  rekordbox, Serato, Traktor, Apple Music/Finder actually read; write the frame(s) that maximise compatibility).
- `tagging.set_track_number(path, n: int, total: int)` — TRCK "n/total".
- `tagging.read_tags_summary(path) -> dict` — genre, energy stars, track number etc. (for exists/copied rows).

### `audition.py` (new)
- `play(track: Track) -> str` — non-blocking: find match (cached per track id), fetch a 20 s clip from ~35% into the
  track (cached on disk in a temp dir for the session), start `afplay` in the background, stop any previous clip.
  Returns a short status string ("▶ Artist - Title (0:20)" or why it couldn't). Must be safe to call from a
  prompt_toolkit key-binding handler (do the slow work on a background thread; never print).
- `stop() -> None`, `cleanup() -> None` (kill player, delete temp clips; registered with atexit).

### `pipeline.py` + `library.py`
- Atomic per-track work: download + genre/beatport + loudness + tagging happen in `seed_dir/.tmp-<id>/`, then
  `os.replace` to the final path. Sweep stale `.tmp-*` dirs on batch start. No untagged files ever at the final name.
- On `YouTubeError` for the match, try `alternatives` in order before giving up.
- `finalize_crate(seed, seed_dir, results) -> list[DownloadResult]` (new, called by the UI after run_batch): for all
  files in the crate (downloaded/copied/exists) compute `mixing.energy_ratings` → `tagging.set_energy`, and
  `mixing.order_set` → `tagging.set_track_number`; fill `energy_rating`/`order` on results.
- exists/copied rows: fill genre/energy etc. from `tagging.read_tags_summary` so the CSV isn't blank.
- `library.write_tracks_csv`: atomic (temp + os.replace) and **merge** with the existing CSV by spotify_id (a re-run
  must not blank earlier downloaded rows). New columns: score, move, energy_rating, order, lufs, replaygain_db,
  beatport_url, label, catalog_number, isrc, mix_name, beatport_key, beatport_camelot, beatport_bpm, key_mismatch,
  bpm_mismatch. `CsvRow` gains `score: Score | None = None`.

### `tunebat_dl.py` (UI)
- Checklist and recommendations table sorted by `mixing.score` (seed first), colour-coded by move
  (same=green, ±1=cyan, relative=yellow, boosts=magenta, fail=red/dim), showing score + chips. Pre-tick = `passed`.
- Audition hotkey in the checklist (e.g. `p` plays the highlighted row via `audition.play`, `s` stops), with the
  status shown in the prompt's toolbar/instruction; keys listed in the instruction line.
- After run_batch: `pipeline.finalize_crate`, then print the set order (n, Camelot, BPM, energy ★, title) with a small
  energy/BPM curve; CSV rows include score. `audition.cleanup()` on exit.
