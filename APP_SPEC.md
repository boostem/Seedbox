# Seedbox — desktop app spec (v3, user-approved 2026-10-03)

Seedbox turns the existing `musicreco` engine (SPEC.md v1+v2: tunebat seed → "r" recs → YouTube Music → MP3 192
→ ID3 tags → per-seed crate folders) into a modern **macOS desktop app** shared as a `.dmg` with friends.

## Decisions (do not change without the lead)
- **Shell:** Python backend (FastAPI + uvicorn on 127.0.0.1) + React web UI rendered in a native window via
  **pywebview**. Packaged with **PyInstaller** into `Seedbox.app` + `Seedbox.dmg`, Apple Silicon, unsigned
  (friends right-click → Open once). `python -m seedbox` runs it from source.
- **Look:** dark DJ-software style (rekordbox/AutoPilot-like): dense, high contrast, Camelot colours, 3-band
  waveforms. Name: **Seedbox**.
- **Crates folder:** `~/Music/MusicReco` (configurable in Settings). On first launch, crates in the project's legacy
  `downloads/` are moved there and `downloads/` is replaced by a symlink to the new root (so paths AutoPilot already
  imported keep resolving).
- **Previews:** when a crate opens, 20 s clips for the seed + every rec are **preloaded in the background**
  (3 at a time, ticked/high-score first, disk cache shared across sessions). Clicking play is instant.
- **New features:** interactive **set builder** (drag reorder, live transition scores, energy/BPM chart, Camelot
  wheel; re-exports TRCK + `.xml` + `.m3u8` + CSV order), **waveforms** (3-band, full tracks + preview clips),
  **intro/outro + mix-point analysis** with suggestions of which parts mix well (built-in numpy/scipy, ~1 s/track,
  tunebat BPM locks the grid).
- **Unchanged rules:** only tunebat `r` (never `er`), pre-tick = `mixing.passes_rule`, YT Music first ±15 s,
  MP3 192, key/BPM tags from tunebat, Beatport extras, energy POPM stars, set order TRCK, playlists `.m3u8` + `.xml`.
  The terminal CLI (`tunebat_dl.py`) keeps working.
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
  tunebat.py, genres*.py, tagging.py, audition.py   frozen (bug fixes only via the lead)
tunebat_dl.py                   CORE agent (CLI keeps working, uses shared helpers)
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
  backwards-compatible alias resolved at call time if feasible). `library.migrate_legacy_downloads(old, new) -> list[Path]`
  moves crate folders (never overwrites; skips name clashes and reports them), then replaces `old` with a symlink.
- `youtube.find_match` memoised per (track.id, tolerance) for the process lifetime (thread-safe, 1 h TTL) so previews
  and downloads share one search. `youtube`/`analysis` resolve ffmpeg/deno via `paths`.
- `mixing.passes_rule(..., tol=0.10)` / `score(..., tol=0.10)` accept the BPM tolerance (default unchanged).
- `pipeline.process_track/run_batch` take `tolerance_s` (default 15.0).
- Crate metadata persistence: `library.write_crate_json(seed_dir, seed, recs)` → `seed_dir/.seedbox/crate.json`
  (raw tunebat dicts for seed + recs, so Tracks can be rebuilt exactly); `library.load_crate_tracks(seed_dir) ->
  dict[str, Track]` (crate.json, falling back to rebuilding from tracks.csv for old crates).
- `library.apply_order(seed_dir, ordered_ids: list[str]) -> None` — TRCK n/total on the files, rewrite `.m3u8` +
  `.xml` + tracks.csv `order` column. `library.list_crates(root) -> list[dict]` (CrateSummary-shaped).
