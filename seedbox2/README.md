# Seedbox 2

The next generation of Seedbox. The goal is mixes that sound better than a good human DJ, in the cleanest UI in DJ
software (the waveforms stay), on an ordinary laptop. It was planned in board ticket #34. The work lives in the
`S2 …` epics on the board, tickets #35–#89.

## Run from source

Do the [Setup](../README.md#setup) in the main README first (`.venv`, `ffmpeg` and `deno`).

```sh
.venv/bin/python -m seedbox2                        # opens the Seedbox 2 window
(cd frontend2 && deno install && deno task build)   # after frontend changes (writes frontend2/dist)
```

The first run builds `frontend2/dist` itself when it is missing, so it takes a little longer (the terminal says so).
It doesn't notice a stale build: after pulling frontend changes, run the build line.

To work on the UI with hot reload, use two terminals:

```sh
cd frontend2 && deno task dev           # Vite on http://localhost:5174, proxies /api to 127.0.0.1:8766
.venv/bin/python -m seedbox2 --dev      # API on :8766; the window loads the Vite page
```

These ports differ from Seedbox 1's (5173 and 8765), so both apps can run side by side, in dev mode too.

Flags:
- `--server-only [--port N]`: no window. Open the printed URL in a browser.
- `--dev`: as above. `--debug`: verbose logs and the web inspector.
- `--self-test [--report FILE]`: JSON diagnostics: ffmpeg/deno running, imports, onnxruntime running a tiny embedded model
  (in-process and in a spawned worker), and the server answering `/api/health` and serving `frontend2/dist`.
- `--version`

Tests: `.venv/bin/python -m pytest tests/test_seedbox2.py tests/test_seedbox2_master.py tests/test_seedbox2_eval.py tests/test_seedbox2_packaging.py tests/test_bench.py tests/test_seedbox2_library.py tests/test_seedbox2_setexport.py tests/test_seedbox2_bridge.py tests/test_seedbox2_jobs.py tests/test_seedbox2_power.py tests/test_seedbox2_waveform.py tests/test_seedbox2_import.py tests/test_seedbox2_mixmetrics.py tests/test_seedbox2_stretch.py tests/test_seedbox2_golden.py tests/test_seedbox2_models.py tests/test_seedbox2_library_view.py tests/test_seedbox2_stems.py tests/test_seedbox2_beats.py tests/test_seedbox2_sections.py tests/test_seedbox2_vocals.py tests/test_seedbox2_energy.py tests/test_seedbox2_listen.py tests/test_seedbox2_mix_plan.py tests/test_seedbox2_stream.py tests/test_seedbox2_render.py tests/test_seedbox2_engine_parity.py tests/test_seedbox2_engine_chunks.py tests/test_seedbox2_engine_browser.py tests/test_seedbox2_automation.py tests/test_seedbox2_onboarding.py tests/test_seedbox2_tunebat_crate.py tests/test_seedbox2_sets.py tests/test_seedbox2_longset.py tests/test_seedbox2_now_playing.py tests/test_seedbox2_assistant.py`, and in `frontend2/` `deno task build`, `deno task test`, `deno task lint` and `deno task screenshot` (Docker; see `frontend2/DESIGN.md`).

Seedbox 2 keeps its own settings, caches and logs in each OS's usual folders (`seedbox2/paths.py`, from
`platformdirs`), so trying it never touches Seedbox 1's sets or settings:

| | macOS | Windows | Linux |
|---|---|---|---|
| Settings and data (`$SEEDBOX_SUPPORT_DIR`) | `~/Library/Application Support/Seedbox 2` | `%LOCALAPPDATA%\Seedbox 2` | `~/.local/share/seedbox2` |
| Cache (`$SEEDBOX_CACHE_DIR`) | `~/Library/Caches/Seedbox 2` | `%LOCALAPPDATA%\Seedbox 2\Cache` | `~/.cache/seedbox2` |
| Logs (`$SEEDBOX_LOG_DIR`) | `~/Library/Logs/Seedbox 2` | `%LOCALAPPDATA%\Seedbox 2\Logs` | `~/.local/state/seedbox2/log` |
| Music library (`$MUSICRECO_OUTPUT_ROOT`), shared with Seedbox 1 | `~/Music/MusicReco` | `Music\MusicReco` | `$XDG_MUSIC_DIR/MusicReco` |

The environment variables override each folder; `#/settings` shows the folders in use, each with a Show in
Finder/Explorer/file manager button. The window is Cocoa on macOS, WebView2 on Windows (the Edge WebView2 runtime
ships with Windows 10 and 11) and WebKitGTK or Qt on Linux, with a dark title bar where the platform allows it.
ffmpeg and deno are found on the PATH or in the usual package manager folders of each OS (`musicreco/paths.py`).

**Installers** (#88, `packaging/README-seedbox2.md`): `packaging/build_seedbox2.py` builds a DMG on macOS (arm64 and
Intel, separately) and an Inno Setup installer on Windows, each under 250 MB, with an LGPL ffmpeg and deno inside and no
torch. CI builds them and smoke tests what gets installed (`installers.yml`). The models download on first run.

**What's there so far** (#36): the server (`seedbox2/server/`: `/api/health`, `/api/system`, the `/api/events`
stream and one empty router per area) and the `frontend2/` app shell (#72: an icon rail, a top bar, a transport strip,
the ⌘K command palette and the `?` shortcuts sheet, a placeholder page per area, `#/settings`, and `#/ui` with the
design tokens and the waveform). The other tickets fill in the areas.

