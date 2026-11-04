"""seedbox2.bench: measuring a stage in its own process, the budgets, comparing runs, and a short run end to end."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from musicreco.paths import ffmpeg_path
from seedbox2.bench import corpus, frontend, results
from seedbox2.bench.measure import measure
from seedbox2.bench.run import check_budgets, run, summarise

ROOT = Path(__file__).resolve().parent.parent


def allocate(mb: int) -> dict:
    """A stage for measure(): holds `mb` MB of touched memory for a moment."""
    a = np.ones(mb * 1024 * 1024 // 8)
    return {"sum": float(a[:10].sum())}


def fail() -> dict:
    raise ValueError("bad audio")


def test_measure_reports_time_and_memory():
    m = measure("tests.test_bench:allocate", 200)
    assert m["info"] == {"sum": 10.0}
    assert m["seconds"] > 0
    assert m["peak_rss_mb"] >= m["baseline_rss_mb"] + 150
    assert m["py_peak_mb"] >= 190  # numpy reports its buffers to tracemalloc


def test_measure_reports_errors():
    m = measure("tests.test_bench:fail")
    assert m["error"] == "ValueError: bad audio"


def test_summary_and_budgets():
    items = [{"item": "a", "seconds": 1.0, "per_4min_s": 2.0, "peak_rss_mb": 100.0, "py_peak_mb": 10.0},
             {"item": "b", "seconds": 3.0, "per_4min_s": 20.0, "peak_rss_mb": 300.0, "py_peak_mb": 30.0},
             {"item": "c", "error": "boom"}]  # fmt: skip
    s = summarise(items)
    assert s == {"count": 2, "errors": 1, "seconds_median": 2.0, "seconds_max": 3.0, "peak_rss_mb": 300.0,
                 "py_peak_mb": 30.0, "per_4min_s_median": 11.0, "per_4min_s_max": 20.0}  # fmt: skip
    rows = check_budgets({"stages": {"analysis": {"summary": s}, "stems": {"skipped": "not yet"}},
                          "frontend": {"scenarios": {"zoom": {"fps": 59.5, "long_tasks": 2, "react_commits_per_s": 30.0},
                                                     "list": {"fps": 60.0, "react_commits_per_s": 30.0}}}})  # fmt: skip
    got = {(r["section"], r["field"]): r["ok"] for r in rows}
    assert got == {("analysis", "per_4min_s_max"): False, ("stages", "peak_rss_mb"): True,
                   ("frontend.zoom", "fps"): True, ("frontend.zoom", "long_tasks"): False,
                   ("frontend.zoom", "react_commits_per_s"): False, ("frontend.list", "fps"): True}  # fmt: skip


def _results(**fields) -> dict:
    return {"commit": "abc", "machine": {"cpu": "M1"}, "corpus": "c",
            "stages": {"analysis": {"summary": {"seconds_median": 4.0, "peak_rss_mb": 500.0}}},
            "frontend": {"scenarios": {"zoom": {"fps": 60.0, "long_tasks": 0}}}} | fields  # fmt: skip


def test_compare_flags_regressions_over_ten_percent():
    old, new = _results(), _results()
    new["stages"]["analysis"]["summary"] = {"seconds_median": 4.5, "peak_rss_mb": 540.0}  # +12.5 %, +8 %
    new["frontend"]["scenarios"]["zoom"] = {"fps": 50.0, "long_tasks": 3}
    bad = {r["metric"] for r in results.compare(old, new) if r["regression"]}
    assert bad == {"analysis.seconds_median", "frontend.zoom.fps", "frontend.zoom.long_tasks"}
    text, regressed = results.compare_markdown(old, new)
    assert regressed and "| analysis.seconds_median | 4 | 4.5 | +12% |" in text


def test_compare_ignores_improvements_and_noise():
    old, new = _results(), _results()
    old["stages"]["analysis"]["summary"] = {"seconds_median": 0.01, "peak_rss_mb": 500.0}
    new["stages"]["analysis"]["summary"] = {"seconds_median": 0.03, "peak_rss_mb": 400.0}  # 3x, but 20 ms
    new["frontend"]["scenarios"]["zoom"]["fps"] = 61.0
    assert not any(r["regression"] for r in results.compare(old, new))
    text, regressed = results.compare_markdown(old, new | {"machine": {"cpu": "i5"}})
    assert not regressed and "Different machines" in text


def test_cli_compare_exit_code(tmp_path):
    old, new = _results(), _results()
    new["stages"]["analysis"]["summary"]["seconds_median"] = 8.0
    (tmp_path / "old.json").write_text(json.dumps(old))
    (tmp_path / "new.json").write_text(json.dumps(new))
    cmd = [sys.executable, "-m", "seedbox2.bench", "compare"]
    same = subprocess.run([*cmd, tmp_path / "old.json", tmp_path / "old.json"], cwd=ROOT, capture_output=True, text=True)
    worse = subprocess.run([*cmd, tmp_path / "old.json", tmp_path / "new.json"], cwd=ROOT, capture_output=True, text=True)
    assert (same.returncode, worse.returncode) == (0, 1)
    assert "1 regression(s)" in worse.stdout


def test_corpus_is_ten_pinned_tracks():
    assert len(corpus.TRACKS) == 10 and len({t.sha256 for t in corpus.TRACKS}) == 10
    assert corpus.TRACKS[1].url.endswith("/Electro%20Cabello.mp3")


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")
def test_run_on_synthetic_tracks(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))  # no models there: stems are skipped
    tracks = corpus.synthetic(tmp_path / "corpus", count=2, seconds=40)
    r = run(tracks, ("analysis", "stems", "plan"), log=lambda _: None, corpus_name="synthetic")
    assert r["stages"]["stems"]["skipped"].startswith("needs the htdemucs model")
    an, plan = r["stages"]["analysis"], r["stages"]["plan"]
    assert an["summary"]["count"] == 2 and plan["summary"]["count"] == 1, (an, plan)
    assert abs(an["items"][0]["info"]["bpm"] - 118) < 1
    assert an["items"][0]["per_4min_s"] == pytest.approx(an["items"][0]["seconds"] * 6, rel=1e-3)
    assert "render" not in r["stages"]
    assert {(b["section"], b["field"]) for b in r["budgets"]} >= {("analysis", "per_4min_s_max"), ("plan", "seconds_max")}
    assert "| analysis | 2 |" in results.report_markdown(copy.deepcopy(r))


@pytest.mark.skipif(frontend.find_chrome() is None or not (frontend.DIST / "index.html").exists(),
                    reason="needs Chrome and frontend2/dist")  # fmt: skip
def test_frontend_bench_measures_every_scenario():
    fe = frontend.run(seconds=0.5, log=lambda _: None)
    assert set(fe["scenarios"]) == {"zoom", "scroll", "hover", "play", "list"}
    for name, sc in fe["scenarios"].items():
        assert sc["frames"] > 5, (name, sc)
        assert "react_commits_per_s" in sc, (name, sc)
    assert fe["scenarios"]["list"]["react_commits"] > 0  # the virtualised table re-renders as rows scroll in