- `pipeline.complete_batch(seed, recs, scores, ticked_ids, seed_dir, results) -> CompletedBatch` — everything the CLI
  does after run_batch (finalize incl. earlier-run files, CSV, skipped.txt, .m3u8, .xml, crate.json). CLI + server share it.

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
| GET | `/api/previews?ids=a,b,c` | PreviewStatus[] in the order asked (≤500 ids; crate open also sends every track's status as `preview` events) |
| GET | `/api/previews/{id}/audio` | audio/mp4 (Range) |
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
| GET | `/api/jobs` | Job[] (in-memory history) |
| GET | `/api/events` | SSE: `data: <ServerEvent JSON>\n\n`, heartbeat comment every 15 s |
| GET | `/*` | frontend `dist/` SPA (index.html fallback) |

## Frontend
- React 19 + TS strict + Vite + Tailwind 4, built with Deno 2.9 (`deno task dev` → Vite on :5173 proxying `/api` to
  :8765; `deno task build` → `frontend/dist`). Hash routing (`#/search`, `#/crate/<seed_id>`, `#/crates`,
  `#/set/<folder>`, `#/settings`). `VITE_MOCK=1` serves fixtures (from tracks.txt/search.txt) so screens can be built
  and screenshotted without the backend.
- One global audio player service (single `<audio>`), SSE hook, typed API client, toast system, keyboard shortcuts.
- Screens: Search (version picker) → Crate (recs table: score/colour/move chips, instant preview play with clip
  waveform, tick boxes pre-ticked by rule, Download button) → Downloads (live per-track progress) → Set builder.
- Visual QA: headless Chromium at
  `~/Library/Caches/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-mac-arm64/chrome-headless-shell`
  (`--headless --screenshot=<png> --window-size=1440,900 --virtual-time-budget=4000 <url>`); look at your screenshots.

## Rules for every agent
- Read SPEC.md (v1/v2 behaviour), this file, `frontend/src/api/types.ts`, and the code you touch.
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
| GET | `/api/crates/{folder}/mix/{from_id}/{to_id}?technique=&bars=&style=` | MixPlan (saved edits if any and no technique/bars/style given, else generated) |
| PUT | same | save edited MixPlan → `.seedbox/mixplans/<from>__<to>.json`, returns validated MixPlan (edited=true) |
| DELETE | same | drop saved edits → generated MixPlan |
| POST | `/api/crates/{folder}/mix/{from_id}/{to_id}/render` (optional body: MixPlan to render unsaved edits) | MixRender (cached by plan_hash; SSE `{type:"render", data:{render_id, state, progress}}` while it runs) |
| GET | `/api/renders/{render_id}.m4a` | rendered audio (Range) |

---

# v5: loops & FX in transitions, full set mixes, setlists (2026-10-03)

Contract: the "Full set mixes" and "Setlists" blocks at the end of `frontend/src/api/types.ts`, plus `DeckFx` /
`DeckPlan.fx` and the `brake` / `spinback` techniques.

## Planner / renderer (musicreco/mixplan.py, mixrender.py)
- `PLAN_VERSION = 3`, `RENDER_VERSION = 2`. `DeckPlan.loops` are honoured everywhere (renderer, editor, saved
  plans), but generated plans only loop deck A for the `loop roll` technique and the `roll` style, both used only
  when asked for; every other plan plays each deck straight through. `DeckPlan.fx` =
  `[{kind: "brake" | "spinback", beat, beats}]` (validated, ≤ 8, beats 0.25–16); the deck is silent after it.
- Echo / reverb sends tap post-filter, pre-EQ. A plain phrase mix has no sends except the vocal-clash throw; every
  other send comes from the transition's style (below).

## Transition styles (MixPlan.style)
- `STYLES` = the default `"phrase mix"`, the blend flavours (`vocal throw`, `echo exit`, `roll`, `swell in`,
  `wash exit`, `filter wash`, `dub echo`; they ride on a long blend / phrase blend / bass swap) and the structural
  styles, which are their technique (`filter sweep`, `loop roll`, `double drop`, `echo out`, `slam`, `quick cut`,
  `drop swap`, `brake`, `spinback`). `validate` fills a missing style from the technique (else the default).
- Choice: the structure first (what the music allows: blend, cut, double drop), then the flavour;
  each candidate is scored 0-1 on the pair's analysis (vocal ends, energy lift where A leaves, B's intro, keys,
  tempo jump). A score is lowered by 0.04 per use of the same style in the previous 4 transitions, 0.03 per use of a
  same-family style (echo / wash / roll / turntable / cut) and 0.25 for a double drop / brake / spinback used in
  that window; a non-default style is never picked a 4th time in a row. Ties go to the phrase mix. `roll` and
  `loop roll` (`_MANUAL_ONLY`) are never picked automatically.
- `MixPlan.style_fit` = `[{style, fit 0-1, why}]`, best first: a blend's flavours plus the bigger moves that also
  suit the pair (double drop, filter sweep, echo out...), or a cut's exits. The editor ranks its menus with it.
- Echo throws (`_echo_shape`): the send is open on the last beat only (25 ms ramp, closed on the 1); send
  0.75-1.0 and B mid dip 0-6 dB from how much louder B comes in than A's last bar (a sung entry counts 6 dB more);
  feedback 10^(-24 / (20 (n - 1))) clamped to 0.35-0.72, n = repeats that fit before B's next vocal / drop (2-8),
  0.52 into a drop, 0.45 when A's last beat is sung and B comes in singing; delay 0.75 beat (0.5 below ~82 BPM,
  1 above ~161). Renderer: ECHO_RETURN 1.15, input band-limited once (200 Hz-5 kHz, 2nd order), one-pole filters in
  the loop, ping-pong (ECHO_PAN 0.35).
