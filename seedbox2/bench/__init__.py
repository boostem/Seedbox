"""Seedbox 2's benchmark harness: `python -m seedbox2.bench` holds the app to the budgets in seedbox2/README.md.

    python -m seedbox2.bench run [-o results.json]      # analysis, stems, plan, render on the 10-track corpus
    python -m seedbox2.bench frontend [-o fe.json]      # fps and long tasks in headless Chrome (frontend2/dist)
    python -m seedbox2.bench compare old.json new.json  # regressions > 10%, as markdown (exit 1 if any)
    python -m seedbox2.bench report results.json        # the numbers and budgets as markdown, to paste

How to run it on a laptop and submit the numbers: seedbox2/bench/README.md.
"""

SCHEMA = 1

# The budgets of seedbox2/README.md the harness checks: (section, field) of the results -> (limit, unit, what).
# Times are held to the worst track or pair, not the median. Frontend budgets apply to every scenario.
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
