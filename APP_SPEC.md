# Seedbox — desktop app spec (v3, user-approved 2026-10-03)

Seedbox turns the existing `musicreco` engine (v1+v2, see "Engine" at the end: tunebat seed → "r" recs → YouTube Music → MP3 192
→ ID3 tags → per-seed crate folders) into a modern **macOS desktop app** shared as a `.dmg` with friends.

## Decisions (do not change without the lead)
- **Shell:** Python backend (FastAPI + uvicorn on 127.0.0.1) + React web UI rendered in a native window via
  **pywebview**. Packaged with **PyInstaller** into `Seedbox.app` + `Seedbox.dmg`, Apple Silicon, unsigned
  (friends right-click → Open once). `python -m seedbox` runs it from source.
- **Look:** dark DJ-software style (rekordbox/AutoPilot-like): dense, high contrast, Camelot colours, 3-band
  waveforms. Name: **Seedbox**.
- **Crates folder:** `~/Music/MusicReco` (configurable in Settings).
- **Previews:** when a crate opens, 20 s clips for the seed + every rec are **preloaded in the background**
  (3 at a time, ticked/high-score first, disk cache shared across sessions). Clicking play is instant.
- **New features:** interactive **set builder** (drag reorder, live transition scores, energy/BPM chart, Camelot
  wheel; re-exports TRCK + `.xml` + `.m3u8` + CSV order), **waveforms** (3-band, full tracks + preview clips),
  **intro/outro + mix-point analysis** with suggestions of which parts mix well (built-in numpy/scipy, ~1 s/track,
  tunebat BPM locks the grid).
- **Unchanged rules:** only tunebat `r` (never `er`), pre-tick = `mixing.passes_rule`, YT Music first ±15 s,
  MP3 192, key/BPM tags from tunebat, Beatport extras, energy POPM stars, set order TRCK, playlists `.m3u8` + `.xml`.
- **Bundled binaries:** ffmpeg from `imageio-ffmpeg` (static, system libs only), `deno` copied from Homebrew (system
  libs only). **yt-dlp must be updatable at runtime** (a frozen copy breaks within weeks).

## Layout & ownership (one owner per file; never edit files you don't own — say so in your reply instead)
```
musicreco/                      engine
  settings.py, paths.py  (new)  CORE agent
  library.py, pipeline.py, youtube.py, analysis.py, mixing.py, models.py*   CORE agent  (*models: CORE may add, never break)
  previews.py            (new)  PREVIEWS agent
  structure.py           (new)  STRUCTURE agent
  transitions.py         (new)  TRANSITIONS agent
  tunebat.py, genres*.py, tagging.py   frozen (bug fixes only via the lead)
_archive/                       the retired terminal tool (tunebat_dl.py, audition.py, SPEC.md), not used by the app
seedbox/
  server.py, jobs.py, events.py, analysis_queue.py, __main__.py?   SERVER agent  (__main__ → PACKAGING)
  app.py, updater.py, __main__.py                                   PACKAGING agent
frontend/                       React 19 + TypeScript + Vite + Tailwind 4, built with **Deno** (no Node installed)
  src/api/types.ts              LEAD (the contract — read it; propose changes in your reply)
  scaffold, design system, api client, shell, Settings + Library pages   FRONTEND-FOUNDATION agent
  src/pages/Search*, Crate*, Downloads*, src/features/crate/**          FRONTEND-CRATE agent (wave 2)
  src/pages/SetBuilder*, src/features/set/**, src/features/waveform/**  FRONTEND-SET agent (wave 2)
packaging/                      PACKAGING agent (PyInstaller spec, build script, icon, dmg, friends README)
```

## Python module contracts