```python
generate(a, a_an, b, b_an, folder, technique=None, bars=None, transition=None, style=None, recent=None) -> dict
plan_styles(pairs: list[(a, a_an, b, b_an, transition)], fixed: list[str | None] | None = None) -> list[str]
```
  `recent` = the styles of the transitions before this one (most recent last); `generate(..., recent=chain[k-4:k])`
  reproduces `plan_styles(...)[k]`. `fixed[k]` = a saved plan's style. The server's `MixService.chain` runs
  `plan_styles` over a crate's (or setlist's) play order with the analyses known now, cached until the order, an
  analysis or a saved plan changes; plan endpoints accept `?style=` (422 for unknown styles), and `SetMixTransition`
  carries `style`.

## Full set mix (musicreco/setmix.py)
```python
build(entries: list[SetEntry], plans: list[dict]) -> Timeline   # one deck per track on a global beat axis
summary(tl, entries) -> dict                                      # tracklist + length without rendering
render(entries, plans, out_path, *, title, progress(f, msg), cancel) -> dict   # SetMixResult minus urls/paths
set_hash(entries, plans) -> str; cue_sheet(result, title, name) / tracklist(result, title); write_sidecars(...)
```

## Endpoints (SERVER)
| Method | Path | Returns |
|---|---|---|
| POST / GET | `/api/crates/{folder}/setmix` | SetMix (start / join) · SetMixStatus |
| POST / GET | `/api/setlists/{id}/setmix` | SetMix · SetMixStatus |
| GET / DELETE | `/api/setmix/{mix_id}` | SetMix (DELETE cancels) |
| GET | `/api/setmix/{mix_id}/audio` | the mix file (Range) |
| GET | `/api/crates/{folder}/links` | CrateLinks |
| GET / POST | `/api/setlists` | SetlistSummary[] · Setlist (`{crates, name?}`) |
| GET / PUT / DELETE | `/api/setlists/{id}` | Setlist (PUT `{name?, crates?}`) · OkResponse |
| POST | `/api/setlists/{id}/auto-order` | Setlist |
| GET / PUT / DELETE | `/api/setlists/{id}/mix/{from}/{to}` | MixPlan (a crate-to-crate link; `folder` = `"setlist:<id>"`) |
| POST | `/api/setlists/{id}/mix/{from}/{to}/render` | MixRender |

SSE: `{type: "setmix", data: SetMix}` on every state / progress change, `{type: "setlist_changed", data: {id}}`.
Render events of a link carry `folder: "setlist:<id>"`. Mixes are written to `<root>/Mixes/<name>/` (skipped by
`library.list_crates`), indexed in `<root>/.seedbox/setmixes.json`; setlists live in `<root>/.seedbox/setlists/`.

# v6: chill mixes + vocal stems (branch claude/chill-mixes, 2026-10-03)

Contract: `LaneId` gains `vocals` / `instrumental`; `MixPlan.swap_kind` / `vocal_stems`; `ChillStyle`, `SwapKind`,
`StemsStatus`, the `stems` SSE event and `TrackAnalysis.kick_per_bar` / `bass_per_bar` / `vocal_source` in
`frontend/src/api/types.ts`.

## Planner (musicreco/mixplan.py)
- `MODE` = `"chill"` (default) or `"fx"` (env `SEEDBOX_MIX_MODE=fx`: the v5 planner, kept intact). `PLAN_VERSION = 4`.
- Chill styles (`CHILL_STYLES`): `phrase mix` (default), `acapella`, `filter fade`, `double drop`, `quick cut`. No
  echo / reverb sends, no deck FX, no loop rolls; any other style asked for falls back with a note. No variety rule:
  each pair gets its own best fit (ties go to the phrase mix).
- Phrase mix: 16 bars (8 when the keys clash). B's fader up with its EQ shut; highs open over bars 1-4, mids over
  5-8; the lows change hands at the swap bar (B's main section on a phrase inside the blend, else halfway); after it A's
  mids leave (from a quarter of the way through the rest, right at the swap for keys a step apart), then its highs,
  its fader over the last quarter, a high-pass rising to 0.45 (~330 Hz) over the last half.
- Bass swap (`swap_kind`, `generate(swap=)` / `?swap=`): `hard` (on the 1), `quick` (over the bar before, -3 dB each at
  the crossing), `slow` (over 4 bars around it). By the songs: a soft low end on either side (low_per_bar < 0.45) →
  slow; a kick not on every beat on either side (kick_per_bar < 0.75) → quick; else hard. (`bass_per_bar` is
  exported but not used yet: on real songs it reads 1.0 for most basslines.) B without bass in the blend: A's lows carry it (`swap_kind` null); into B's drop: hard at the blend end.
- Loops only when needed: when A's clean ending (after its last sung bar, 4-bar grid) is shorter than the blend and
  the plain window would run over A's vocals or past its end, the blend starts on that ending and A's last 8 bars
  repeat (slip loop) until A is out; the repeat's vocal is muted (needs A's stems when those bars are sung; a fading
  or drum-less ending is never looped).
