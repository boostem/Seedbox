"""`python -m seedbox2.bench run`: every stage on every track (analysis, stems) or consecutive pair (plan,
render), each in its own process, summarised and checked against the budgets."""

from __future__ import annotations

import datetime as dt
import os
import platform
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil

from seedbox2 import __version__
from seedbox2.bench import BUDGETS, SCHEMA, corpus, stages
from seedbox2.bench.measure import measure

ROOT = Path(__file__).resolve().parents[2]


def _cpu() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                  timeout=5).stdout.strip()  # fmt: skip
        if sys.platform.startswith("linux"):
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.processor() or platform.machine()


def _commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                             timeout=5)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def machine(label: str | None = None) -> dict:
    """What the numbers were measured on. `on_battery` is None on machines without a battery."""
    try:
        battery = psutil.sensors_battery()
    except (AttributeError, NotImplementedError, OSError):
        battery = None
    return {
        "label": label or os.environ.get("SEEDBOX_BENCH_LABEL") or platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu": _cpu(),
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 1024**3, 1),
        "on_battery": None if battery is None else not battery.power_plugged,
        "python": platform.python_version(),
        "ci": bool(os.environ.get("CI")),
    }


def _q(values: list[float], f) -> float | None:
    return round(f(values), 4) if values else None


def summarise(items: list[dict]) -> dict:
    ok = [i for i in items if "error" not in i]
    secs = [i["seconds"] for i in ok]
    per4 = [i["per_4min_s"] for i in ok if "per_4min_s" in i]
    out = {
        "count": len(ok),
        "errors": len(items) - len(ok),
        "seconds_median": _q(secs, statistics.median),
        "seconds_max": _q(secs, max),
        "peak_rss_mb": _q([i["peak_rss_mb"] for i in ok], max),
        "py_peak_mb": _q([i["py_peak_mb"] for i in ok if i.get("py_peak_mb") is not None], max),
    }
    if per4:
        out |= {"per_4min_s_median": _q(per4, statistics.median), "per_4min_s_max": _q(per4, max)}
    return {k: v for k, v in out.items() if v is not None}


def check_budgets(results: dict) -> list[dict]:
    """One row per budget that has a measured value: {section, field, value, limit, unit, what, ok}."""
    from seedbox2.bench import CENTRED_BUDGETS, FRONTEND_BUDGETS, REACT_COMMITS_BUDGET, RENDERLESS_SCENARIOS
    from seedbox2.bench.frontend import ENGINE_CPU_BUDGET

    rows = []
    sections = results.get("stages", {})
    peaks = [s["summary"]["peak_rss_mb"] for s in sections.values() if "peak_rss_mb" in s.get("summary", {})]
    for (section, field), (limit, unit, what) in BUDGETS.items():
        value = max(peaks) if section == "stages" and peaks else sections.get(section, {}).get("summary", {}).get(field)
        if value is not None:
            rows.append({"section": section, "field": field, "value": value, "limit": limit, "unit": unit,
                         "what": what, "ok": value <= limit})  # fmt: skip
    for name, sc in results.get("frontend", {}).get("scenarios", {}).items():
        budgets = dict(FRONTEND_BUDGETS)
        if name in RENDERLESS_SCENARIOS:
            budgets["react_commits_per_s"] = REACT_COMMITS_BUDGET
        if name == "play_centred":
            budgets.update(CENTRED_BUDGETS)
        for field, (limit, unit, what) in budgets.items():
            if field in sc:
                ok = sc[field] >= limit if field == "fps" else sc[field] <= limit
                rows.append({"section": f"frontend.{name}", "field": field, "value": sc[field], "limit": limit,
                             "unit": unit, "what": what, "ok": ok})  # fmt: skip
    for name, sc in results.get("engine", {}).get("scenarios", {}).items():
        rows.append({"section": f"engine.{name}", "field": "cpu_percent", "value": sc["cpu_percent"],
                     "limit": ENGINE_CPU_BUDGET, "unit": "%", "what": "of one core, 2 decks x 4 stems",
                     "ok": sc["cpu_percent"] <= ENGINE_CPU_BUDGET})  # fmt: skip
    return rows