### CORE — `musicreco/settings.py`, `musicreco/paths.py`, refactors
```python
# settings.py
@dataclass class Settings: output_root: Path; workers: int = 2; tolerance_s: float = 15.0;
    bpm_tolerance: float = 0.10; preview_concurrency: int = 3; preview_cache_mb: int = 500
    mix_length: str = "normal"   # mix plans' preferred blend length: short / normal / long = 8 / 16 / 32 bars
    mix_view: str = "simple"     # transition editor: simple (style + amounts) / advanced (main lanes) / manual (every lane)
def app_support_dir() -> Path      # ~/Library/Application Support/Seedbox (created)
def cache_dir() -> Path            # ~/Library/Caches/Seedbox (created)
def load_settings() -> Settings    # JSON at app_support_dir()/settings.json; env MUSICRECO_OUTPUT_ROOT overrides root
def save_settings(s: Settings) -> None
def to_json(s) -> dict / from_json(d) -> Settings
# paths.py
def ffmpeg_path() -> str | None    # order: $SEEDBOX_BIN_DIR, sys._MEIPASS/bin, PATH, /opt/homebrew/bin, imageio_ffmpeg
def deno_path() -> str | None
def bundled() -> bool
```
- `library.downloads_root()` replaces the hard-coded `DOWNLOADS_ROOT` everywhere (keep `DOWNLOADS_ROOT` as a
  backwards-compatible alias resolved at call time if feasible).
- `youtube.find_match` memoised per (track.id, tolerance) for the process lifetime (thread-safe, 1 h TTL) so previews
  and downloads share one search. `youtube`/`analysis` resolve ffmpeg/deno via `paths`.
- `mixing.passes_rule(..., tol=0.10)` / `score(..., tol=0.10)` accept the BPM tolerance (default unchanged).
- `pipeline.process_track/run_batch` take `tolerance_s` (default 15.0).
- Crate metadata persistence: `library.write_crate_json(seed_dir, seed, recs)` → `seed_dir/.seedbox/crate.json`
  (raw tunebat dicts for seed + recs, so Tracks can be rebuilt exactly); `library.load_crate_tracks(seed_dir) ->
  dict[str, Track]` (crate.json, falling back to rebuilding from tracks.csv for old crates).
- `library.apply_order(seed_dir, ordered_ids: list[str]) -> None` — TRCK n/total on the files, rewrite `.m3u8` +
  `.xml` + tracks.csv `order` column. `library.list_crates(root) -> list[dict]` (CrateSummary-shaped).
- `pipeline.complete_batch(seed, recs, scores, ticked_ids, seed_dir, results) -> CompletedBatch` — everything that
  happens after run_batch (finalize incl. earlier-run files, CSV, skipped.txt, .m3u8, .xml, crate.json). The server uses it.

### PREVIEWS — `musicreco/previews.py`
```python
class PreviewManager:
    def __init__(self, cache_dir: Path, concurrency: int = 3, max_cache_mb: int = 500,
                 on_event: Callable[[dict], None] | None = None) -> None
    def preload(self, tracks: list[Track]) -> None       # queue in the given order (dedupe, skip ready/no_match)
    def prioritize(self, track_id: str) -> dict          # move to front / start now; returns PreviewStatus dict
    def status(self, track_id: str) -> dict              # PreviewStatus (types.ts), audio_url "/api/previews/<id>/audio"
    def audio_path(self, track_id: str) -> Path | None
    def clear_queue(self) -> None                        # opening another crate drops queued (not in-flight) work
    def shutdown(self) -> None
```
Clip: `youtube.find_match` → `youtube.fetch_clip(match, start≈35% of duration, 20 s)`, alternatives on failure.
Cache `cache_dir/previews/<id>.m4a` + `<id>.json` (status/peaks/youtube title); LRU eviction over max_cache_mb.
`peaks`: ~200 values 0-1 from the clip. `on_event` gets every status change (server forwards as SSE).

### STRUCTURE — `musicreco/structure.py` (numpy + scipy only)
```python
ANALYSIS_VERSION: int
def decode_mono(path: Path, sr: int = 22050) -> np.ndarray          # via paths.ffmpeg_path()
def analyze(path: Path, bpm_hint: float | None = None, track_id: str = "") -> dict   # TrackAnalysis (types.ts)
def load_or_analyze(seed_dir: Path, track_id: str, path: Path, bpm_hint: float | None) -> dict
     # cache seed_dir/.seedbox/analysis/<id>.json keyed by (size, mtime, ANALYSIS_VERSION)
def waveform_peaks(samples: np.ndarray, sr: int, points: int) -> dict   # {points_per_second, low, mid, high}
```
Beat grid from bpm_hint (tunebat; handle half/double), phase + downbeat detection, bars, 8/16/32-bar phrases,
sections, intro/outro edges (length, drums_only, has_vocals), cues (first downbeat, mix-in, drop(s), mix-out),
3-band waveform (~50 points/s, capped ~20k points). ~1 s per 3-min track. Never raises for bad audio → raise a
clear ValueError the caller can surface.

