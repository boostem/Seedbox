"""Score analysers on datasets: the per-track scores, the mean per dataset and analyser, the table and the JSON."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import traceback
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .analysers import ANALYSERS
from .datasets import Reference
from .metrics import LOWER_IS_BETTER, METRICS, score

FORMAT = "seedbox2-eval-analysis/1"


def _one(job: tuple[str, Reference]) -> dict:
    name, ref = job
    row = {"dataset": ref.dataset, "track": ref.track, "analyser": name}
    try:
        est = ANALYSERS[name](ref.audio)
        row["metrics"] = score(ref, est)
        # the headline values, to see what went wrong without rerunning
        row["reference"] = {"bpm": ref.bpm, "key": ref.key}
        row["estimate"] = {"bpm": est.bpm, "key": est.key}
    except Exception as e:  # noqa: BLE001 (one bad file must not stop the run; it shows up as an error)
        row["error"] = f"{type(e).__name__}: {e}"
        row["trace"] = traceback.format_exc(limit=3)
    return row


def evaluate(refs: dict[str, list[Reference]], analysers: list[str], jobs: int = 1,
             log: Callable[[str], None] = print) -> list[dict]:
    work = [(a, r) for rs in refs.values() for r in rs for a in analysers]
    if jobs <= 1:
        rows = []
        for i, w in enumerate(work, 1):
            rows.append(_one(w))
            if i % 25 == 0:
                log(f"{i}/{len(work)}")
        return rows
    with ProcessPoolExecutor(jobs) as pool:
        return list(pool.map(_one, work, chunksize=4))


def summarise(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """dataset -> analyser -> {metric: mean points, "tracks": n, "errors": n}."""
    acc: dict[tuple[str, str], dict[str, list[float]]] = {}
    counts: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        key = (r["dataset"], r["analyser"])
        c = counts.setdefault(key, [0, 0])
        c[0] += 1
        if "error" in r:
            c[1] += 1
            continue
        for m, v in r["metrics"].items():
            acc.setdefault(key, {}).setdefault(m, []).append(v)
    out: dict[str, dict[str, dict]] = {}
    for (ds, an), (n, err) in counts.items():
        s = {m: round(sum(v) / len(v), 2) for m, v in acc.get((ds, an), {}).items()}
        out.setdefault(ds, {})[an] = {**s, "tracks": n, "errors": err}
    return out


def table(summary: dict[str, dict[str, dict]]) -> str:
    """One block per dataset: a row per analyser, a column per metric it was scored on."""
    lines = []
    for ds, by in summary.items():
        cols = [m for m in METRICS if any(m in s for s in by.values())]
        head = ["analyser", "tracks", *cols]
        body = [[an, f"{s['tracks']}" + (f" ({s['errors']} err)" if s["errors"] else ""),
                 *(f"{s[m]:.1f}" if m in s else "—" for m in cols)] for an, s in by.items()]
        w = [max(len(row[i]) for row in [head, *body]) for i in range(len(head))]
        lines.append(f"{ds}" + ("" if cols else ": nothing scored (no analyser estimates what it annotates)"))
        for row in [head, *body]:
            lines.append("  " + "  ".join(c.ljust(w[i]) if i == 0 else c.rjust(w[i]) for i, c in enumerate(row)))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def regressions(summary: dict, baseline: dict, max_drop: float) -> list[str]:
    """Metrics more than `max_drop` points worse than the baseline run (only datasets and analysers in both)."""
    out = []
    for ds, by in baseline.get("summary", {}).items():
        for an, base in by.items():
            now = summary.get(ds, {}).get(an)
            if now is None:
                continue
            if now["tracks"] != base["tracks"]:
                out.append(f"{ds}/{an}: {now['tracks']} tracks scored, the baseline has {base['tracks']}")
                continue
            for m in METRICS:
                if m not in base:
                    continue
                if m not in now:
                    out.append(f"{ds}/{an} {m}: no longer scored (baseline {base[m]:.1f})")
                    continue
                drop = (now[m] - base[m]) if m in LOWER_IS_BETTER else (base[m] - now[m])
                if drop > max_drop + 1e-9:
                    out.append(f"{ds}/{an} {m}: {now[m]:.1f}, baseline {base[m]:.1f} ({drop:+.1f} points worse)")
    return out


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5,
                              cwd=Path(__file__).resolve().parent).stdout.strip() or None
    except OSError:
        return None


def results_dir() -> Path:
    if env := os.environ.get("SEEDBOX2_EVAL_RESULTS", "").strip():
        return Path(env).expanduser()
    from .datasets import data_root  # noqa: PLC0415

    return data_root().parent / "eval-results"


def run_record(rows: list[dict], refs: dict[str, list[Reference]], analysers: list[str]) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "format": FORMAT,
        "created": now.isoformat(timespec="seconds"),
        "commit": _git_commit(),
        "analysers": analysers,
        "datasets": {ds: len(rs) for ds, rs in refs.items()},
        "summary": summarise(rows),
        "tracks": rows,
    }


def save(record: dict, out: Path | None = None) -> Path:
    if out is None:
        stamp = record["created"].replace(":", "").replace("-", "").replace("+0000", "Z")
        out = results_dir() / f"analysis-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, allow_nan=False) + "\n", encoding="utf-8")
    return out


def strip_tracks(record: dict) -> dict:
    """A run without the per-track rows (what the committed baseline keeps)."""
    return {k: v for k, v in record.items() if k != "tracks"}


