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
- `--self-test`: JSON diagnostics: ffmpeg/deno, imports, and the server answering `/api/health` and serving `frontend2/dist`.
- `--version`

Tests: `.venv/bin/python -m pytest tests/test_seedbox2.py tests/test_seedbox2_eval.py tests/test_bench.py tests/test_seedbox2_library.py tests/test_seedbox2_jobs.py tests/test_seedbox2_power.py tests/test_seedbox2_waveform.py tests/test_seedbox2_import.py tests/test_seedbox2_mixmetrics.py tests/test_seedbox2_golden.py tests/test_seedbox2_models.py tests/test_seedbox2_library_view.py tests/test_seedbox2_stems.py tests/test_seedbox2_beats.py tests/test_seedbox2_listen.py tests/test_seedbox2_mix_plan.py tests/test_seedbox2_engine_parity.py tests/test_seedbox2_engine_chunks.py tests/test_seedbox2_engine_browser.py tests/test_seedbox2_automation.py tests/test_seedbox2_onboarding.py`, and in `frontend2/` `deno task build`, `deno task test`, `deno task lint` and `deno task screenshot` (Docker; see `frontend2/DESIGN.md`).

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
nothing shows a real plan yet: the now-playing view (#77) and transition editor (#79) mount it with the engine's
clock (#65), and `#/ui` plays a synthetic bass hand-off.

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
  (minutes per track); `seedbox1` (`musicreco/structure.py` without the tunebat
  BPM hint) is the baseline. It has no key detection, and finds the tempo of 59 % of the first 50 GiantSteps tracks
  (it reads most drum & bass at 2/3 of its tempo).
- **Runs** are saved as JSON (`$SEEDBOX2_EVAL_RESULTS`, default `eval-results` in the cache folder) with every
  track's scores. `--baseline FILE --max-drop 2` exits 1 when a metric is more than 2 points worse. The nightly
  workflow (`.github/workflows/eval-nightly.yml`) does that on the first 50 GiantSteps tracks against
  `seedbox2/eval/baseline.json`: an analysis ticket that improves a number refreshes it with `--write-baseline`.

**The live engine** (#65, `frontend2/src/audio/engine/`): plays a plan in the browser with no server render.
`new LiveEngine()`, then `load(deck, { hash, trimDb })`, `schedule(plan)`, `play(when?)`, `pause()`, `seek(beat)`,
`setLoop(startBeat, endBeat)`, and `beat()` / `seconds()` for playheads (`index.ts` has the whole API). One AudioWorklet
(`processor.ts`) runs `core.ts`: both decks' graphs and the master, on one sample clock. A deck is four stems or the full
track -> per-stem gain -> trim -> FX -> LR4 isolator -> filter -> fader, with echo and reverb sends. The core has no browser
in it, so the same code runs under deno for the parity test: `tests/test_seedbox2_engine_parity.py` nulls it against
`musicreco/mixrender.py` on blends, EQ, filter sweeps, loops, effects, sends and varispeed (-69 to -75 dB without stretch,
against a limit of -40 dB; -42 dB in varispeed). When changing either engine's DSP, change both: `constants.ts` lists what they share.

Audio comes in 20 s chunks: `GET /api/tracks/{hash}/audio/{source}/{index}`, FLAC at 44.1 kHz, cut so that they're a whole
decode sample for sample. `loader.ts` fetches what the plan plays over the next 25 s and evicts what it doesn't need, with
at most 200 MB decoded in the page, whether held or being decoded. A deck whose track has no stems, or loses them to the
cache's eviction, plays the full track with its stem lanes folded into its EQ (`fold.ts`, the same result as
`validate(has_stems=...)`).

What it doesn't do yet:
- **Key lock and `key_shift` (#66):** off its own tempo a deck plays varispeed, so its pitch moves with the tempo, as a
  render without key lock does.
- **The renderer's master (#58):** its leveller and limiter look ahead without limit. Live, the leveller is the same law
  reacting about a second late, and the limiter has 25 ms of latency and works on sample peaks. They only act on loud
  blends; below them, the two engines null.
- **Trim:** the caller passes each deck's `trimDb`; nothing computes it in the page yet (0 dB by default).
- **Screens:** nothing in the UI plays through it yet; #67 and #77 are its first users, and `audio/player.ts` stays until then.

`python -m seedbox2.bench engine` measures it in headless Chrome (below).

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
| Set planning | `seedbox2/sets/` | Ordering on our own analysis with energy arcs and lookahead; multi-crate sets; an optional Claude-powered assistant (#60–#64) |
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
| LAION CLAP `larger_clap_music`, `clap-htsat-fused` | CC0-1.0 | Apache-2.0 |
| MERT, MuQ | Apache-2.0, MIT | CC BY-NC 4.0 |
| madmom | BSD-2 | CC BY-NC-SA 4.0 |
| Essentia | AGPL-3.0 | CC BY-NC-SA 4.0 (proprietary licence on request) |
| Signalsmith Stretch, onnxruntime | MIT | |
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