- Two vocals never at once (`_handoff`, needs the stem of the deck that goes quiet; else A's mids duck to -18 dB): B
  starts singing while A is between lines → A's vocal muted there; mid-line before the swap → B's vocal waits for A's
  line to end, at the latest the swap (the vocals change hands with the bass); later → A's vocal fades just before its
  line (or over the 2 beats before B's) unless A's line ends within 2 bars.
- Acapella (score 0.76): A's vocal split, keys same / ±1 / relative, tempo gap ≤ 2%, A singing across the swap bar on
  the plain window (a line running there or starting within a bar) that ends ≥ 2 bars after it inside the blend, and
  B not singing before then (or B split: its vocal waits). A's instrumental drops out on the 1 of the swap, its vocal
  rides on with its mids / highs open, then fades a beat after the line.
- Filter fade (0.66 when two full arrangements collide: A's outro under 4 bars or the blend's first half in A's main
  part, and B's intro under 4 bars or its main part arriving in the first half; 0.58 for keys a step apart, so it's
  offered but the phrase mix wins): B low-pass -0.7 opening over 3/4 of the blend, A high-pass to 0.6 from the first
  quarter; the bass still swaps. Double drop: as v5 (EQ + filter only).
  Quick cut: A out on the 1, nothing else (tempo gaps over 12% or when asked).
- Clashing keys: 8 bars, A's mids out over bars 2-3 (its vocal with them), B's mids in over bars 4-5.

## Stems (musicreco/stems.py, musicreco/separator.py, musicreco/roformer.py, seedbox/stems_queue.py)
- Model (`separator.MODEL`): unwa's BS-RoFormer "Resurrection" vocal model (BS-RoFormer, dim 256, depth 12, 62
  bands, STFT 2048 / hop 512, chunks of 785,920 samples with overlap 2; MVSep multisong vocals SDR 11.34, the
  cleanest "bleedless" scores among the public RoFormers, so a muted vocal leaves little behind). Weights:
  `https://huggingface.co/pcunwa/BS-Roformer-Resurrection/resolve/main/BS-Roformer-Resurrection.ckpt`, 204,510,749
  bytes, sha256 9dbfe5cb...2dd5, no license stated: downloaded on first use into `<app support>/models/`
  (resumable, retried, checksum-verified), never committed or bundled. Runners-up: becruily "deux" (Mel-Band,
  435 MB, CC BY-NC weights) and unwa "Leap" (vocal SDR 11.72, ~30% slower).
- `musicreco/roformer.py`: an inference-only port of MSST's BS- / Mel-Band RoFormer (MIT, derived from
  lucidrains/BS-RoFormer, MIT); checkpoints load with `strict=True`. Needs torch + einops only. The network runs on
  MPS when available (macOS 14+ with torch ≥ 2.9), STFT / mask math on the CPU, attention split to stay under
  ~512 MB per call; a GPU failure falls back to the CPU for good. The model is dropped after 5 minutes idle.
- Release builds bundle torch 2.11.0 (`RELEASE_TORCH`): the newest with macOS 12 wheels for Python 3.14.
- `<crate>/.seedbox/stems/<id>.flac`: the vocal stem (44.1 kHz stereo 16-bit FLAC, sample-aligned with
  `mixrender.decode_stereo`); `<id>.json`: cache key (size, mtime, STEMS_VERSION, model id, audio fingerprint),
  `vocal_db` / `mix_db` loudness envelopes at 20 fps. `vocal_per_bar(meta, analysis)` = share of a bar's frames with
  the stem within 15 dB of the track and 32 dB of its own loud level, full at 35%; `enrich` puts it in the analysis
  (`vocal_source: "stems"`, the heuristic kept as `vocal_per_bar_heuristic`).
- One worker; songs in use (open transition, setlist, set mix) at the front in play order; the rest of a crate in
  the background, started only while no transition or set mix renders. SSE `{type: "stems", data: {folder,
  track_id, state, progress, error?}}`; `GET /api/crates/{folder}/stems` → StemsStatus.
- Renderer: per deck, where its `vocals` / `instrumental` lanes move and the stem exists, the source becomes
  `g_i * (track - vocals) + g_v * vocals` before the time stretch (both at 1 = the original samples). `plan_hash` /
  `set_hash` include the stem file when a deck uses it. `RENDER_VERSION = 4`.
- Analysis: `ANALYSIS_VERSION = 5` adds `kick_per_bar` and `bass_per_bar` (between-kick low level vs the bar's).
