"""Seedbox 2's benchmark harness: `python -m seedbox2.bench` holds the app to the budgets in seedbox2/README.md.

    python -m seedbox2.bench run [-o results.json]      # analysis, stems, plan, render on the 10-track corpus
    python -m seedbox2.bench frontend [-o fe.json]      # fps, long tasks, React commits in headless Chrome
    python -m seedbox2.bench compare old.json new.json  # regressions > 10%, as markdown (exit 1 if any)
    python -m seedbox2.bench report results.json        # the numbers and budgets as markdown, to paste

How to run it on a laptop and submit the numbers: seedbox2/bench/README.md.
"""

SCHEMA = 1

# The budgets of seedbox2/README.md the harness checks: (section, field) of the results -> (limit, unit, what).
# Times are held to the worst track or pair, not the median. Frontend budgets apply to every scenario, except
# React commits, which only the waveform scenarios are held to (a virtualised list re-renders as rows scroll in).
BUDGETS: dict[tuple[str, str], tuple[float, str, str]] = {
    ("analysis", "per_4min_s_max"): (15.0, "s", "beats + key + waveform per 4-minute track"),
    ("stems", "per_4min_s_max"): (120.0, "s", "stems per 4-minute track (x86; 60 s on the M1)"),
    ("plan", "seconds_max"): (0.5, "s", "plan one transition"),
    ("render", "seconds_max"): (2.0, "s", "render a 32-bar transition"),
    ("render", "peak_rss_mb"): (300.0, "MB", "render a 32-bar transition"),
    ("stages", "peak_rss_mb"): (2500.0, "MB", "peak RSS of the heaviest stage (whole app < 2.5 GB)"),
}
FRONTEND_BUDGETS: dict[str, tuple[float, str, str]] = {
    "fps": (58.0, "fps", "60 fps (58 allows for rounding)"),
    "long_tasks": (0.0, "", "no long tasks > 50 ms"),
}
# frontend2/PERFORMANCE.md: zooming, scrolling, hovering and playing the waveform draw from refs, not React renders.
# A few commits a second are left for coarse UI that changes with the view ("zoomed in?" enables the fit button).
RENDERLESS_SCENARIOS = ("zoom", "scroll", "hover", "play", "play_centred", "now_playing", "now_playing_low_power", "timeline_zoom", "timeline_drag")
REACT_COMMITS_BUDGET: tuple[float, str, str] = (5.0, "/s", "no React render per frame (waveform scenarios)")
# Low-power mode's cap on the decks (#77): the display still runs at 60, the decks draw at 30.
LOW_POWER_DRAWS_BUDGET: tuple[float, str, str] = (31.0, "/s", "draws at most 30 times a second in low-power mode")
# The centred waveform (#76): no jitter. An even 60 Hz has a frame time spread of about 0.1 ms; at 16 bars on a 1,400 px
# view, 1 ms of unevenness in the playhead is 0.05 px.
CENTRED_BUDGETS: dict[str, tuple[float, str, str]] = {
    "frame_stddev_ms": (2.0, "ms", "frame time variance while the waveform scrolls"),
    "head_jitter_ms": (1.0, "ms", "frame-to-frame unevenness of the playhead"),
}
