# Seedbox 2 benchmarks

`python -m seedbox2.bench` measures Seedbox 2 against the budgets in [`seedbox2/README.md`](../README.md#reference-laptop-and-budgets).
The numbers that count come from the two reference laptops, on battery; please run it on yours and submit them.

## What it measures

| Command | What |
|---|---|
| `run` | **analysis** (each track), **stems** (each track), **plan** and **render** (each consecutive pair, a 32-bar transition) on the corpus. Each stage runs in its own process: wall time, peak RSS of the process and its children (psutil, plus the OS's own peak), and the peak of Python's heap (tracemalloc, in a second run of the stage, since tracing slows allocation-heavy code down by about a quarter). Analysis and stems are also given per 4 minutes of audio. |
| `frontend` | `frontend2/dist` in headless Chrome on `#/bench`: frames per second, 95th-percentile frame time, long tasks (> 50 ms) and React commits per second while ⌘/ctrl-wheel zooming the waveform, scrolling it, moving the mouse over it, playing with the waveform following the playhead, and scrolling a 10,000-row virtualised table. The waveform scenarios may cause at most 5 React commits a second ([`frontend2/PERFORMANCE.md`](../../frontend2/PERFORMANCE.md)). |
| `engine` | `python -m seedbox2.bench engine`: the live audio engine's AudioWorklet in an `OfflineAudioContext` in headless Chrome, flat out, with 2 decks x 4 stems and every stage on, both at the decks' own tempo and in varispeed. Wall time over audio time is its share of one core (budget: 15%). It also checks that the worklet computes the same samples as the core run on the page. Run it on the reference laptops: a fast desktop says little about them. |
| `compare OLD NEW` | Every metric both files have; a regression is a metric that got worse by more than 10 % (and by more than a small noise floor). Markdown on stdout, exit code 1 if anything regressed. |
| `report FILE` | A results file as markdown: the machine, each budget with ✅/❌, and a table per stage. |

Until the Seedbox 2 versions land, the stages run Seedbox 1's code (`seedbox2/bench/stages.py` says which, and the
results record it per stage): analysis is `musicreco.structure` (until #43, #49) plus our key (#45), planning and rendering are
`musicreco.mixplan` and `mixrender` (until #51 and the v2 engine). Stems are `seedbox2.analysis.stems.separate`
(#44), and skipped until the `htdemucs` model is downloaded (start Seedbox 2 once, or Settings → Models). When one of
those tickets lands, point its wrapper in `stages.py` at the new code.

The corpus is 10 tracks by Kevin MacLeod ([incompetech.com](https://incompetech.com), CC BY 4.0), 2:40 to 4:28
long, downloaded once (about 70 MB) into `bench/corpus` in Seedbox 2's cache folder (`$SEEDBOX_BENCH_DIR/corpus` if
set) and checked against their SHA-256 (`corpus.py`). `--synthetic` uses generated four-to-the-floor loops instead,
for quick or offline runs; their numbers don't compare with the real corpus's.

## Run it on a laptop

1. Do the [Setup](../../README.md#setup) and build the frontend: `(cd frontend2 && deno install && deno task build)`.
   Install the dev requirements too: `.venv/bin/pip install -r requirements-dev.txt` (psutil).
2. Have Chrome, Chromium or Edge installed (or set `$CHROME` to its executable).
3. Make the machine like a user's: **unplug it** and leave it on battery, close other apps, use the default power
   mode (not "high performance"), and let it sit for a minute after waking. On a Mac, keep the lid open.
4. Run (about 10–20 minutes on the reference laptops, plus the download the first time):

   ```sh
   .venv/bin/python -m seedbox2.bench run --frontend --label "M1 Air 8GB" -o bench-m1.json
   ```

   It prints a line per measurement while it goes and the report at the end. `--stages analysis,plan` runs a subset,
   `--tracks 3` the first three tracks, `--no-tracemalloc` skips the traced second run of each stage (half the
   time), `--cpu-throttle 4` slows Chrome's CPU to approximate a slower machine.
5. Compare two runs (say before and after a change, on the same machine):
   `.venv/bin/python -m seedbox2.bench compare before.json after.json`.

## Submit the numbers

Open a pull request that adds the results file as `seedbox2/bench/results/<yyyy-mm-dd>-<machine>.json` (for
example `2026-11-02-m1-air-8gb.json`), with the output of `python -m seedbox2.bench report` in the description, and
mention whether the laptop was on battery. The file records the machine (CPU, cores, RAM, OS, battery state), the
commit, the corpus and what each stage ran, so runs stay comparable later.

## Nightly CI

`.github/workflows/bench.yml` runs `run --frontend` every night (and on demand from the Actions tab) on
GitHub's Ubuntu runner, keeps the corpus and the last night's results in the Actions cache, and compares tonight's
results with them. On a regression over 10 % it comments on the open issue titled *Nightly benchmark regressions*
(created the first time). Every night's results and comparison are kept as an artifact for 90 days, and the report
is in the run's summary. A pull request labelled `bench` runs it too, on every push, and gets one comment (updated
each time) with its numbers and the comparison with the last nightly run; it doesn't replace the nightly baseline. CI runners are shared and noisy and are not the reference laptops: CI catches regressions,
the laptops check the budgets.

## Results file

```jsonc
{
  "schema": 1, "seedbox": "2.0.0.dev0", "commit": "fd5069b", "created": "2026-10-29T16:10:31+00:00",
  "machine": {"label": "M1 Air 8GB", "os": "Darwin 25.1.0", "arch": "arm64", "cpu": "Apple M1", "cores": 8,
              "threads": 8, "ram_gb": 8.0, "on_battery": true, "python": "3.12.8", "ci": false},
  "corpus": "incompetech-10-v1", "tracemalloc": true,
  "stages": {
    "analysis": {"impl": "musicreco.structure.analyze (…)",
                 "summary": {"count": 10, "errors": 0, "seconds_median": 0.9, "seconds_max": 1.2,
                             "per_4min_s_median": 1.1, "per_4min_s_max": 1.4, "peak_rss_mb": 240, "py_peak_mb": 60},
                 "items": [{"item": "Funkorama", "seconds": 0.9, "per_4min_s": 1.07, "peak_rss_mb": 238,
                            "baseline_rss_mb": 120, "py_peak_mb": 58, "info": {"bpm": 120, "bars": 100}}]},
    "stems": {"impl": "…", "skipped": "not implemented yet (#44)"}
  },
  "frontend": {"browser": "…", "cpu_throttle": 1, "viewport": [1440, 900],
               "scenarios": {"zoom": {"seconds": 5, "frames": 300, "fps": 60, "frame_p95_ms": 16.8,
                                      "frame_max_ms": 17, "long_tasks": 0, "long_task_ms_max": 0,
                                      "react_commits": 10, "react_commits_per_s": 2.0}}},
  "budgets": [{"section": "analysis", "field": "per_4min_s_max", "value": 1.4, "limit": 15, "unit": "s",
               "what": "beats + key + waveform per 4-minute track", "ok": true}]
}
```

Bump `SCHEMA` in `seedbox2/bench/__init__.py` when the shape changes, and `CORPUS` in `corpus.py` when the tracks do.