def _measure(target: str, *args, trace: bool) -> dict:
    """Time and RSS from an untraced run (tracemalloc slows allocation-heavy stages down by a quarter), the Python
    heap's peak from a second, traced run."""
    m = measure(target, *args, trace=False)
    if trace and "error" not in m:
        traced = measure(target, *args, trace=True)
        m["py_peak_mb"] = traced.get("py_peak_mb")
    return m


def _item(name: str, m: dict, duration_s: float | None = None) -> dict:
    out = {"item": name} | {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items() if k != "traceback"}
    if duration_s and "seconds" in m:
        out["per_4min_s"] = round(m["seconds"] * 240.0 / duration_s, 4)
    return out


def run(tracks: list[tuple[str, Path, float]], only: tuple[str, ...] = stages.ORDER, trace: bool = True,
        work: Path | None = None, log=print, label: str | None = None, corpus_name: str = corpus.CORPUS) -> dict:
    """Results dict (see README.md) for `tracks` = [(name, path, duration_s)]."""
    results = {"schema": SCHEMA, "seedbox": __version__, "commit": _commit(),
               "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "machine": machine(label), "corpus": corpus_name, "tracemalloc": trace, "stages": {}}  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="seedbox2-bench-", dir=work) as tmp:
        tmp = Path(tmp)
        analyses: dict[str, Path] = {}
        plans: dict[tuple[str, str], Path] = {}
        pairs = list(zip(tracks, tracks[1:]))
        for stage in stages.ORDER:
            if stage not in only:
                continue
            why = stages.missing(stage)
            if stage in ("plan", "render") and not why and len(tracks) < 2:
                why = "needs two tracks"
            if why:
                log(f"{stage}: skipped, {why}")
                results["stages"][stage] = {"impl": stages.IMPL[stage], "skipped": why}
                continue
            target = f"seedbox2.bench.stages:{stage}"
            items = []
            if stage in ("analysis", "stems"):
                for name, path, dur in tracks:
                    if stage == "analysis":
                        analyses[name] = tmp / f"{len(analyses)}.analysis.json"
                        args = (str(path), str(analyses[name]))
                    else:
                        args = (str(path), str(tmp / "stems" / name))
                    items.append(_item(name, _measure(target, *args, trace=trace), dur))
                    log(_line(stage, items[-1]))
            else:
                for (an, ap, _), (bn, bp, _) in pairs:
                    pair = f"{an} → {bn}"
                    if an not in analyses or bn not in analyses or not analyses[an].exists() or not analyses[bn].exists():
                        items.append({"item": pair, "error": "no analysis for both tracks (run the analysis stage)"})
                    elif stage == "plan":
                        plans[(an, bn)] = tmp / f"{len(plans)}.plan.json"
                        m = _measure(target, str(ap), str(analyses[an]), str(bp), str(analyses[bn]),
                                    str(plans[(an, bn)]), trace=trace)  # fmt: skip
                        items.append(_item(pair, m))
                    elif (an, bn) in plans and plans[(an, bn)].exists():
                        items.append(_item(pair, _measure(target, str(plans[(an, bn)]), str(ap), str(bp), trace=trace)))
                    else:
                        items.append({"item": pair, "error": "no plan (run the plan stage)"})
                    log(_line(stage, items[-1]))
            results["stages"][stage] = {"impl": stages.IMPL[stage], "summary": summarise(items), "items": items}
    results["budgets"] = check_budgets(results)
    return results


def _line(stage: str, item: dict) -> str:
    if "error" in item:
        return f"{stage:>8}  {item['item']}: ERROR {item['error']}"
    extra = f", {item['per_4min_s']:.2f} s per 4 min" if "per_4min_s" in item else ""
    return f"{stage:>8}  {item['item']}: {item['seconds']:.2f} s{extra}, peak {item['peak_rss_mb']:.0f} MB"
