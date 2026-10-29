# Seedbox 2

The next generation of Seedbox. The goal is mixes that sound better than a good human DJ, in the cleanest UI in DJ
software (the waveforms stay), on an ordinary laptop. It was planned in board ticket #34. The work lives in the
`S2 …` epics on the board, tickets #35–#89.

## Run from source

Do the [Setup](../README.md#setup) in the main README first (`.venv`, `ffmpeg` and `deno`).

```sh
(cd frontend2 && deno install && deno task build)   # once, and after frontend changes (writes frontend2/dist)
.venv/bin/python -m seedbox2                        # opens the Seedbox 2 window
```

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

Tests: `.venv/bin/python -m pytest tests/test_seedbox2.py tests/test_seedbox2_eval.py tests/test_bench.py`, and in `frontend2/` `deno task build`, `deno task test` and `deno task lint`.

Seedbox 2 keeps its own settings, caches and logs in `~/Library/{Application Support,Caches,Logs}/Seedbox 2`
(`$SEEDBOX_SUPPORT_DIR`, `$SEEDBOX_CACHE_DIR` and `$SEEDBOX_LOG_DIR` override them), so trying it never touches
Seedbox 1's sets or settings. The music library (`~/Music/MusicReco`) is shared. #37 moves these folders to each
platform's conventions.

**What's there so far** (#36): the server (`seedbox2/server/`: `/api/health`, `/api/system`, the `/api/events`
stream and one empty router per area) and the `frontend2/` app shell (sidebar, hash router, a placeholder page per
area, `#/system`, and `#/ui` with the design tokens and the waveform). The other tickets fill in the areas.

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
  `seedbox2/eval/annotate.py`). They live in `$SEEDBOX2_EVAL_DATA` (default: `eval-data` in the cache folder);
  `--dataset NAME=FOLDER` points at another copy.
- **Annotating:** `python -m seedbox2.eval annotate song.mp3 [--rekordbox out.xml]` writes `song.json` from an
  analysis; correct it by hand, or in Rekordbox (import the XML, fix the grid and memory cues, export the collection,
  `python -m seedbox2.eval import-rekordbox exported.xml --verified`).
- **Metrics** (points, 0-100): beat and downbeat F-measure (±70 ms), tempo Accuracy1 (±4 %) and Accuracy2 (octave
  errors allowed) and the octave-error rate, MIREX weighted key score, section boundary hit rate at ±1 bar, vocal
  frame F-measure.
- **Analysers** register in `seedbox2/eval/analysers.py`; `seedbox1` (`musicreco/structure.py` without the tunebat
  BPM hint) is the baseline. It has no key detection, and finds the tempo of 59 % of the first 50 GiantSteps tracks
  (it reads most drum & bass at 2/3 of its tempo).
- **Runs** are saved as JSON (`$SEEDBOX2_EVAL_RESULTS`, default `eval-results` in the cache folder) with every
  track's scores. `--baseline FILE --max-drop 2` exits 1 when a metric is more than 2 points worse. The nightly
  workflow (`.github/workflows/eval-nightly.yml`) does that on the first 50 GiantSteps tracks against
  `seedbox2/eval/baseline.json`: an analysis ticket that improves a number refreshes it with `--write-baseline`.

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
| UI | `frontend2/` | React 19 + Vite + Tailwind 4 + zustand (forked from `frontend/`). Design language v2 (#71). WebGL2 waveforms (#74). A WebAudio AudioWorklet engine that plays plans live (#65–#67) |
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

Undecided (#35). Models and libraries with non-commercial terms (Essentia models, madmom weights, MERT) are not used
in either case. Pending #35, prefer MIT/Apache/CC BY components: beat_this, HTDemucs, CLAP, onnxruntime, Signalsmith
Stretch.

## Roadmap (dependency waves)

Each wave only depends on earlier waves. Tickets wait in Backlog; move them to Todo to start them.

1. #35 decide the licence posture for models and DSP libraries, #36 own server package and frontend2 app shell (stop delegating to Seedbox 1)
2. #37 cross-platform data folders, paths and window (Windows, Intel Mac, Linux), #38 SQLite library keyed by audio content hash, #41 CI on macOS, Windows and Linux, #42 onnxruntime model manager (download on first run, checksums, versions), #50 analysis evaluation harness on labelled real audio, #54 objective mix metrics on every render, #68 performance budgets and a benchmark harness, #71 design language v2 (motion, sizes, colour roles) in DESIGN2.md and tokens, #84 frontend performance (no per-frame React renders, virtualised lists, workers)
3. #39 process-pool job system with persisted state, priorities and cancel, #43 beats and downbeats with beat_this + a piecewise beatgrid, #49 binary waveform data at 150 points/s with Bessel band split, #72 minimal app shell with a command palette and panel motion, #81 visual regression tests for every route, #86 blind A/B listening test page, #87 golden corpus of real track pairs and render regression, #88 installers for macOS (arm64 + Intel) and Windows
4. #40 import local music (folders, drag and drop, Rekordbox/iTunes XML), #44 stem separation with HTDemucs on the CPU, cached, #51 mix plan model v2 with stem lanes and piecewise grids, #74 WebGL2 waveform renderer (RGB and 3-band, 60 fps), #85 low-power mode and polite background work, #89 self-update and a support bundle
5. #45 our own key detection (no more tunebat keys), #46 phrases and sections from stems (intro, build, drop, breakdown, outro), #47 vocal activity and vocal phrases from the vocal stem, #48 energy curve, loudness and CLAP embeddings for the vibe, #52 time-stretch and pitch-shift engine (key lock, ±1 semitone), #56 automation shaped by the audio (masking-aware EQ, gain and filter), #65 real-time AudioWorklet engine that plays mix plans live, #69 streaming decode and chunked DSP (no whole tracks in RAM), #73 library view (virtualised table, smart filters, analysis status), #78 stem waveforms (four lanes or one stacked colour view), #80 first-run onboarding, model download and empty states, #83 one waveform style for preview clips too
6. #53 stem-aware transitions (bass hand-off, vocal hand-off, no clashes), #55 cue points and transition lengths learnt from real DJ mixes, #58 master chain and loudness for continuous mixes, #60 set ordering on our own analysis with energy arcs, #66 key-locked tempo in the browser (Signalsmith Stretch WASM), #67 edit while it plays (live lanes, scrubbing, loop a transition), #76 centred scrolling waveform with a frame-accurate playhead, #82 waveform overlays (beatgrid, phrase bands, cues, sections)
7. #57 genre-aware transition styles as data, #59 research spike: learn EQ/fader curves from real DJ mixes, #62 long sets across crates and the whole library, #63 natural-language set assistant (Claude API, optional), #64 suggest tracks that fit a gap in the set, #70 render and export the whole set as one continuous mix, #75 whole-set timeline view with the energy arc, #77 live "now playing" view with two scrolling decks, #79 transition editor v2 (stem lanes, simple first)
8. #61 plan transitions across the whole set with lookahead