### TRANSITIONS — `musicreco/transitions.py`
```python
def suggest(a: Track, a_an: dict | None, b: Track, b_an: dict | None, bpm_tol: float = 0.10) -> dict  # Transition
def suggest_set(tracks: list[Track], analyses: dict[str, dict | None]) -> list[dict]                 # consecutive pairs
def partners(track: Track, analysis: dict | None, candidates: list[tuple[Track, dict | None]],
             direction: str = "out", limit: int = 8) -> list[dict]                                   # Partner dicts
def order_set(seed: Track, tracks: list[Track], analyses: dict[str, dict | None]) -> list[Track]
     # like mixing.order_set but also rewards good outro→intro fits when analyses exist
```
Suggestions are concrete and DJ-usable: where to start B (cue), where in A to bring it in, overlap in bars
(phrase-aligned), technique, notes ("A's 32-bar outro is drums-only; B's 16-bar intro too → 16-bar long blend
starting at A 4:12"), warnings (vocal clash, key clash, big pitch change, short intro).

### SET PLAN — `musicreco/setplan.py`
```python
def plan_set(tracks: list[Track], analyses: dict[str, dict | None], transitions: list[dict] | None = None,
             mix_length: str = "normal") -> list[Pick]      # one planner technique per consecutive pair
def annotate(tracks, analyses, transitions, mix_length="normal") -> list[dict]   # + plan_technique, plan_bars, note
```
Picks the set's transitions together, not one pair at a time: every pair's own pick costs nothing, alternatives that
still work for it cost what they give up, and a dynamic program over the set adds set costs. It never plays the same
cut or effect twice in a row and keeps long blends as the default. It breaks up a run of more than three blends with
a slam, cut or echo out, and makes cuts cheaper at the set's energy peaks. The picks depend only on the order, the
tracks and their analyses, never on saved edits. `CrateSet.transitions` carry them, and a generated MixPlan for a
consecutive pair uses the set's pick, so regenerating or editing one transition never reshuffles the others.

### SERVER — `seedbox/` (FastAPI)
`create_app(settings: Settings, static_dir: Path | None) -> FastAPI`; `python -m seedbox.server --port 8765 --dev`
(dev: CORS for http://localhost:5173). All JSON per `frontend/src/api/types.ts`. Long work runs in threads; the
event loop never blocks. Crate `{folder}` path params are URL-encoded folder names under `settings.output_root`
(reject traversal).

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{ok: true}` |
| GET | `/api/system` | SystemStatus |
| POST | `/api/system/update-ytdlp` | `{ok, version, message}` |
| GET/PUT | `/api/settings` | Settings (PUT partial) |
| GET | `/api/search?term=&page=` | SearchPage (term may be a Spotify/tunebat URL or 22-char ID → that track) |
| GET | `/api/crates/new/{seed_id}` | Crate; starts preview preload (seed, then recs by score) |
| GET | `/api/previews/{id}` · POST `/api/previews/{id}/prioritize` | PreviewStatus |
| GET | `/api/previews/{id}/audio` | audio/mp4 (Range) |
| GET | `/api/jobs` | Job[] (in-memory history of this session) |
| POST | `/api/jobs` `{seed_id, track_ids}` | Job (runs pipeline + complete_batch; queues analysis after) |
| GET / DELETE | `/api/jobs/{job_id}` | Job / cancel |
| GET | `/api/crates` | CrateSummary[] |
| GET | `/api/crates/{folder}/set` | CrateSet (queues missing analyses) |
| PUT | `/api/crates/{folder}/order` `{track_ids}` | CrateSet (library.apply_order) |
| POST | `/api/crates/{folder}/auto-order` | CrateSet (transitions.order_set → apply_order) |
| GET | `/api/crates/{folder}/partners/{track_id}?direction=out\|in` | Partner[] |
| GET | `/api/analysis/{folder}/{track_id}` | TrackAnalysis (404 until ready) |
| GET | `/api/files/{folder}/{filename}` | the MP3 (Range) |
| POST | `/api/reveal` `{path}` | opens Finder (`open -R`), path must be under output_root |
| GET | `/api/covers/{folder}/{track_id}` | embedded APIC art of that crate file (image/jpeg), 404 if none |
| POST | `/api/system/pick-folder` | PickFolderResult — native folder dialog via a hook app.py registers (501 without a window) |
| GET | `/api/events` | SSE: `data: <ServerEvent JSON>\n\n`, heartbeat comment every 15 s |
| GET | `/*` | frontend `dist/` SPA (index.html fallback) |

## Frontend
- React 19 + TS strict + Vite + Tailwind 4, built with Deno 2.9 (`deno task dev` → Vite on :5173 proxying `/api` to
  :8765; `deno task build` → `frontend/dist`). Hash routing (`#/search`, `#/crate/<seed_id>`, `#/crates`,
  `#/set/<folder>`, `#/settings`). `VITE_MOCK=1` serves fixtures (`frontend/src/mock/fixtures`) so screens can be built
  and screenshotted without the backend.
- One global audio player service (single `<audio>`), SSE hook, typed API client, toast system, keyboard shortcuts.
- Screens: Search (version picker) → Crate (recs table: score/colour/move chips, instant preview play with clip
  waveform, tick boxes pre-ticked by rule, Download button) → Downloads (live per-track progress) → Set builder.
- Visual QA: headless Chromium at
  `~/Library/Caches/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-mac-arm64/chrome-headless-shell`
  (`--headless --screenshot=<png> --window-size=1440,900 --virtual-time-budget=4000 <url>`); look at your screenshots.

## Rules for every agent
- Read this file (the v1/v2 engine contracts are at the end), `frontend/src/api/types.ts`, and the code you touch.
- Python: `.venv/bin/python` (deps installed; ask the lead rather than pip-installing). Style as in musicreco
  (`from __future__ import annotations`, type hints, short docstrings, sparse comments, no prints in library code).
- Scratch files go in your scratch dir, never in the project. Never modify `downloads/` or `~/Music/MusicReco` in
  tests (copy files out). Never read outside the project + your scratch dir (exceptions are named in your brief).
  No audible sound in tests. tunebat rate-limits (~4 quick calls → 429): ≤3 live tunebat calls per agent.
- Reply with: what you built (public API), how you tested it (real results), deviations/contract issues, rough edges.

---

# v4 — rendered transitions with DJ effects (user-approved 2026-10-03)

The user wants to HEAR each transition in the set builder: EQ bass swaps, band-pass/high/low-pass filter sweeps,
beat-synced echo throws, reverb washes, loops, and BPM changes (key lock on) — with editable automation lanes.
Long extended outro→intro blends when key/BPM allow (up to 32 bars), 4/8/16/32-bar options, slams/cuts otherwise.
Tempo: the master clock moves imperceptibly (gentle ramps over 16–32 bars) so tracks meet at transition points.
Full-set mix rendering is deferred (TODO.md). Contract: the "Mix plans & rendered transitions" block at the end of
`frontend/src/api/types.ts` (MixPlan, DeckPlan, Keyframe, LaneId, DeckLoop, MixRender).

## Ownership
- `musicreco/mixplan.py` (new) — PLANNER agent: generate/validate MixPlans from Tracks + TrackAnalysis + Transition.
- `musicreco/mixrender.py` (new) — RENDER agent: pedalboard (Rubber Band key-lock stretch, filters, delay, reverb,
  limiter) renderer MixPlan → audio + beat map; objective quality checks.
- `seedbox/*` — SERVER agent: endpoints below, plan persistence, render cache.
- `frontend/src/features/set/**`, `features/waveform/**`, `pages/SetBuilder*` — SET UI agent: lanes editor + playback.

## Python contracts
```python
# mixplan.py
PLAN_VERSION: int
def generate(a: Track, a_an: dict, b: Track, b_an: dict, folder: str, technique: str | None = None,
             bars: int | None = None, transition: dict | None = None) -> dict          # MixPlan
def validate(plan: dict) -> dict          # normalised copy (clamped values, sorted keyframes, every lane present); ValueError on nonsense
def lane_value(keyframes: list[dict], beat: float | np.ndarray) -> float | np.ndarray   # shared interpolation (renderer + tests)
def deck_time(deck: dict, beat: float | np.ndarray) -> float | np.ndarray               # master beat -> deck file time (loops honoured)
# mixrender.py
def render(plan: dict, a_path: Path, b_path: Path, out_path: Path, sr: int = 44100) -> dict   # MixRender minus audio_url/render_id
def plan_hash(plan: dict, a_path: Path, b_path: Path) -> str
```

## Endpoints (SERVER)
| Method | Path | Returns |
|---|---|---|
| GET | `/api/crates/{folder}/mix/{from_id}/{to_id}?technique=&bars=` | MixPlan (saved edits if any and no technique/bars given, else generated; without a technique a consecutive pair gets the set plan's) |
| PUT | same | save edited MixPlan → `.seedbox/mixplans/<from>__<to>.json`, returns validated MixPlan (edited=true) |
| DELETE | same | drop saved edits → generated MixPlan |
| POST | `/api/crates/{folder}/mix/{from_id}/{to_id}/render` (optional body: MixPlan to render unsaved edits) | MixRender (cached by plan_hash; SSE `{type:"render", data:{render_id, state, progress}}` while it runs) |
| GET | `/api/renders/{render_id}.m4a` | rendered audio (Range) |

---

# Engine — v1 + v2 behaviour and contracts

Folded in from the terminal-era SPEC.md (the original is in `_archive/`). The flow the app drives:

1. Search → `GET https://api.tunebat.com/api/tracks/search?term=<q>&page=<n>` (50/page). Results must
   disambiguate versions: title, artists, album, release date, duration, Camelot, BPM.
2. Picked track = **seed**.
3. `GET https://api.tunebat.com/api/tracks?trackId=<id>` → use **only `data.r`** (ignore `data.er` entirely).
4. Recommendations: seed first, every `r` track shown, pre-ticked iff it passes the rule
   (Camelot classic-adjacent AND BPM ±10% incl. half/double-time) unless the user pre-ticks all. Show why.
5. Download ticked tracks, **2 in parallel**: YouTube Music search first, fallback regular YouTube; accept the candidate
   whose duration is within **±15 s** of the Spotify duration (closest wins, prefer ytmusic). No candidate within
   tolerance → **skip** and log to `skipped.txt`. Output **MP3 192 kbps** (re-encoded from YouTube's lossy stream).
6. Tag each MP3 (ID3v2.3) from tunebat data + genre, write `tracks.csv` + `skipped.txt`.

Sample responses (parsed): `frontend/src/mock/fixtures/tunebat.json`.

## Output layout
```
<crates folder>/                     # library.downloads_root(), ~/Music/MusicReco by default
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

v2 adds more frames (below); v3 adds the rekordbox `.xml` and `.m3u8` playlists per crate.

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
DOWNLOADS_ROOT: Path   # v3: library.downloads_root() (the crates folder from settings)
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

## v2 — features + fixes (user-approved 2026-10-02)

Approved: (1) reliability bug fixes, (2) ranked + colour-coded recommendations, (3) 20 s preview clips,
(4) order the crate into a set, (5) energy 1–10 as **POPM star rating** + ReplayGain loudness tags, (6) Beatport extra tags.
Rejected in v2 (v3 later added packaging, the preview disk cache and rekordbox/M3U playlists): tunebat advanced search, using ~/Music/DJ, disk cache, tooling/packaging, SoundCloud,
download verification, comment tag, rekordbox/M3U, `er`, Spotify API. **Key/BPM tags always come from tunebat** —
Beatport key/BPM go to tracks.csv only (with mismatch flags). The pre-tick gate stays `mixing.passes_rule` exactly.

New shared types are in `musicreco/models.py` (lead-owned): `MatchResult.alternatives`, `ProviderUnavailable`,
`BeatportInfo`, `Score`, `Loudness`, and `DownloadResult.beatport/loudness/energy_rating/order`.

### v2 contracts

### `youtube.py` + `tunebat.py` (reliability)
- `find_match` fills `MatchResult.alternatives` (other in-tolerance candidates, best first, max 4).
- `download(candidate, dest)`: retry transient failures (HTTP 403/429/5xx, timeouts, "fragment"/"unable to download")
  up to 3 attempts with a fresh `YoutubeDL` and 2–5 s jitter. `YouTubeError` gets attribute `transient: bool`.
- New `fetch_clip(candidate: YTCandidate, start_s: float, length_s: float, dest: Path) -> Path` — short audio clip
  (yt-dlp `download_ranges`), any audio container afplay can play (m4a/mp3). Used by previews.
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
