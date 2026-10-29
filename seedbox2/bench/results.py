"""Compare two results files and format them as markdown (`compare`, `report`)."""

from __future__ import annotations

THRESHOLD = 0.10  # a metric is a regression when it gets worse by more than this

# field -> (higher is better, noise floor: changes smaller than this are never regressions)
METRICS: dict[str, tuple[bool, float]] = {
    "seconds_median": (False, 0.05),
    "per_4min_s_median": (False, 0.1),
    "peak_rss_mb": (False, 10.0),
    "py_peak_mb": (False, 10.0),
    "fps": (True, 1.0),
    "frame_p95_ms": (False, 1.0),
    "long_tasks": (False, 0.5),
    "long_task_ms_max": (False, 10.0),
    "react_commits_per_s": (False, 2.0),
}


def flatten(results: dict) -> dict[str, float]:
    """{"analysis.seconds_median": 4.1, "frontend.zoom.fps": 60.0, ...} for the compared metrics."""
    out = {}
    for stage, s in results.get("stages", {}).items():
        for field, v in s.get("summary", {}).items():
            if field in METRICS:
                out[f"{stage}.{field}"] = v
    for name, sc in results.get("frontend", {}).get("scenarios", {}).items():
        for field, v in sc.items():
            if field in METRICS:
                out[f"frontend.{name}.{field}"] = v
    return out


def compare(old: dict, new: dict, threshold: float = THRESHOLD) -> list[dict]:
    """Every metric both runs have: {metric, old, new, change (relative, + is worse), regression}."""
    a, b = flatten(old), flatten(new)
    rows = []
    for key in sorted(a.keys() & b.keys()):
        higher_better, floor = METRICS[key.rsplit(".", 1)[1]]
        worse = (a[key] - b[key]) if higher_better else (b[key] - a[key])
        change = worse / abs(a[key]) if a[key] else (float("inf") if worse > 0 else 0.0)
        rows.append({"metric": key, "old": a[key], "new": b[key], "change": change,
                     "regression": worse > floor and change > threshold})  # fmt: skip
    return rows


def _num(v: float) -> str:
    return f"{v:.3g}" if abs(v) < 100 else f"{v:.0f}"


def _machine(r: dict) -> str:
    m = r.get("machine", {})
    batt = {True: "on battery", False: "plugged in"}.get(m.get("on_battery"), "no battery")
    return f"{m.get('label', '?')}: {m.get('cpu', '?')}, {m.get('ram_gb', '?')} GB, {m.get('os', '?')}, {batt}"


def compare_markdown(old: dict, new: dict, threshold: float = THRESHOLD) -> tuple[str, bool]:
    """(markdown, any regression)."""
    rows = compare(old, new, threshold)
    bad = [r for r in rows if r["regression"]]
    lines = [f"**Seedbox 2 benchmark** `{old.get('commit') or '?'}` → `{new.get('commit') or '?'}`", ""]
    if old.get("machine", {}).get("cpu") != new.get("machine", {}).get("cpu"):
        lines += [f"⚠️ Different machines: {_machine(old)} vs {_machine(new)}", ""]
    if old.get("corpus") != new.get("corpus"):
        lines += [f"⚠️ Different corpora: {old.get('corpus')} vs {new.get('corpus')}", ""]
    if bad:
        lines += [f"{len(bad)} regression(s) over {threshold:.0%}:", "", "| Metric | Before | After | Change |",
                  "|---|---:|---:|---:|"]  # fmt: skip
        lines += [f"| {r['metric']} | {_num(r['old'])} | {_num(r['new'])} | +{r['change']:.0%} |" for r in bad]
    else:
        lines.append(f"No regressions over {threshold:.0%} across {len(rows)} metrics.")
    over = [b for b in new.get("budgets", []) if not b["ok"]]
    if over:
        lines += ["", "Over budget: " + ", ".join(f"{b['section']}.{b['field']} {_num(b['value'])}{b['unit']} "
                                                  f"(budget {_num(b['limit'])}{b['unit']})" for b in over)]
    return "\n".join(lines) + "\n", bool(bad)


def report_markdown(r: dict) -> str:
    """The run as markdown: machine, budgets, per-stage summary."""
    lines = [f"**Seedbox 2 benchmark** {r.get('seedbox', '?')} `{r.get('commit') or '?'}`, {r.get('created', '')}",
             "", _machine(r), f"Corpus: {r.get('corpus')}", ""]  # fmt: skip
    if r.get("budgets"):
        lines += ["| Budget | Measured | Limit | |", "|---|---:|---:|---|"]
        for b in r["budgets"]:
            lines.append(f"| {b['what']} ({b['section']}) | {_num(b['value'])} {b['unit']} | {_num(b['limit'])} "
                         f"{b['unit']} | {'✅' if b['ok'] else '❌'} |")  # fmt: skip
        lines.append("")
    lines += ["| Stage | Items | Median s | Max s | Per 4 min (max) | Peak RSS MB | Python heap MB |",
              "|---|---:|---:|---:|---:|---:|---:|"]  # fmt: skip
    for stage, s in r.get("stages", {}).items():
        if "skipped" in s:
            lines.append(f"| {stage} | skipped: {s['skipped']} | | | | | |")
            continue
        m = s["summary"]
        cell = lambda k: _num(m[k]) if k in m else ""  # noqa: E731
        err = f" ({m['errors']} failed)" if m.get("errors") else ""
        lines.append(f"| {stage} | {m['count']}{err} | {cell('seconds_median')} | {cell('seconds_max')} | "
                     f"{cell('per_4min_s_max')} | {cell('peak_rss_mb')} | {cell('py_peak_mb')} |")  # fmt: skip
    fe = r.get("frontend", {}).get("scenarios")
    if fe:
        lines += ["", "| UI scenario | fps | p95 frame ms | Long tasks | Longest task ms | React commits/s |",
                  "|---|---:|---:|---:|---:|---:|"]  # fmt: skip
        for name, sc in fe.items():
            if "error" in sc:
                lines.append(f"| {name} | error: {sc['error']} | | | | |")
                continue
            commits = _num(sc["react_commits_per_s"]) if "react_commits_per_s" in sc else ""
            lines.append(f"| {name} | {_num(sc['fps'])} | {_num(sc['frame_p95_ms'])} | {sc['long_tasks']} | "
                         f"{_num(sc['long_task_ms_max'])} | {commits} |")  # fmt: skip
    return "\n".join(lines) + "\n"