**The library** (#38, `seedbox2/library/`): `library.sqlite3` in the support folder (WAL), keyed by a SHA-1 of
the decoded audio (duration and the first and last 30 s of PCM), so retagging, renaming or moving a file keeps its
analyses. `db.py` holds the schema as numbered migrations (`PRAGMA user_version`; append, never edit) and the
connections: one writer shared by all threads behind a lock, a read-only connection per thread; worker processes
open their own. `repo.Library` is the only way in (no SQL in the routers). `POST /api/library/import/seedbox1`
(optionally `{"root": "<crates folder>"}`) imports Seedbox 1's crates, their running order as sets and its edited
mix plans, without writing to Seedbox 1's files; importing again updates them and keeps plans edited here since.

**Jobs** (#39, `seedbox2/jobs/`): analysis and render work runs in spawned worker processes, never on the server's
threads. A job type is a module-level function `fn(payload, ctx)` registered with a pool
(`@register("beats", pool="light")`); `ctx.progress(f)` reports progress and stops a cancelled job,
`ctx.cached(key, factory)` keeps an ONNX session per worker across jobs, `ctx.library` is the worker's own library
connection. The `heavy` pool has one worker (stems, full renders) and starts no job while less than 1.5 GB of RAM is
available; the `light` pool has cores - 2. The queue runs the highest priority first (`PRIORITY`: the open set, the
open crate, the library, background); `POST /api/jobs/bump` with `{"hashes": [...]}` moves the jobs about what the UI
just opened to the front. Every job is a row in the `jobs` table: after a restart, jobs that were running are queued
again (or failed, for types registered `resumable=False`). A job that ignores a cancel, or a worker that crashes,
costs only that worker; the supervisor starts another. `/api/jobs` lists, submits and cancels; `/api/events` carries
`job.queued`, `job.started`, `job.progress` (coalesced: a slow client only gets the latest per job) and the terminal
`job.done`, `job.failed` and `job.cancelled`, which are never dropped.

**Low-power mode** (#85, `seedbox2/power.py`): Settings → Low-power mode turns it on automatically (on battery or when
the machine runs hot, the default), always, or never; `GET /api/power` and the `power` event say whether it is on and why,
and the top bar shows what is paused. Battery or mains comes from `/sys/class/power_supply` on Linux and psutil on macOS
and Windows; heat from `NSProcessInfo.thermalState` on macOS and the kernel's trip points in `/sys/class/thermal` on
Linux (Windows has no such API, so only the battery counts there). While it is on: stems at background priority wait, while
a set you open still gets its stems (from the INT8 model); half the light workers (idle ones leave at once, busy
ones when they finish); beats use small0; waveforms draw at 30 fps. At `critical` heat the library's analysis waits too
and one light worker is left. A job already running finishes. Jobs analysed in this mode keep the small model's result.
Whatever the mode, worker processes run at low OS priority (`nice` +10, `BELOW_NORMAL_PRIORITY_CLASS` on Windows), so the
UI and audio come first and a job still gets the whole machine when nothing else wants it. The two "Always use…" switches
under Models and Stems choose the small models whether low-power mode is on or not. The benchmark's frontend run turns the
mode off, since its 60 fps budget is for the normal mode on battery.

**The waveform renderer** (#74, `frontend2/src/features/waveform/gl/`): one WebGL2 context for the library overviews,
the track view and the transition editor; `WaveOverview` is the strip for preview clips and for the decks when they
exist, with its playhead on an overlay. RGB, 3-band and stacked modes are chosen in `#/settings`, and Canvas 2D takes
over when WebGL2 isn't there.

**The centred waveform** (#76, `TrackWaveform` with `deck`): the waveform scrolls under a fixed playhead, as on a
Rekordbox or Serato deck, with a beat-phase ruler (the bar's four beats) and the bars.beats to the next phrase; the
Centred/Page switch beside the zoom buttons is kept in localStorage, and page mode stays for editing (a view without
`deck` pages, as the transition editor will). A drag or horizontal scroll takes the view for 2.5 s. The playhead comes
from `AudioContext.currentTime` every animation frame, with no store or React: `player.time()` anchors the audio element
to a context that runs while it plays, `LiveEngine.seconds()` has its own, and `engineSource(engine, toTrack)` passes
that to the view as `source`. As `currentTime` moves in steps of the output buffer, `FrameSmoother`
(`frontend2/src/audio/playhead.ts`) predicts from the display's frame times and follows the clock slowly. Measured with
`python -m seedbox2.bench frontend` (scenario `play_centred`, headless Chrome on SwiftShader, 10 s, on a 12-thread desktop under other load, not yet the reference laptops): 59.6 fps,
frame time spread 1.36 ms, no long tasks, no React commits; the raw clock's unevenness 8.98 ms a frame, the playhead's 0.41 ms.

**Waveform overlays** (#82, `draw.ts` and `grid.ts` in `frontend2/src/features/waveform/`): what `TrackWaveform` draws
around the waveform, on 2D canvases under and over the WebGL layer that repaint only when the view, the data or the
look changes (a centred deck: when it scrolls a device pixel), never for the playhead, which has its own layer.

- **Beatgrid:** beat, bar and phrase lines with bar numbers. Lines follow the piecewise grid (`TrackAnalysis.grid`, the
  segments of `seedbox2/analysis/grid.py`; the now-playing session sends them as `grid.segments`) and fall back to
  `first_downbeat_s` + `bar_s`. A phrase line weighs more for the phrase it opens: 8 bars thin, 16 brighter, 32 two
  pixels (`PHRASE_LINE`), from `phrases[]` when analysed (the session sends `phrases` from the sections analysis) or the
  8-bar grid. Zoomed out, shorter phrases drop before longer ones, and bar numbers stay on the phrase starts that fit.
- **Phrase bands:** each phrase tinted by its section (intro, build, drop, breakdown, outro: the music colours),
  every other one a little stronger to count by. On by default.
- **Cues:** a hot cue (`CuePoint.slot`, 0-7) takes its slot's colour (`cue-1`…`cue-8`) with its letter; the analysis'
  cues keep their section or mix colour and merge when they coincide. Labels that would overlap stack in up to three
  rows, as Mixxx 2.4 does, keeping clear of the intro and outro labels.
- **Transition:** a region with a `technique` is tinted in its style's colour (`style-blend`, `-swap`, `-filter`,
  `-echo`, `-cut`; `styleOf`), as is the overlap in the transition editor (`MixWaveform`).

**Now playing** (#77, `frontend2/src/features/nowplaying/`, `#/now-playing?set=ID`, ⌘K "Go to Now playing"): the set as two
stacked decks, full screen and without the shell. Both waveforms scroll under one playhead column, zoomed together in bars so
a beat is the same width on both, so the grids line up when the plan has the tracks in sync; its phrases are tinted by section and
the transition is marked on each deck's track in its style's colour. A deck's panel shows its BPM, its pitch against its own tempo, its key and the
time left, with stem meters and the plan's EQ, filter and fader moving as it plays; the strip along the bottom names the next track, the
technique and the bars to go. `?demo=1` shows it on synthetic data (screenshots, the benchmark).

- **Playing a set:** `GET /api/sets/{id}/session` gives the tracks and each pair's plan as version 3. `SetDriver` plays those on
  one `LiveEngine`: `planChain` stretches each plan's window over its tracks (the first from the top of its A, the last to the
  end of its B), and 8 beats before a window ends the next plan takes over. `engine.schedule(next, { from: "b", to: "a" })`
  has the worklet carry the file position across, so the track goes on from its sample. A pair with no plan stops the set after that track, and the strip says so
  beforehand. Track `i` stays on deck A or B by its parity, so a track doesn't move when the next plan takes over. `planChain`
  draws the set-wide tempo line (under "Exporting a set"). The timeline's own Play button (`SetPlayback`, #75) still plays each
  plan's own tempo map.
- **Meters aren't output levels:** the worklet taps no stems, so a meter is that point of the stem waveform by the plan's gain and
  fader (the 3 bands through the EQ without stems). Nothing else in this view reads the audio.
- **Measured** (`python -m seedbox2.bench frontend`, scenarios `now_playing` and `now_playing_low_power`; 1440x900, on an RTX 4070 Ti
  desktop running WebGL on SwiftShader, with the CPU throttled 4x as well): 58-60 fps, no long tasks, no React commits; at low power's cap
  the decks draw 30 times a second. **Not yet measured on a real integrated GPU or the reference laptops.**

**Waveforms** (#49, `seedbox2/analysis/waveform.py`): the `waveform` job (light pool, so it never waits behind
stems) decodes a track to mono at 22,050 Hz, a second at a time, and splits it with 4th-order Bessel filters
(low-pass 600 Hz, band-pass 600-4,000 Hz, high-pass 4,000 Hz, as Mixxx). Each band keeps its peak and RMS at 150
points/s, plus a 1,200-column overview (as Rekordbox) and, once the track has stems, one full-band overview row per
stem. Values are bytes on a 48 dB log scale in 56 steps (`amplitude = 10 ** ((byte / 255 - 1) * 48 / 20)`). The file
is a 32-byte header (`SBWF`, version, points/s, bands, counts, duration) and planar Uint8 rows, gzipped in the cache
folder's `waveforms/`: about 0.2 s and at most 187 KB per 6 minutes of music (measured on 20 tracks). `GET
/api/tracks/{hash}/waveform` serves it as `application/octet-stream` (gzip-encoded when accepted) with an ETag from
the content hash, so an unchanged waveform is a 304; before it is computed it answers 202 and queues the job at the
front. Importers call `waveform.submit(jobs, hash)` first; the stems job (#44) calls it again to add the stem rows.

**Stem waveforms** (#78, `StemWaveform` in `frontend2/src/features/waveform/`): drums, bass, vocals and other, in their
stem colours, as one waveform split into four layers or as four thin lanes (Settings → Waveforms → Stems). Stem detail
is a file of its own (`<hash>.sbsw.gz`, 150 points/s, same byte scale, about 149 KB per 6 minutes on dense noise),
so the 200 KB file every view loads doesn't grow: the waveform job writes both when the track has stems, and `GET
/api/tracks/{hash}/waveform/stems` answers 200, 202 while the job writes it, or 404 until the track has stems. Whole
tracks and windows read the same file through the peak mip chain. `gainsAt` takes the gains at the playhead, normally
`planStemGains(deck, beat)` (the curves of `lane_value`), and each frame they go to the shader as a uniform with no
React render, so a stem dims as the plan plays it down. The dimming applies to the whole view at the current gain;
the transition editor (#79) will mount it with the engine's clock (#65), and `#/ui` plays a synthetic bass hand-off. The
now-playing view (#77) draws the band waveform on its decks and reads the same gains for its stem meters.

**Energy, loudness and the vibe** (#48, `seedbox2/analysis/energy.py`, `loudness.py`, `clap.py`, `vibe.py`): the
`energy` job (light pool) runs once a track's beats have finished, and again if a grid or stems arrive later. One streamed
decode at 48 kHz gives both analyses.

- **Loudness:** BS.1770 in numpy: 400 ms K-weighted blocks every 100 ms and a 4x-oversampled true peak per second.
  On 8 real tracks it agrees with ffmpeg's `ebur128` within the 0.1 dB ffmpeg prints (integrated at most 0.04 LU apart, true peak at most 0.05 dB). Those two curves answer everything else, so a change of sections needs no new decode:
  `GET /api/analysis/{hash}/loudness` adds the integrated LUFS and true peak per section, and
  `loudness.overlap_gain_db` is the gain that matches two tracks over an overlap, for the mix engine.
- **Energy:** each bar gets loudness, onset density, brightness, and drums and bass presence, on fixed scales, from the
  stems when they're cached and otherwise from proxies in the mix. `GET /api/analysis/{hash}/energy` has the 0-1 curve,
  the track's 1-10 `rating`, and every bar's features.
- **Calibration:** **not fitted to hand ratings yet: none exist.** The files carry no Mixed In Key tags either (checked on
  1,500 of the library's files). The weights in `energy.CALIBRATION` are an expert prior. The scale comes from the
  distribution of 152 real tracks (56 from the golden corpus, 96 from the library at random): their 5th percentile is a 3
  and their 95th a 9. The order looks right (C418 and Boards of Canada at 1, acid and peak-time house at 9), but deep
  house also reaches 9 where Mixed In Key would probably say 6, so treat the numbers as provisional. To fit it, rate at
  least 15 tracks (`python -m seedbox2.eval energy template > ratings.toml`) and run `energy fit ratings.toml`, which prints
  leave-one-out Spearman and mean error, and the `CALIBRATION` to paste.
- **CLAP:** `laion/larger_clap_music_and_speech`, Xenova's FP16 ONNX export, chosen on the golden tracks over the music-only
  checkpoint, whose text queries all returned the same tracks (results below). `python -m seedbox2.models.export
  clap_audio --check` (and `clap_text`) checks the file and our front ends against torch and transformers:
  the embeddings are within 2.4e-4 per component of torch's, the log-mel within 4e-5 dB and the tokens identical. The `clap` job embeds 3-5 windows of 10 s (`intro`, `main`, `outro`, and if there's room
  `breakdown`, `second`), chosen from the `sections` analysis or the energy curve, into `<cache>/embeddings/`. On one thread a window takes about 0.36 s to embed, so a track costs a few seconds, plus 6 s to open the graph once per worker on a loaded machine. Opening it adds about 540 MB to the worker.
- **Text:** `clap_text` runs in a worker that closes the 251 MB encoder afterwards, because any of the light workers could
  otherwise keep a copy. Vectors are cached on disk, so a repeated text costs nothing. A new text took 1-2.6 s through a worker on a real library of 30 tracks, and a repeated one under 10 ms.
- **Similarity:** `GET /api/analysis/{hash}/similar?limit&window&bpm&key&bpm_pct` compares each of the seed's windows with
  the same window of the others, and keeps tracks within 6 % of its tempo (also half and double time) and adjacent on the
  Camelot wheel, unless `bpm=false` or `key=false`. A track whose BPM or key is unknown doesn't pass a filter on it.
  `POST /api/analysis/vibe/search` `{text, limit, bpm, bpm_pct, key}` ranks by a track's best window; the Python
  function is `vibe.search`, which the set assistant (#63) and gap suggestions (#64) will call.

Which CLAP (56 golden tracks, 3 windows each, FP16 against FP32 files): `larger_clap_music_and_speech` found the genre of
43 % of the tracks from text alone, 73 % by nearest neighbour and 50 % in the top five. `larger_clap_music` scored 27 %,
29 % and 30 %, and returned nearly the same tracks for every text ("dark", "warm", "euphoric"). The INT8 text encoder was
rejected: its cosine with torch's falls to 0.97, enough to change rankings. Subtracting the library's mean vector didn't help
either (38 % instead of 47 %). The 47 % uses the caption "this is X music", which `vibe.PROMPT` adds. This is a small test,
and even the chosen model confuses neighbouring genres: expect a useful ordering, not exact labels.

**Local music** (#40, `seedbox2/library/importer.py`, `watch.py`, `playlists_xml.py`, `tags.py`): MP3, AAC/M4A,
FLAC, WAV, AIFF and OGG/Opus come in three ways. *Watch folders* (Settings; kept in `settings.json` next to the
library, default: the music folder) are scanned at start, then followed with `watchdog`: new and changed files are
added once they stop growing, moved ones keep their track (same hash, same analyses), deleted ones are forgotten (a
track with no file left goes, unless a crate, set or plan uses it). *Dropping* files or folders onto the window
hands their paths to `POST /api/library/import` (pywebview's drop event); in a browser they are uploaded into the
music folder's `Imported` folder. *Rekordbox XML and iTunes/Music library XML* (`POST /api/library/import/xml`):
every playlist becomes a crate named by its folder path, and Rekordbox's cues and beatgrid are kept as the track's
`rekordbox` analysis. Tags (mutagen) fill the title, artist and album; BPM, key, comment and the rest are hints in
`tracks.tags` (`file`, `rekordbox`, `itunes`), never analysis. Files inside a Seedbox 1 crate folder go through the
Seedbox 1 importer, so tunebat downloads into the music folder land in the library with their crate. Every new
track queues the job types registered `per_track=True`: the light ones at library priority, the heavy ones (stems)
at background priority. Progress is the `library.import` event.

**Crates from tunebat** (`seedbox2/library/tunebat_crate.py`, `#/discover`): Seedbox 1's search → crate → download.
Search tunebat (a song, an artist or a Spotify link; `#/discover?q=…` starts with a search) and open a result. It
comes with every track tunebat recommends from it, best first by musicreco's mixing score. The ones that pass Seedbox 1's
pre-tick rule (a compatible key and the tempo within the tolerance of Seedbox 1's settings, half and double time
count) are marked "Mixes" and start ticked with the seed. The others start unticked, unless "Tick every recommendation"
is on (kept in the browser's storage). Tracks the library already has (same title and artist, or the tunebat id
Seedbox 1's import kept) start unticked. The download is musicreco's batch, as in Seedbox 1: YouTube, tags, loudness,
`tracks.csv` and `crate.json`, in the music folder's `<Artists - Title>` folder. The importer then takes the folder in
as a crate and a set. One download runs at a time. Tunebat calls go through the first run's client: one at a time,
1.2 s apart. API: `GET /api/tunebat/crates/{seed_id}`, `POST /api/tunebat/crates/{seed_id}/download` `{ids}` (202,
progress as `tunebat.crate` events with the whole download), `GET /api/tunebat/crates/downloads` and
`POST /api/tunebat/crates/downloads/{id}/cancel`.

**Models** (#42, `seedbox2/models/`): every model runs through onnxruntime (no torch in the app). The weights aren't
bundled: `registry.py` lists each model with a version, a URL pinned to an upstream revision, its SHA-256, size,
licence and attribution, and what Seedbox 2 uses without it. When the app starts it downloads the missing ones into
`models/<name>/<version>/` in the cache folder, in the background (Settings → Models → "Download missing models when
Seedbox 2 starts"). Downloads resume from a `.part` file with an HTTP Range request, and only a file whose SHA-256
matches is moved into place. A new version replaces the old one once it is verified. Without a model the app works
as before: `ctx.session_or_none(name)` gives a job None and the job uses the Seedbox 1 heuristics, and
`GET /api/models` and Settings → Models list what is missing and what is used instead. A job's
`ctx.session(name)` is one `InferenceSession` per worker process with inter-op threads = 1 and intra-op threads =
the physical cores of its pool's share, divided between the pool's workers (light: all the cores; heavy, which runs
beside it: half). With the `accelerator` setting it uses CoreML on macOS or DirectML on Windows
(`onnxruntime-directml`), with the CPU after it and on its own if the accelerator can't take the graph. Changing the
setting affects workers started after the change. `/api/models` lists the models and starts, cancels and deletes
downloads; progress is `model.progress`, then `model.done`, `model.failed` or `model.cancelled`, and a `models`
event with the new status. `seedbox2/models/export/` (dev-only, the one place torch is allowed) rebuilds the ONNX
files from the upstream checkpoints and checks any file against torch:
`python -m seedbox2.models.export beat_this --check FILE` (the registry's file differs from torch by at most 2e-6

**Key detection** (#45, `seedbox2/analysis/key.py`): the `key` job (light pool, per track) makes chroma with a
constant-Q transform (one bin per tuned semitone, C2 to C7, about 0.1 s per track, with numpy and scipy only) of the
bass and other stems or, without stems, the mix, averages it per beat and correlates it with the 24 rotations of
Faraldo's `edma` profile (`bgate` is there too), plus a small prior for minor keys. The tuning is estimated first
(a track 40 cents sharp keeps most of its score: 0.62, against 0.53 without it). The same match on 8-bar blocks,
smoothed with Viterbi, gives `changes`, stretches of at least 16 bars in another key (5.5 % of GiantSteps tracks);
when #46 lands its sections can replace the blocks. The job runs on what is there, and runs again when the beats or
stems arrive. `confidence` is the chance of the right key or a fifth, fitted on half of GiantSteps; below 0.4 the key
is unknown: the table shows "?", `key.planning_key(lib, hash)` gives the planner None, and `best_guess` keeps what it
would have been. Tag and tunebat keys are never used as ours: until the analysis they fill in as grey hints, after it
the first one that disagrees (`key_hint`) sits next to our key. Clicking a key opens the editor: confidence, source,
key changes, hints, a wheel to set your own key (the `key_override` analysis, which survives re-analysis) and the
Camelot/Open Key switch (remembered in the browser). `GET /api/analysis/{hash}/key`, `PUT` and `DELETE
/api/analysis/{hash}/key/override`.

Measured on all 604 GiantSteps Key tracks (MIREX weighted score, on the mix, with Beat This! beats): **0.664**, under
the ticket's 0.70 target. Half was used for tuning: 0.669 there, 0.658 on the half held out; 59.6 % exact, 8.6 %
fifth, 5.0 % relative, 4.8 % parallel, 22.0 % other. As a reference, Essentia 2.1's `KeyExtractor` on the same files
gets 0.650 with `edma` and 0.680 with `bgate`, so this is what template matching reaches here, not a bug. Counting
only the 87.6 % of tracks with a confident key, the score is 0.716 (held-out half: 0.698), and 0.296 on the ones shown
as "?". The stems did not help: on the 53 tracks separated, bass + other scored 0.542 and the mix 0.609 on the same
tracks (difference -0.068 ± 0.071, 95 %), which is too few tracks to call it worse, but it isn't the gain the plan
expected. Our own corpus is empty so far, so it hasn't been scored.
**Vocals** (#47, `seedbox2/analysis/vocals.py`): the `vocals` job (light pool) runs once a track has both stems and beats, and reads only the stems, so it needs no source file. It stores per-beat activity on the track's grid, phrases (start and end beats, kind `lead`, `rap`, `chop` or `backing`) and `acapella` and `instrumental` windows, served by `GET /api/analysis/{hash}/vocals`. `vocals.plan_input(analysis, data)` swaps Seedbox 1's `vocal_per_bar` and `has_vocals` for these before `musicreco.mixplan.generate`, so its clash checks read them; a track without stems keeps the heuristic. Nothing in the app calls it yet: Seedbox 2 has the plan schema (#51) but no planner of its own, so whichever ticket adds the planner should feed it `plan_input_for(lib, hash, analysis)`.

Measured with `python -m seedbox2.eval analysis --analyser seedbox2-vocals --analyser seedbox1` (`fetch jamendo`, `fetch musdb18 --limit 12`). The likelihood's weights were fitted on Jamendo's validation split, so neither row below was seen in fitting. Frame F-measure, in points:

| Tracks | `seedbox2-vocals` | `seedbox1` |
|---|---|---|
| Jamendo test split, 16 tracks, hand labels | 92.9 | 62.6 |
| MUSDB18-HQ test split, 12 of 50 tracks, truth from the true vocal stem | 88.6 | 58.7 |

The target is 90, so MUSDB18 misses it. Its median is 93.5, and the misses are vocals quiet in a dense mix: `PR - Oh No` scores 54 with precision 1.00 and recall 0.37. Moving the truth's level threshold from -35 to -20 dB only reaches 89.6. Comparing the stem with its own loud level raised MUSDB18 to 89.8, but on real instrumentals (each mix minus its true vocals) it called 10.5 % of the audio vocal instead of 3.4 % (worst track 24.7 % instead of 13.2 %), so it isn't used. Refitted on level alone, the validation split's frame F is unchanged at 93.2, so flatness and harmonicity add nothing measurable to activity. The phrase kinds have no labelled data behind them, only synthetic tests: on the 16 validation tracks they gave 155 `lead`, 6 `backing`, 1 `chop` and no `rap`, so treat them as unvalidated.

**The library view** (#73, `frontend2/src/features/library/`, `seedbox2/library/view.py`): `#/library` is a
virtualised table of every track (artwork, title, artist, BPM, Camelot-coloured key, energy 1-10, length, genre, an
overview waveform and the analysis/stems status). `GET /api/library/view` sends every track as one compact row
(`POST` with `{"hashes"}` for a few); our analyses win (`beats`/`tempo` → `bpm`, `key` → `camelot`, `energy`), and
until a track has them the hints in its tags fill in (Rekordbox, Seedbox 1/tunebat, the file, iTunes; shown in grey
italics). The client filters and sorts those rows (`filter.ts`): vibe text over title, artist, album, genre, comment
and label; a BPM range (optionally counting half and double tempo); keys that mix with a Camelot key; energy; "not
analysed yet". A filter can be saved as a smart crate (`GET`/`PUT /api/library/smart-crates`, in `settings.json`).
The waveform column is one canvas over the table: `POST /api/library/overviews` returns the 1,200-column overviews of
the rows in view in one binary response, and each row's image is drawn once and blitted on scroll. Rows drag (the
selection, or the row grabbed) onto a crate or a set in the side list, or onto "New crate"/"New set"
(`POST /api/library/crates[/{id}/tracks]`, `POST /api/sets[/{id}/tracks]`); files dropped from the OS import as
before. The top bar shows the analysis queue from the `job.*` events: what runs, the stems' progress, what waits and
an ETA learnt from the jobs seen finishing. Measured on 3,000 tracks in headless Chrome: 60 fps scrolling, and 56-58 fps
with no long tasks at a 4× CPU throttle.

## Mix plans

**The plan** (#51, `seedbox2/mix/plan.py`): one transition as JSON, version 3 (Seedbox 1's are version 2), for the
offline renderer and the live engine. Lanes, loops, effects and the tempo map sit on Seedbox 1's master timeline in
beats. Every deck has a piecewise `grid`, and `deck_time(deck, beat)` goes through it, loops included: to place an
analysed grid, use `Grid.scaled(factor, offset).to_json()`. `key_shift` is whole semitones per deck (±2).

`stem_lanes` (`gain_drums`, `gain_bass`, `gain_vocals`, `gain_other`) are linear gains, so 0 is true silence; `null`
means the deck plays the full mix. `validate(raw)` gives the plan as stored, for an editor.
`validate(raw, has_stems=...)` gives it as it plays: a deck without stems has its stem lanes folded into its EQ (bass
into the lows, vocals into the mids, drums into the highs; "other" has no band and stays up), and the plan's notes say
so.

Validation refuses bad values rather than clamping them. Loops start, end and read from whole beats, and last whole
beats or ½, ¼, ⅛ or 1/16 of one (Seedbox 1's loop rolls end in ½-beat loops).

**Stretching** (#52, `seedbox2/mix/stretch.py`): `render_deck(plan, "a", audio, sr)` gives a deck's audio on the
master timeline, and `render_stems` does that for each stem. The engine is Signalsmith Stretch (`python-stretch`):

| 32 bars at a tempo ramp | Timing | CPU per minute of audio (this machine, one core) |
|---|---|---|
| Seedbox 1: Rubber Band R3 + pilot track | as measured by the pilot | 4.2 s at a constant ratio, 13 s on a ramp |
| Signalsmith Stretch, this engine | within 1 ms, from the plan alone | 1.0 s at a constant ratio, 1.2 s on a ramp |

Rubber Band R3 was the other candidate. It can't follow a map from the plan through either binding: `pylibrb`'s
`set_keyframe_map` can't be called (no type caster for its argument), and R3 in real-time mode with the ratio set by
hand drifts 11 to 17 ms over 32 bars at a constant ratio.

The time map comes from the plan: the tempo lane integrated gives each output sample its master beat, and `deck_time`
gives that beat's place in the file. The binding restarts the engine on every call, so a changing ratio plays as
constant-ratio pieces, cut on master beats, no more of them than keep it within 0.5 ms of the map. Each seam is a 20 ms
crossfade ending on the cut. A constant ratio has no seams.

`key_shift` (±2 semitones, per deck) is applied by the engine. With `key_lock` the pitch stays at the file's plus the
shift. Without it the pitch also follows the tempo, as on a turntable. A lone sine lands within 9 cents of the
semitone. Drum stems use 40 ms blocks instead of 120 ms, which cuts pre-echo in the tests. Four stems of one deck cost
about four times the full mix, and `python-stretch` holds the GIL, so threads can't share that out. On one deck, 32 bars
took 1.2 s of CPU as a full mix and 4.3 s as four stems here, so stems are over the 2 s render budget for now.

`python -m seedbox2.eval stretch cpu` measures the CPU table, and `stretch listen` renders the golden pairs with a
tempo gap for `#/listen`, with one Seedbox 1 plan and mixer on both sides so that only the stretcher differs.


`convert.load(raw)` reads a stored plan of either version. Imports keep Seedbox 1's JSON, so user-edited plans
convert when read: lanes, markers, notes and `edited` carry over, and loops off the beat move to the nearest beat with
a note. `frontend2/src/api/mixplan.generated.ts` is generated from the models: after changing `plan.py`, run
`.venv/bin/python -m seedbox2.mix.typescript` (a test fails if you forget).

**Automation shaped by the audio** (#56, `seedbox2/mix/automation.py`): `adapt(plan, audio_a, audio_b)` replaces a
generated plan's constants with values measured from the two tracks, on the plan's own timeline:

- **Masking:** per bar and band, the same index `mixmetrics` reports. The deck not leading has its mids and highs cut only
  as far as it takes to reach `TARGET_MASKING`, and not at all where the tracks don't collide. With stems, a band that is
  mostly one stem has that stem's lane turned down instead, and the report has the index of every pair of stems. The
  lows stay as the technique planned them (a generator's vocal duck too), except that a kill opens where the other deck
  has no lows to clash with, and a hole between A's lows leaving and B's arriving is closed.
- **Gain riding:** the predicted short-term loudness of the sum follows the line between the tracks' levels: down on the
  faders, up on the EQ bands that aren't killed, and back to the plan a bar outside the blend. An offline render then
  corrects it from the loudness measured on the audio (`ride_measured`, up to three passes, dropping one that doesn't help),
  which is what the leveller and the effect returns need; that makes a render about 2.5 times as long.
- **Filter:** every cutoff moves by the track's spectral centroid against `REF_CENTROID_HZ`, within ½ to 2 times.
- **Effects:** the outgoing deck's echo time, feedback, send levels and reverb size come from the tempo and the onsets per
  beat of its last phrase.

The result is ordinary keyframes, and the report goes to the notes. It's a default: `edited` plans come back untouched,
and a deck's `pinned` names the lanes and send settings a person set, which are left alone while the rest fits around
them; `with_pins(new, old)` carries them onto a regenerated plan. `seedbox2/mix/offline.py` renders plan v3 through
Seedbox 1's renderer, playing stem lanes from the cached stems.

On the 22 golden pairs with audio on the owner's machine (`golden compare RUN:seedbox1 RUN:seedbox2`), mean dip -2.56 to
-1.31 LU, bump 1.39 to 1.18 LU, masking low-mid 0.25 to 0.20, high-mid 0.22 to 0.17, high 0.17 to 0.15. Of the 16 blends,
7 hold ±1 LU (Seedbox 1: none). What doesn't: holes a technique makes on purpose (loop rolls, where +6 dB on the EQ
isn't enough with the lows killed), and pairs whose levels differ a lot. A cut isn't ridden. These numbers are without
stems, which the corpus doesn't have cached; metrics don't hear, so this still needs the blind test (`#/listen`).

**Stem-aware transitions** (#53, `seedbox2/mix/stemplan.py`): `plan(generated, a, b)` rewrites a generated plan's blend
on the stem lanes, from what is known of the two tracks (`Track`: stems or not, Camelot key, the vocal stem's phrases and
acapella/instrumental windows, phrase starts, per-stem activity; `track_from_analysis` and `track_from_audio` build one):

- **Bass**: one hand-off on a phrase boundary of B inside the blend, the one nearest to where the generator brought B's
  lows in. A's bass stem goes to 0 over the quarter beat before it and B's comes up over the quarter beat after: silence,
  not an EQ kill. B's lows open on it at the latest and, with stems, A's carry to it. A deck without stems plays the
  bass lane on its lows (the EQ fallback); between two decks without stems the generator's EQ swap stays.
- **Vocals**: where both would sing, one vocal stem is muted for the overlap (B's before the hand-off, A's from it, or
  the other one when that deck has no stems), with a beat's fade. Seedbox 1's -18 dB mid duck is taken out of decks with
  stems; between two decks without stems it stays.
- **Drums**: B's drums come in first under a 250 Hz high-pass up to the hand-off and A's go out last under it, so hats
  layer and the kicks never sound together. Where a deck's lows are killed anyway the filter is left as planned.
- **Mashup moment**: with compatible keys (same, ±1, relative, after `key_shift`), the acapella deck's stems, and the
  windows lined up (B moved by whole 8-bar phrases if that does it), 8 or 16 bars of B's acapella over A's instrumental
  before the hand-off, or A's acapella over B's instrumental after it.

The rules are checks too (`check`, on the plan as it plays): `bass_overlap_db` (the bass both decks play at once against
the larger, at most -20 dB), `vocal_overlap_s` (at most 0.5 s) and `kick_double_s` (at most 0.25 s). The planner applies
them as `forces`, listed in its report, and `enforce` puts them back after `automation.adapt` and every measured gain
pass, so `offline.render_adapted` ends with them holding; its plan's `stems` has the report and the final checks.
`timeline.plan_pair` stores the stem-aware plan, so a set's transitions use the stems once they are cached.

## Ordering a set

**Set ordering** (#60, `seedbox2/sets/`, `#/sets`): puts a set's tracks in order from our own analysis. Tag and tunebat
keys, tempos and energies count as unknown here (the genre tag is the exception: no analysis gives one), so imported
tracks order as well as any once they're analysed.

- **Cost** (`cost.py`, one unit is about one uncomfortable move): Camelot moves, where a clash costs 3 and a track can be
  played a semitone up or down for 1.5 (the shift stays on for the whole track, so a Viterbi pass picks them per order;
  it's a mix plan's `key_shift`); tempo distance, also at half or double time, which barely counts under 5 % and is a
  breach over 8 %; the energy's distance from the arc at each position and each step's from the arc's step; the distance
  from the tempo journey; CLAP distance (end of one track to start of the next, and main to main), relative to this set's
  spread, since CLAP cosines aren't an absolute scale; each run of three vocal-heavy tracks, from #47's `vocals`
  analysis; a change of genre tag, unless the tags share a word. **The weights are a prior: there are no human-ordered
  sets to fit them on yet, and the library on the development machine is empty, so they haven't met real tracks.** The
  tempo and journey weights were picked on random synthetic sets at 120-128 BPM asking for 120 to 128: 1.3 % from the line
  and 3.4 % at most per pair, against 2.0 % and 4.1 % at a third of the journey weight, with the arc's fit about the same. What
  isn't known costs a small fixed amount per pair, or nothing per position, and the page says what's missing.
- **Arcs** (`arcs.py`): `warm_up`, `peak_time` (the default), `sunset`, `rollercoaster`, `flat`, or a curve drawn on the
  page, `[[time, level], ...]`. A level is on the set's own range: 0 is its quietest track and 1 its most intense.
- **Tempo journey:** a geometric drift from `tempo_start` to `tempo_end`. Without them, it runs across the middle of the
  set's tempos (20th to 80th percentile) in the direction the arc goes, and holds the median if the arc comes back.
- **Solver** (`solver.py`): beam search (width 300) from the first track, then local search (reversals, swaps, blocks of
  up to three tracks) with kicks. Up to 8 free tracks are all tried, so that's exact. Pins hold a track at any position,
  and a track that comes back in the set can be pinned at each place. The order as it stands is one of the starts, so the
  result is never worse and a tie keeps it. Steps are counted, not timed, and the random moves have a fixed seed, so a set
  always gets the same order. On a development machine: 30 tracks 0.4 s, 50 tracks 0.6 s, 100 tracks 3.2 s, 200 tracks 10 s
  (random synthetic sets; not measured on the reference laptops). It runs in the server's thread pool, not a worker
  process, and its effect on other requests during a long solve hasn't been measured.
- **Explanations** (`explain.py`): each transition has words ("energy +1, 8A → 9A, same vibe"), flags (`key_clash`,
  `key_fix`, `tempo_wide`, `tempo_over_limit`, `genre_change`, `vocal_run`) and the cost of each term.
- **API:** `GET /api/sets/arcs`; `GET /api/sets/{id}/order` (the set as it stands, explained, and its saved settings);
  `POST` the same path for the best order (saves nothing); `PUT` it with `hashes` to save an order and its settings (the
  `sets.order_settings` column, schema v3).

**The timeline** (#75, `frontend2/src/features/sets/timeline/`, `seedbox2/sets/timeline.py`, `#/sets`): the whole set on one
canvas, tracks overlapping where they mix, with their waveforms and sections, the transitions' regions, the energy, the
target arc, the tempo and the Camelot path.

- **Zoom** is one renderer and one view from the whole set to a few bars (⌘/ctrl + wheel, = − 0). Clicking a transition glides
  to it and opens the transition editor (#79) under the timeline; closing it glides back.
- **Time:** a track plays at its own speed outside its transitions, and both files last the plan's master seconds inside one.
  An arc's time counts tracks, not seconds, as the solver does, so the drawn arc is the one it's asked to follow.
- **Dragging a track** saves the order at once (pins keep their positions). `POST /api/sets/{id}/timeline` then answers with
  each pair's place in its files; a pair with no plan is `pending`, estimated at 16 bars and hatched, and goes to a `set_plan`
  job (`waiting` while a track's beats are still to come, `failed` with why). An order that's only proposed plans too, but
  can't be dragged until it's used or discarded.
- **`set_plan` is a stopgap:** one pair at a time, `musicreco.mixplan.generate` on our beats and energy analysis, with the vocal
  stem's activity when a track has it (`vocals.plan_input_for`). It doesn't use stems or the set around the pair, and
  doesn't run #56's `adapt`: that's #53 and #61. It never touches a pair that has a plan, so edits and imports stand.
- **Playing:** the live engine has two decks, so the set plays as a chain of plans (`playback.ts`), as now-playing does: each is
  stretched over its first track's part of the set, and `engine.schedule(next, carry)` moves on once the outgoing track has
  gone and the tempo has settled on the incoming one's, with the track they share going on from its sample. Unlike
  now-playing's fixed chain it plays from any second of the set and copes with plans still to come: a next plan that isn't
  ready holds the music at the last of this one, and it picks up when it is. Reordering stops the music. A seek before Play
  only moves the playhead: the AudioContext waits for Play.

**A track for a gap** (#64, `seedbox2/sets/bridge.py`, `#/sets`): every transition has a `score` out of 100, from what its two
tracks cost each other (key, tempo, vibe, genre, the energy step; not how they fit the arc or journey). Under 55 it is a
`gap`, and the saved order offers "Find a bridge" there. That catches any clash left in the order and any tempo over the
limit, and leaves out a +2 move with a vibe shift and a genre change, or a pair nothing is known about. A clash a semitone
fixes isn't a gap by itself: the shift counts for 1.5, half on each of the track's transitions. **The threshold comes from
those cases on the cost scale, which is itself a prior: it hasn't met real sets.**

- **Library:** the analysed tracks outside the set, ranked by what their two new transitions would cost: tempo (cheapest
  halfway between the neighbours, half and double time count), the Camelot move of each leg (from the keys the neighbours
  are played at, and the bridge may be shifted a semitone), how far the energy goes outside the neighbours' range, and the
  CLAP distance of each leg, relative to the spread among the candidates. A long intro (30 s or more) takes a little off the
  best 40. Only tracks with both legs cheaper than the gap are shown. A track with no analysed tempo and key isn't a
  candidate; the answer counts them. On a development machine, 5,000 synthetic tracks with embeddings: 0.6 s for the first
  search (it builds the vibe index), then 0.12-0.17 s (not measured on the reference laptops).
- **Tunebat:** "Look on tunebat" asks for the recommendations of both neighbours, using the id Seedbox 1's import kept or
  else a search for the title and artist, which must match. It ranks them the same way on tunebat's tempo and key, with no
  vibe or energy, and leaves out what the library has. A track recommended from both comes first, all else equal. A
  download goes through `TunebatTry` (YouTube, the Imported folder, the importer), and the page searches the library again as
  analyses finish, so the track shows up there once it has a tempo and key. Calls keep to one at a time, 1.2 s apart.
- **Putting one in:** `POST /api/sets/{id}/bridge` `{position, hash, from, to}`. Pins after it move with their tracks; 409 if
  those two aren't neighbours there any more. `GET /api/sets/{id}/bridge?position&limit` and `.../bridge/online` search.

**Long sets** (#62, `seedbox2/sets/longset.py`, `#/crates`): one set from several crates, sets or the whole library, in the
order they're ticked. An imported playlist is a crate, so that's how playlists come in. A saved smart crate isn't a source
yet.

- **Crate order:** each source gets a share of the set's positions, in order, and a track playing outside its share costs
  `Weights.crate_order` x (1 + the positions it's out), in the same cost as everything else. So it's soft: a track leaves
  its crate when that saves more than it costs, and the page says how many did. Switched off, the crates mix freely. A track
  in two sources plays once, for the first, and a track that comes back in a set comes in once.
- **Length:** at most 200 tracks, what the solver takes in about 10 s. Crates and sets stay whole if they fit, and the
  library fills up what's left (60 tracks if no length is asked for, or as many as the crates have if that's more). Sources
  too large for the length are cut to their shares by a greedy walk along the arc and the tempo journey: key, tempo and
  arc for every track, then the embeddings of the best 30 per pick. The solver then orders what was picked.
- **Bridges:** for each ordered pair of sources, the cheapest pair from the last 5 tracks of one to the first 5 of the other,
  by the same key, tempo, vibe and genre costs plus the step in energy, with no semitone fixes. A crate's end is its last
  tracks as it stands, so it says little until the crate has a running order. `good` (2.0 or less) is a prior like the
  weights, not fitted to anything; a clash or a tempo over the limit is `poor`, and a pair without a key or tempo `unknown`.
- **Gaps:** a built set shows #64's scores, and "Find a bridge" runs its library search before the set is saved, leaving out
  the set's own tracks. "Add here" puts one in, and the set is explained again. Tunebat's recommendations need a saved set:
  they're on `#/sets` once it is.
- **Timings:** on a development machine, 5,000 random synthetic tracks with embedding files: whole library to 60 tracks
  0.8 s, to 200 tracks 4 to 7 s; three crates of 50, about 4 s (without embedding files). Not measured on the
  reference laptops, and none of this has met a real library: the crate-order weight is a prior too.
- **API:** `POST /api/sets/build` (`sources`, `max_tracks`, `keep_order`, and the order settings), `/build/explain`,
  `/build/bridge` and `/bridges`. Nothing is saved until the result goes to `POST /api/sets`.

**"Why this transition?"** (`explain.why`): the *Why?* link under each transition says it in sentences, from the numbers the
solver used and the pair's saved mix plan (`service.add_mix_plans`), if it has one. No model writes these, so they work with
the assistant off and can't invent anything.

**The set assistant** (#63, `seedbox2/assistant/`, optional, off by default): "a 90-minute sunset deep-house set, start
mellow, peak at 60 minutes, end on something vocal" becomes a draft on `#/sets`. Everything else in the app works without it
and without a network, and the `anthropic` package isn't imported until a run or a dry run.

- **Settings** (Settings → Set assistant, `config.py`): on or off, the model (`claude-sonnet-5-5` by default) and the API key,
  which goes in the OS keychain through `keyring` and nowhere else. With no keychain there's no key: it's never written to a
  file. `settings.json` only notes that a key was stored, so opening Settings doesn't make the keychain ask for a password.
- **Tools** (`tools.py`): `search_library` (tempo, key, energy, vocals, genre, and a vibe text ranked here by CLAP),
  `choose_tracks`, `set_energy_arc`, `set_tempo_journey`, `lock_track`, `run_solver` (the order and #60's explanation, for
  the model to read and improve on) and `finish`. A bad call goes back to the model as a tool error. A run is limited to 15
  requests and 100 tracks, one runs at a time, and Stop takes effect before the next request.
- **The result** is a draft that saves nothing. With other tracks than the open set's it's kept as a new set (`POST /sets`,
  then `PUT /sets/{id}/order`), with the open set's own tracks it's a proposed order; either way it's then a set like any
  other. The model's reasoning stays with it as `note` in `sets.order_settings`, shown beside the order until dismissed.
- **What is sent** (the privacy note, and `Session.public_track` holds it true): what you type; titles, artists, genre tags;
  length, tempo, key, energy, the share that's sung, and a vibe similarity. Only tracks in the crates or set you ticked (none
  ticked: the whole library). Tracks go under ids local to the run (`t1`), so no content hashes; no audio, embeddings, file
  paths, albums or crate and set names. The SDK's headers about the computer (OS, architecture, Python) are taken out.
  "See what would be sent" (`POST /api/assistant/dry-run`) runs the real client up to the socket and shows the exact URL,
  headers (but the key) and body, without a key or a network.
- **Not measured:** the tests use a mocked client (`tests/test_seedbox2_assistant.py`, also the real SDK over a fake
  transport). No run has yet met the real API with a real library, so the system prompt and the tool descriptions are untuned.

**The renderer** (#69, `seedbox2/mix/render.py`, on `stream.py`): plays a stored plan of either version, holding only
what it plays. A deck decodes the parts of its file the window and its trim need, sample-accurate against a decode of
the whole file: MP3, FLAC, WAV and AIFF seek; M4A, Ogg, Opus and WebM don't land exactly when ffmpeg seeks, so they decode
from their start and throw away what's before the window, which costs time, not memory. Each mixer stage takes float32
chunks and keeps its own state, and lanes stay at control rate. A track's whole-file loudness is measured once and kept
in `track-loudness.json` in the cache folder. It sounds like Seedbox 1's renderer, whose stages it shares (the tests hold
the difference between the two at least 120 dB down). Rubber Band is what's left over budget: it wants the whole deck and
a float64 ratio per sample, all of it in `_stretched`, until the Signalsmith engine (#52) replaces it.

**The master chain** (#58, `seedbox2/mix/master.py`, `frontend2/src/audio/engine/master.ts`): a render and the live engine run
the same causal chain, so they null. Each track gets one gain, the bus gets the chain.

- **Track gain:** `loudness.track_gain_db` puts a track's body at -12 LUFS: the loudest sections (16 s stretches where a track has none)
  that cover a quarter of it, from the stored curve, so no new decode. `GET /api/analysis/{hash}/loudness` returns it as `track_gain_db`, the live
  engine's `trimDb`. Seedbox 1 trims to the bars in the overlap, up to 6 dB over the whole-track trim: a quiet intro is
  boosted, and so is the drop after it. Seedbox 2 renders keep the track's own dynamics. Seedbox 1's renderer is unchanged:
  `render_audio`'s `trims_db` and `master` are what Seedbox 2 passes.
- **Glue:** 2:1, stereo linked, 30 ms attack, 300 ms release, 6 dB knee at -12 dB RMS: its deepest moment averaged about 2 dB on 24 real tracks.
- **Limiter:** ceiling -1.6 dBTP. 4x true peak (24-tap interpolator: 12 taps under-read noise by 0.3 dB and clipped audio by
  1.4), the minimum required over 25 ms, averaged over 25 ms, so 25 ms of latency. It held every fixture and real track within 0.03 dB.
- **Target:** Settings → Master, streaming (-14 LUFS) or club (-9), `GET`/`POST /api/mixes/master`, `master.target` in `settings.json`.
  It only sets the make-up. With nothing measured (`open_loop_makeup_db`), which is how the live engine and transition renders play,
  on 24 real whole tracks: streaming averaged +0.25 LU from the target (sd 0.9), club +0.4 (sd 2.2, with one outlier 5 LU over).
  Club needs 8 dB of make-up on ordinary tracks, so expect about 9 dB of momentary limiting on the deepest peak.
  `find_makeup` measures a render and closes in within 0.1 LU unless that takes over 12 dB of limiting; those mixes stay
  quieter (up to 1.5 LU under -9 on the 8 of those tracks this was run on) and the tracklist says what they reached. `offline.export` returns that make-up,
  and `LiveEngine.setMaster(targetLufs, makeupDb)` plays at the same level.
- **Parity:** with the same make-up, offline and live are within 0.01 LU and null at -69 to -75 dB, with glue and limiter working
  hard or not (`tests/test_seedbox2_engine_parity.py`; the ticket's limit is 0.5 LU).
- **Export:** `offline.export` writes the audio and `<name>.txt`: the tracks with their start times, then the target, integrated
  LUFS, LRA (EBU Tech 3342) and true peak measured on the render, which agree with ffmpeg's `ebur128` on the test
  signals (0.15 LU, 0.3 LU, 0.2 dB). It takes one plan, and nothing in the app calls it: whole sets go through
  `setexport` (#70, below), which feeds the chain chunk by chunk (`Master.process`, `flush`).

**Exporting a set** (#70, `seedbox2/mix/setchain.py`, `setrender.py`, `setexport.py`, `seedbox2/sets/export.py`, the panel under the running order in `#/sets`): the whole set as one continuous mix in WAV (24-bit), FLAC (24-bit), MP3 320 or AAC 256, with chapters in M4A, MP3 (ID3 CHAP) and FLAC, and `<name>.txt` and `<name>.cue` beside it holding the timestamps and the loudness measured on the file. Mixes go to `Seedbox Mixes` next to the music folder, not in it: the music folder is watched, and a mix would come back as a track.

- **In the app, a set plays on the live engine** (`SetDriver`, `SetPlayback`). The export only ever makes a file, and `POST /api/sets/{id}/export` is its only route.
- **The tempo line:** a plan's window ends part way through its drift to B's own tempo, and the next plan starts from that tempo, so a chain of plans used to step at every handoff (124.3 to 126 BPM on a generated pair), and went back to each track's own tempo when both its transitions meet well away from it. `setchain.tempo_line` goes straight from one blend's end to the next one's start, over the track's file time, so both plans read the same tempo at the same place in the track. Blends keep their tempo, and an edited plan keeps every keyframe in its window. `planChain` in `session.ts` is the same code, held to it by `setchain.fixture.json`. Only the clock changes: which beat is where in a file doesn't depend on it.
- **The renderer:** `SetRender.step` gives the next chunk of the bus. A plan's stretch of the set is a `_Segment`; each deck decodes and stretches 64 beats at a time (`_Blocks`), cut on a master beat and away from deck effects, with a 20 ms crossfade ending on the cut. At a constant 1:1 those seams cross-fade identical samples and null. A deck with stems plays them only in the blocks where a stem lane isn't at 1, not for four files' worth of the whole track. Where one plan takes over from the next, the two strips are the same place in the same file and cross-fade over 20 ms; sends ring on for 8 s and fade. Against Seedbox 1's mixer on a plan with no stretching or loops, it is within a sample's rounding of the deck's start.
- **The master:** the bus goes to a spool on disk (raw float32, 1.3 GB per hour, in the cache folder; the job checks the disk first). `setexport.find_makeup` runs the chain over it until it meets the target, as `master.find_makeup` does on a whole array, then a last pass goes to ffmpeg with `master.Meter` listening. `.part` gets the file's name once ffmpeg has finished.
- **Job:** `set_export` on the heavy pool, with the bar and time left in `job.progress`'s message, and `POST /api/jobs/{id}/cancel`, which also deletes the spool.
- **Resuming:** a checkpoint per track pickles the render in the track's body, after the last transition's tails have gone and before this one's reverb exists, because pedalboard's reverb can't pickle. A job interrupted by quitting runs again at the next start. A job whose worker died is marked failed, and exporting the set again picks up there, in any format. The tests hold a resumed bus to be bit-for-bit the uninterrupted one. The spool's folder is a hash of the plans as played and the tracks, so an edited plan starts over.
- **Measured** (`python -m tests.measure_set_export --hours 2 --stems`, synthetic tracks as 192k MP3s, every transition's bass stem moving, on a 12-thread desktop, not yet the reference laptops): a 2.17-hour set of 28 tracks peaked at **435 MB RSS** (the limit is 1 GB) with a 2.76 GB spool, and took 183 s to render, 207 s to find the make-up and 54 s to encode. Its make-up stopped at -14.6 LUFS, under the -14 target, at the 12 dB limiter cap, because this synthetic set is that dynamic (LRA 11.5 LU). **Real music and the reference laptops are still to be measured.**

The glue replaces Seedbox 1's leveller, so `automation` predicts it instead. The golden numbers under "Automation shaped by the
audio" above were measured before this and need a new run on the owner's machine.

## Evaluating the analysis

`seedbox2/eval/` (#50) scores analysers against labelled real audio, so every analysis ticket has a number to beat.
It needs `mir_eval` (in `requirements-dev.txt`).

```sh
.venv/bin/python -m seedbox2.eval fetch giantsteps-tempo giantsteps-key --limit 50   # annotations + previews
.venv/bin/python -m seedbox2.eval analysis                    # every dataset found, a table per dataset
.venv/bin/python -m seedbox2.eval analysis --dataset giantsteps-tempo --analyser seedbox1 --analyser <yours>
```

- **Datasets:** GiantSteps Tempo and Key (EDM previews, downloaded and md5-checked), the Harmonix Set (beats,
  downbeats, sections; bring your own audio as `audio/<id>.<ext>` or `$SEEDBOX2_HARMONIX_AUDIO`) and our own corpus,
  `$SEEDBOX2_CORPUS`: audio files with a `<name>.json` annotation next to them (the format is in
  `seedbox2/eval/annotate.py`) and GTZAN (1,000 30-second excerpts of 10 genres with the beat and downbeat
  annotations of Marchand & Peeters; `fetch gtzan` downloads the 1.2 GB archive; Beat This! wasn't trained on it).
  They live in `$SEEDBOX2_EVAL_DATA` (default: `eval-data` in the cache folder);
  `--dataset NAME=FOLDER` points at another copy.
- **Annotating:** `python -m seedbox2.eval annotate song.mp3 [--rekordbox out.xml]` writes `song.json` from an
  analysis; correct it by hand, or in Rekordbox (import the XML, fix the grid and memory cues, export the collection,
  `python -m seedbox2.eval import-rekordbox exported.xml --verified`).
- **Metrics** (points, 0-100): beat and downbeat F-measure (±70 ms), tempo Accuracy1 (±4 %) and Accuracy2 (octave
  errors allowed) and the octave-error rate, MIREX weighted key score, section boundary hit rate at ±1 bar, vocal
  frame F-measure.
- **Analysers** register in `seedbox2/eval/analysers.py`; `seedbox2` and `seedbox2-small` are the beats analysis
  (#43) with final0 and small0 and the key (#45) of the mix; `seedbox2-stems` takes the key from the harmonic stems
  (minutes per track); `seedbox2-sections` adds the sections from the stems (#46), keeping each track's beats
  and stems in `$SEEDBOX2_EVAL_WORK`; `seedbox1` (`musicreco/structure.py` without the tunebat
  BPM hint) is the baseline. It has no key detection, and finds the tempo of 59 % of the first 50 GiantSteps tracks
  (it reads most drum & bass at 2/3 of its tempo).
- **Sections** (#46): `seedbox2-sections` scored 47.9 against Seedbox 1's 34.4 on `boundary_f`, over 37 Harmonix tracks
  (42.2 against 29.7 on the 18 of them not used for tuning). The annotations were shifted onto our own audio copies, and
  copies of a different edit were left out; with so few tracks the difference has a wide interval (held out: +2 to +23).
- **Runs** are saved as JSON (`$SEEDBOX2_EVAL_RESULTS`, default `eval-results` in the cache folder) with every
  track's scores. `--baseline FILE --max-drop 2` exits 1 when a metric is more than 2 points worse. The nightly
  workflow (`.github/workflows/eval-nightly.yml`) does that on the first 50 GiantSteps tracks against
  `seedbox2/eval/baseline.json`: an analysis ticket that improves a number refreshes it with `--write-baseline`.

**The live engine** (#65, `frontend2/src/audio/engine/`): plays a plan in the browser with no server render.
`new LiveEngine()`, then `load(deck, { hash, trimDb })`, `schedule(plan)`, `play(when?)`, `pause()`, `seek(beat)`,
`setLoop(startBeat, endBeat)`, `scrub(beat)` / `endScrub()`, and `beat()` / `seconds()` for playheads (`index.ts` has the whole API). One AudioWorklet
(`processor.ts`) runs `core.ts`: both decks' graphs and the master, on one sample clock. A deck is four stems or the full
track -> per-stem gain -> trim -> FX -> LR4 isolator -> filter -> fader, with echo and reverb sends. The core has no browser
in it, so the same code runs under deno for the parity test: `tests/test_seedbox2_engine_parity.py` nulls it against
`musicreco/mixrender.py` on blends, EQ, filter sweeps, loops, effects, sends and varispeed (-69 to -75 dB without stretch,
against a limit of -40 dB; -42 dB in varispeed). The master chain nulls as well (#58, under "The master chain"). When changing either engine's DSP, change both: `constants.ts` lists what they share.

Audio comes in 20 s chunks: `GET /api/tracks/{hash}/audio/{source}/{index}`, FLAC at 44.1 kHz, cut so that they're a whole
decode sample for sample. `loader.ts` fetches what the plan plays over the next 25 s and evicts what it doesn't need, with
at most 200 MB decoded in the page, whether held or being decoded. A deck whose track has no stems, or loses them to the
cache's eviction, plays the full track with its stem lanes folded into its EQ (`fold.ts`, the same result as
`validate(has_stems=...)`).

What it doesn't do yet:
- **The renderer's master (#58):** its leveller and limiter look ahead without limit. Live, the leveller is the same law
  reacting about a second late, and the limiter has 25 ms of latency and works on sample peaks. They only act on loud
  blends; below them, the two engines null.
- **Trim:** the caller passes each deck's `trimDb`; nothing computes it in the page yet (0 dB by default).
- **Screens:** `#/mixes` (#67) and the now-playing view (#77) play through it; #79 comes next, and `audio/player.ts` stays for the library's single tracks.

`python -m seedbox2.bench engine` measures it in headless Chrome (below).

**Key lock and `key_shift`** (#66, `stretcher.ts`, `stretched.ts`): the renderer's engine, Signalsmith Stretch, as the official npm package's WebAssembly, run inside the engine's own worklet rather than through the package's AudioWorklet node, so that it stays on the one sample clock. `scripts/vendor-stretch.ts` copies the WebAssembly out of the pinned package, and a test holds the copy to it. The rules are the renderer's: with `key_lock` a deck off its own tempo stretches and keeps its pitch plus `key_shift`; without it the pitch follows the tempo (plain varispeed, or the stretcher when there's a `key_shift` too). On its own tempo with no shift a deck is still a copy, and a deck stretched anywhere goes through the stretcher for its whole length.

- **Timing:** each block feeds the stretcher up to where the plan's time map will be when that block's sound comes out, so the ratio follows the tempo lane as a real ramp, with no pieces or seams. Rounding is taken back on the next block, so nothing adds up: over 64 bars of a ramp from 124 to 128 BPM the worst beat is 0.46 ms from the master grid and the drift, first bars to last, is 0.01 ms (`stretcher.test.ts`). The renderer's stretch and this land within 0.2 ms of each other (`test_key_lock_sounds_like_the_renderers_stretch`).
- **Latency:** a plan is known in advance, so the source is read ahead by the stretcher's own latency and nothing comes out late: stretching adds no output latency. The price is a jump (a seek, a loop's wrap): the stretcher starts again from a seek with one block and one interval of output worked out unheard, all in one audio block. Loop wraps and seeks do that under their closed fades. It reads the source from about 0.15 s before where it lands, which isn't always loaded yet.
- **What isn't the renderer's:** the renderer runs a stretcher per stem, and drums get shorter blocks. Live there's one per deck, with the stems mixed before it, each under its gain at the moment that sample will be heard; four per deck would use up the budget. An edit to a stem's gain is heard about 0.15 s later for that reason, and a kick is a little softer than in a render. A scrub's 40 ms bursts use the varispeed read, so they stay as cheap as they were, off pitch.
- **Effects** read back what the stretcher has already put out, from a history as long as the deck's longest effect needs.
- **Low-power mode** uses a cheaper setting: 100 ms blocks every 45 ms, where the normal setting is 120 ms every 30 ms. `store/power.ts` sets it, and coming on during playback it's a change of graph like any other, faded over 5 ms.

| `python -m seedbox2.bench engine`, 2 decks x 4 stems, every stage on | One core | Slowest block (2.9 ms of audio) | Slowest around a seek |
|---|---|---|---|
| On tempo (a copy) | 2.0% | 0.7 ms | 0.2 ms |
| Varispeed | 2.1% | 0.3 ms | 0.5 ms |
| Key lock, both decks stretched | 4.2% | 1.5 ms | 4.1 ms |
| Key lock, low-power mode | 3.2% | 1.3 ms | 2.4 ms |

Measured on a Ryzen 5 9600X desktop on mains in headless Chromium. **Not yet measured on the reference laptops**, against the 15% budget. A seek is the number to watch there: starting two stretchers takes longer than a block's 2.9 ms even here, so it leans on the output buffer. A stretcher adds 1.2 MB of memory, and taking a new one costs about 3 ms, on the audio thread, when a plan needs more than the pool has.

**Editing while it plays** (#67). `schedule(plan)` can come at any time. If only lanes' keyframes moved, the running voices fade each changed lane to its new values over 5 ms (`EDIT_FADE`) and keep their filters and tails, and it costs next to nothing, so a drag can send one per pointer event. Any other change (tempo, ranges, FX, an isolator or a send switching on) builds a new graph, settles its filters unheard and fades to it over the same 5 ms, handing over echo and reverb tails where the sends match. A plan that arrives during a fade waits and only the newest is kept; the position and the loop region carry over in beats. After the fade the output nulls against a fresh run of the new plan (`engine.test.ts`).

`scrub(beat)` / `endScrub()` play 40 ms bursts forward from the pointer with 3 ms fades, on one graph, so scrubbing costs what playing costs. It goes quiet when the pointer rests, and playing carries on from where it lets go if it was playing. A/B (`features/mix/session.ts`) is just `schedule` of the other plan.

`#/mixes` is the first screen on all this: a saved plan with scrubbing on the waveforms, a loop selection on the ruler, "loop the transition" and A/B. The library keeps one plan per pair and nothing generates auto plans until #53, so side A is "Original" for an imported plan. `trimDb` is 0 there because the library has no loudness to work it out from yet.

**The transition editor** (#79, `TransitionEditor` in `frontend2/src/features/mix/`) is that screen at three levels, and the choice stays in localStorage:

- **Simple** (`styles.ts`): a style, a length and two amounts, how smooth and how much FX, write every lane of the plan in the browser, and a coloured ribbon shows the result. The styles are a table of recipes after Seedbox 1's techniques, until a generator (#53) and styles as data (#57) replace it. A deck with stems hands over on its stem lanes, one without on its EQ.
- **Advanced** (`LaneStack.tsx`): the fader and four stem rows per deck, each stem's curve drawn on its own waveform, which dims as the curve turns it down. A deck whose track has no stems gets EQ rows on its band waveforms instead.
- **Manual:** a row for every lane of both decks. Drag a keyframe, double-click to add one, right-click to remove it, Alt-click to change its curve, or click it to set its numbers.

Every edit goes straight to `schedule`, so it's heard as the pointer moves. A drag or a held slider is one step of undo (Ctrl+Z, Ctrl+Shift+Z; `session.ts`). An edit is saved 0.6 s after the last change: `PUT /api/mixes/plans/{from}/{to}` refuses anything `validate` refuses, and `plans.auto_plan` keeps the plan the edits started from. "Reset to auto" goes back to it (`POST .../reset`), and undo can bring the edit back.

**The checks** (`checks.ts`) are bass overlap, vocal overlap, key and tempo stretch, and loudness dip and bump, estimated from the plan and the waveforms after every edit, because nothing has been rendered. They use `mixmetrics.checks`' limits, but they're estimates: the levels come from waveform peaks rather than measured loudness, so an exported render's `mixmetrics` still has the last word. Vocal overlap needs both tracks' stems. A 128-bar overlap takes about 5 ms to check in Node, and a fix about 10 ms.

Each Fix button is one undoable edit: bass and vocals hand over on one bar in the middle of where they clash (a person's lanes are pinned as usual), loudness rides the faders and then the mids and highs that aren't killed, and key and tempo shifts B by up to 2 semitones and meets the two tempos halfway.

**The server renders for export only** (#70, under "Exporting a set"). Nothing under `/api` renders audio to listen to: `tests/test_seedbox2_library.py` holds that, and playing or auditioning a plan is the live engine's job. The listening page (`#/listen`) plays files rendered offline, and `python -m seedbox2.eval` renders for its reports.

**Mix metrics** (#54): `seedbox2/eval/mixmetrics.py` measures a rendered transition from its audio: beat alignment
between the decks (ms), bass overlap below 120 Hz (s, dB·s), vocal overlap (s), the short-term loudness dip/bump
against the tracks (LU), true peak and clipped samples, a spectral masking index per band, and the key distance and
stretch actually used; `checks()` grades them pass/warn/fail for the transition editor (`frontend2/src/features/mix`,
shown on `#/ui`). Compare engines over a set of pairs:

```sh
.venv/bin/python -m seedbox2.eval mixes                                  # built-in synthetic pairs
.venv/bin/python -m seedbox2.eval mixes pairs.toml --json metrics.json   # your own pairs (format: seedbox2/eval/mixes.py)
```

The `seedbox2` column is Seedbox 1's plan with its automation fitted to the audio (see Mix plans); it plans and stretches
on its own once #52 and #53 land. An engine should call `mixmetrics.measure` on every render. Without stems (#44, #47) bass overlap uses a low-pass of each deck and vocal overlap a tonal voice-band
proxy.

**The blind listening test** (#86, `seedbox2/eval/listening.py`, `seedbox2/server/routers/listen.py`,
`frontend2/src/features/listen/`): metrics don't hear, so `#/listen` lets people rate transitions. The page is hidden:
nothing links to it, so open the address yourself. It plays two renders of one pair in random
order, as A and B, and the rater says which is better, with optional 1–5 scores (smoothness, energy, "would a DJ do
this") for each and a note. Press `A` and `B` to switch at the same moment in the track. The client only gets an opaque trial id,
so neither the page nor its requests name what is playing; the answer goes in the `ratings` table with what A and B really were.

Renders are files, so a human DJ's version is just dropped in:
`<support folder>/listening/<pair>/<version>[--<style>].<wav|mp3|m4a|aac|flac|ogg|opus>`, for example
`human--DJ Someone.mp3`. To render them with our engines:

```sh
.venv/bin/python -m seedbox2.eval listen                                      # built-in synthetic pairs
.venv/bin/python -m seedbox2.eval listen pairs.toml --techniques "bass swap,echo out"   # alternative plans too
```

The Results tab (`#/listen?tab=results`) has win rates with Wilson 95% intervals per engine version and per style, and
each two versions head to head. A trial only counts in a table when its two renders differ there. To pool results, Export
writes a ratings file to the listening folder (ratings only, no audio) and Import merges one; ratings have a uid, so
importing a file twice adds nothing. Everyone rating needs the same renders: share the folder itself. Levels aren't
matched between renders, so a louder one may be favoured.

**The golden corpus** (#87): `seedbox2/eval/golden.toml` is 44 real track pairs across house, techno, drum & bass,
hip-hop, pop, disco/funk with live drums and halftime, with tempo gaps of 0-8 %, key clashes and vocal-heavy pairs. Only
hashes and metadata are committed. The audio stays on the machine and is found by its library hash, wherever the file is
and whatever it is called: most of it is the owner's library, the rest (each with a `source` URL) is free to download.

```sh
export SEEDBOX2_GOLDEN_AUDIO=/mnt/media/MUSIC:$HOME/golden-free       # folders with the audio (or --audio DIR)
.venv/bin/python -m seedbox2.eval golden check                        # the corpus's coverage; which audio is here
.venv/bin/python -m seedbox2.eval golden                              # render everything, write the report
.venv/bin/python -m seedbox2.eval golden --only dnb --only key-clash  # by tag or pair id
.venv/bin/python -m seedbox2.eval golden compare OLD_RUN NEW_RUN      # side by side; exits 1 on regressions
.venv/bin/python -m seedbox2.eval golden compare RUN:seedbox1 RUN:seedbox2
```

- **A run** is a folder in `$SEEDBOX2_EVAL_RESULTS/golden/` with `run.json`, the renders and `report.html`: a player,
  the waveforms of both decks and the mix (click to seek, overlap shaded), the plan's automation and notes, the checks
  and the metrics for each pair. 44 pairs take about 2 minutes at the default jobs on a 12-thread machine. A pair
  whose audio isn't here is reported as not rendered.
- **Comparing:** to compare old code, render it from a `git worktree` of that commit and compare the two runs. Every
  metric from `mixmetrics` has a tolerance (`METRICS` in `golden.py`), and half of it applies to the mean over all pairs,
  so slow drift shows too. Also flagged: a check that got worse, a pair that fails or no longer renders. A metric
  that stops applying isn't (a cut has no beat alignment), so also read the technique in the report when a pair changes.
  Render time is recorded but not flagged, as it depends on what else the machine is doing.
- **Nightly:** `seedbox2/eval/golden-nightly.sh` renders origin/main in its own worktree, compares it with the accepted
  baseline (the first run; `golden accept RUN` moves it, after a change you've listened to and want to keep), posts the
  summary to a results ticket on the board and keeps the last 14 runs. How to schedule it is at the top of the script.
- **Adding tracks:** `golden track FILE...` prints the `[[track]]` entries; give them a genre and add `[[pair]]`s. Pair
  ids must stay stable, as comparisons go by id.

## Architecture (target)

| Part | Where | What |
|---|---|---|
| Launcher and window | `seedbox2/app.py`, `seedbox2/shell.py` | pywebview: Cocoa on macOS, WebView2 on Windows, GTK/Qt on Linux; folders from `platformdirs` (#36, #37) |
| Server | `seedbox2/server/` | FastAPI on 127.0.0.1, one router per area, one event stream (#36) |
| Library | `seedbox2/library/` | SQLite (WAL), keyed by a content hash of the decoded audio, so analysis survives retagging and renames; imports Seedbox 1 data (#38). Local folders and Rekordbox/iTunes XML, with tunebat/YouTube as one source among others (#40) |
| Jobs | `seedbox2/jobs/` | Worker processes, not threads: one *heavy* worker (stems, renders) and *light* workers (beats, key, waveforms, embeddings). Persisted, prioritised, cancellable, with a RAM guard (#39) |
| Models | `seedbox2/models/` | onnxruntime only (no torch in the app). Weights are downloaded on first run, checksummed and listed with their licences (#42) |
| Analysis | `seedbox2/analysis/` | beat_this beats and downbeats with a piecewise grid; HTDemucs stems; our own key detection; sections and phrases from the stems; vocal activity; energy; CLAP embeddings; 150 points/s binary waveforms (#43–#49), measured by #50 |
| Mix engine | `seedbox2/mix/` | One plan schema with per-stem lanes and piecewise grids (#51). The offline renderer and the live engine execute the same plan. Stem-aware transitions, learnt cue points, audio-shaped automation, styles as data, master chain (#52–#59) |
| Set planning | `seedbox2/sets/`, `seedbox2/assistant/` | Ordering on our own analysis with energy arcs and lookahead; multi-crate sets; an optional Claude-powered assistant (#60–#64) |
| UI | `frontend2/` | React 19 + Vite + Tailwind 4 + zustand (forked from `frontend/`). Design language v2 (#71). Performance rules in `frontend2/PERFORMANCE.md`: no React render per frame, narrow selectors, virtualised lists, waveform data in a worker (#84). WebGL2 waveforms (#74). A WebAudio AudioWorklet engine that plays plans live (#65–#67) |
| Evaluation | `seedbox2/eval/` | `python -m seedbox2.eval analysis/mixes/golden` and the `#/listen` blind A/B page (#50, #54, #86, #87) |
| Benchmarks | `seedbox2/bench/` | `python -m seedbox2.bench` checks the budgets below, nightly in CI and on real laptops ([how](bench/README.md)) (#68) |

Seedbox 1's `musicreco` stays importable: tunebat, YouTube and tagging are reused, not forked.

## Reference laptop and budgets

Two reference machines, both measured on battery:
- An 8 GB M1 MacBook Air.
- An 8 GB 4-core x86 laptop with integrated graphics (an i5-1135G7 class machine), on Windows.

There is no GPU requirement and no CUDA.

| What | Budget |
|---|---|
| App start | < 3 s |
| UI | 60 fps while scrolling, zooming and playing; no long tasks > 50 ms |
| Beats + key + waveform | < 15 s per 4-minute track |
| Stems (background) | ≤ 60 s per track (M1), ≤ 120 s (x86) |
| Plan one transition | < 500 ms |
| Render a 32-bar transition | < 2 s, < 300 MB |
| Live engine, 2 decks x 4 stems | < 15% of one core, ~200 MB decoded |
| Whole app peak RSS | < 2.5 GB |
| Installer (without models) | < 250 MB |

## Research behind the plan

Detailed findings, with numbers and sources, are in each epic's and ticket's description. The headlines:
- **Beats:** beat_this (CPJKU, ISMIR 2024, MIT) is state of the art and takes about 5 s per track on a laptop CPU, with ONNX ports available.
- **Stems:** HTDemucs (MIT) is about 9 dB SDR. Mixxx's 2025 ONNX export runs at an RTF of about 0.2–0.35 on CPU. djay and Serato also separate when a track loads and play from cached stems.
- **Key:** Faraldo's EDM key profiles, run on the harmonic stems. Pitch and tempo changes stay small, since real DJs transpose 2.5% of tracks (94% of those by one semitone) and keep 86% of tempo changes under 5% (Kim et al., ISMIR 2020).
- **Cue points:** transitions cluster at 8, 16 and 32 bars, and DJs agree on cue points within 8 bars 74% of the time (Kim et al. 2020). That figure is the target for learnt cue points (#55).
- **Transitions:** what listeners dislike most is vocal over vocal and rushed transitions (DJtransGAN, ICASSP 2022). The stems are what make "never two basslines, never two lead vocals" enforceable (#53).
- **Waveforms:** Rekordbox stores 150 entries/s plus a 1,200-column overview. Mixxx uses Bessel 600/4000 Hz band splits and draws in GLSL at refresh rate (Mixxx 2.4).

## Licences

Decided in #35: **personal use only.** Seedbox is not sold or distributed, and the repository is private. So
licences don't limit the choice of components: pick whatever sounds, measures or runs best. That includes:
- GPL and AGPL code: pedalboard, Rubber Band R3, libKeyFinder, Essentia.
- Models and weights with non-commercial terms: MERT, MuQ, the madmom weights and the Essentia models.

Verified licences of the candidates (2026-10-29), for the model list (#42) and in case this ever changes:

| Component | Code | Weights |
|---|---|---|
| beat_this | MIT | MIT |
| HTDemucs | MIT | no licence given (upstream: "likely CC-NC-BY") |
| LAION CLAP `larger_clap_music`, `larger_clap_music_and_speech`, `clap-htsat-fused` | CC0-1.0 | Apache-2.0 |
| MERT, MuQ | Apache-2.0, MIT | CC BY-NC 4.0 |
| madmom | BSD-2 | CC BY-NC-SA 4.0 |
| Essentia | AGPL-3.0 | CC BY-NC-SA 4.0 (proprietary licence on request) |
| Signalsmith Stretch (`python-stretch`, and its npm WebAssembly in the live engine), onnxruntime | MIT | |
| pedalboard (bundles JUCE, Rubber Band, FFTW) | GPLv3 | |
| Rubber Band R3 | GPL-2.0-or-later or paid commercial licence | |
| libKeyFinder | GPL-3.0-or-later | |

Seedbox 1 is GPL through pedalboard, mutagen (GPL-2.0-or-later) and the ffmpeg binary in imageio-ffmpeg (a GPLv3
build).

Attribution: keep each model's licence and credit next to it in the model list (#42), and cite the papers (beat_this,
HTDemucs, CLAP, MERT, MuQ) in this README when they are used. CC BY and Apache only ask for that once the work is
shared.

If Seedbox is ever shared, this decision has to be made again. Sharing it as GPLv3 would keep everything except the
non-commercial weights and the unlicensed HTDemucs weights. Selling or closing it would also rule out GPL code and
Rubber Band (unless a commercial licence is bought).

## Roadmap (dependency waves)

Each wave only depends on earlier waves. Tickets wait in Backlog; move them to Todo to start them.

1. #35 decide the licence posture for models and DSP libraries, #36 own server package and frontend2 app shell (stop delegating to Seedbox 1)
2. #37 cross-platform data folders, paths and window (Windows, Intel Mac, Linux), #38 SQLite library keyed by audio content hash, #41 CI on macOS, Windows and Linux, #42 onnxruntime model manager (download on first run, checksums, versions), #50 analysis evaluation harness on labelled real audio, #54 objective mix metrics on every render, #68 performance budgets and a benchmark harness, #71 design language v2 (motion, sizes, colour roles) in DESIGN2.md and tokens, #84 frontend performance (no per-frame React renders, virtualised lists, workers)
3. #39 process-pool job system with persisted state, priorities and cancel, #43 beats and downbeats with beat_this + a piecewise beatgrid, #49 binary waveform data at 150 points/s with Bessel band split, #72 minimal app shell with a command palette and panel motion, #81 visual regression tests for every route, #86 blind A/B listening test page, #87 golden corpus of real track pairs and render regression, #88 installers for macOS (arm64 + Intel) and Windows
4. #40 import local music (folders, drag and drop, Rekordbox/iTunes XML), #44 stem separation with HTDemucs on the CPU, cached, #51 mix plan model v2 with stem lanes and piecewise grids, #74 WebGL2 waveform renderer (RGB and 3-band, 60 fps), #85 low-power mode and polite background work, #89 self-update and a support bundle
5. #45 our own key detection (no more tunebat keys), #46 phrases and sections from stems (intro, build, drop, breakdown, outro), #47 vocal activity and vocal phrases from the vocal stem, #48 energy curve, loudness and CLAP embeddings for the vibe, #52 time-stretch and pitch-shift engine (key lock, ±1 semitone), #56 automation shaped by the audio (masking-aware EQ, gain and filter), #65 real-time AudioWorklet engine that plays mix plans live, #69 streaming decode and chunked DSP (no whole tracks in RAM), #73 library view (virtualised table, smart filters, analysis status), #78 stem waveforms (four lanes or one stacked colour view), #80 first-run onboarding, model download and empty states, #83 one waveform style for preview clips too
6. #53 stem-aware transitions (bass hand-off, vocal hand-off, no clashes), #55 cue points and transition lengths learnt from real DJ mixes, #58 master chain and loudness for continuous mixes, #60 set ordering on our own analysis with energy arcs, #66 key-locked tempo in the browser (Signalsmith Stretch WASM), #67 edit while it plays (live lanes, scrubbing, loop a transition), #73 centred scrolling waveform with a frame-accurate playhead, #82 waveform overlays (beatgrid, phrase bands, cues, sections)
7. #57 genre-aware transition styles as data, #59 research spike: learn EQ/fader curves from real DJ mixes, #62 long sets across crates and the whole library, #63 natural-language set assistant (Claude API, optional), #64 suggest tracks that fit a gap in the set, #70 render and export the whole set as one continuous mix, #75 whole-set timeline view with the energy arc, #77 live "now playing" view with two scrolling decks, #79 transition editor v2 (stem lanes, simple first)
8. #61 plan transitions across the whole set with lookahead
