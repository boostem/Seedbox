"""seedbox2.eval.golden: the shipped corpus holds what it promises, audio is found by hash, a corpus renders end to end
into a report, regressions between runs get flagged, and the nightly script posts its summary to the board."""

import copy
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from musicreco.paths import ffmpeg_path
from seedbox2.eval import golden, mixes
from seedbox2.library.hashing import content_hash

ROOT = Path(__file__).resolve().parent.parent
SR = 44100
SCRIPT = ROOT / "seedbox2/eval/golden-nightly.sh"
needs_ffmpeg = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")
needs_bash = pytest.mark.skipif(sys.platform == "win32", reason="the nightly script is bash, for the owner's machine")


# --- the corpus definition -------------------------------------------------------------------------------------------


def test_shipped_corpus_covers_what_the_ticket_asks():
    c = golden.load_corpus()
    assert golden.validate(c) == []
    assert 30 <= len(c.pairs) <= 50
    assert {"house", "techno", "dnb", "hiphop", "pop", "disco", "halftime", "key-clash", "vocal-heavy"} <= {
        t for p in c.pairs for t in p.tags}  # fmt: skip
    gaps = [golden.tempo_gap_pct(c.tracks[p.a].bpm, c.tracks[p.b].bpm) for p in c.pairs]
    assert max(gaps) <= 8.0 and min(gaps) < 0.5 and max(gaps) > 6.0


def test_shipped_corpus_commits_no_audio_or_local_paths():
    text = golden.CORPUS.read_text(encoding="utf-8")
    for leak in ("/mnt/", "/home/", "Dropbox", "Qbittorrent", "C:\\"):
        assert leak not in text


def test_tempo_gap_counts_half_and_double_time_as_the_same_tempo():
    assert golden.tempo_gap_pct(87, 174) == 0
    assert golden.tempo_gap_pct(100, 108) == pytest.approx(8)
    assert golden.tempo_gap_pct(176, 91.5) == pytest.approx(3.98, abs=0.01)


def _track(id_, bpm=124.0, key="8A", n=None):
    return golden.Track(id_, f"{n if n is not None else abs(hash(id_)) % 16**8:040x}", "A", id_, 100.0, bpm, key, "house")


def test_validate_catches_a_broken_corpus():
    tracks = {t.id: t for t in (_track("a", n=1), _track("b", bpm=140.0, n=2), _track("c", key="8A", n=3))}
    pairs = [golden.GoldenPair("gap", "a", "b"), golden.GoldenPair("gone", "a", "nope"),
             golden.GoldenPair("clash", "a", "c", ["key-clash"]), golden.GoldenPair("gap", "a", "c")]  # fmt: skip
    bad = golden.validate(golden.Corpus(tracks, pairs), coverage=False)
    assert any("gap" in b and "over 8" in b for b in bad)
    assert any("unknown track" in b for b in bad)
    assert any("doesn't clash" in b for b in bad)
    assert any("defined twice" in b for b in bad)
    full = golden.validate(golden.Corpus(tracks, pairs))
    assert any("the corpus should have" in b for b in full) and any("tagged dnb" in b for b in full)


def test_select_by_tag_id_and_limit():
    c = golden.Corpus({}, [golden.GoldenPair("x", "a", "b", ["house"]), golden.GoldenPair("y", "a", "b", ["pop"]),
                           golden.GoldenPair("z", "a", "b", ["pop"])])  # fmt: skip
    assert [p.id for p in golden.select(c, ["pop"])] == ["y", "z"]
    assert [p.id for p in golden.select(c, ["x", "z"])] == ["x", "z"]
    assert [p.id for p in golden.select(c, None, 1)] == ["x"]


# --- end to end on synthetic audio -----------------------------------------------------------------------------------


def _write(path: Path, bpm: float, key: str, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(path, SR, mixes.synth_track(bpm, 48, key, 16, 16, [], seed=seed).T)


@pytest.fixture(scope="module")
def corpus_dir(tmp_path_factory):
    """A two-pair corpus of synthetic tracks; its audio under odd names in a subfolder, a third pair with no audio."""
    root = tmp_path_factory.mktemp("golden")
    music = root / "music"
    _write(music / "deep/renamed one.wav", 124.0, "8A", 1)
    _write(music / "two.wav", 124.0, "9A", 2)
    _write(music / "three.wav", 126.0, "8A", 3)
    stanzas = []
    for tid, name, bpm, key in (("one", "deep/renamed one.wav", 124, "8A"), ("two", "two.wav", 124, "9A"),
                                ("three", "three.wav", 126, "8A")):  # fmt: skip
        h = content_hash(music / name)
        stanzas.append(f'[[track]]\nid = "{tid}"\nhash = "{h.hash}"\nartist = "Synth <&>"\ntitle = "{tid}"\n'
                       f'duration = {h.duration}\nbpm = {bpm}\nkey = "{key}"\ngenre = "house"\n')  # fmt: skip
    stanzas.append(f'[[track]]\nid = "absent"\nhash = "{"f" * 40}"\nduration = 1.0\nbpm = 124\nkey = "8A"\n')
    stanzas += ['[[pair]]\nid = "one-two"\na = "one"\nb = "two"\ntags = ["house"]\n',
                '[[pair]]\nid = "two-three"\na = "two"\nb = "three"\ntags = ["house"]\n',
                '[[pair]]\nid = "gone"\na = "one"\nb = "absent"\ntags = ["house"]\n']  # fmt: skip
    corpus = root / "golden.toml"
    corpus.write_text("\n".join(stanzas), encoding="utf-8")
    return root


def _env(root: Path, **extra) -> dict:
    return {**os.environ, "SEEDBOX2_EVAL_RESULTS": str(root / "results"), "SEEDBOX_SUPPORT_DIR": str(root / "support"),
            "SEEDBOX2_GOLDEN_AUDIO": str(root / "music"), **extra}  # fmt: skip


def _golden(root: Path, *args, **env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "seedbox2.eval", "golden", *args], cwd=ROOT, env=_env(root, **env),
                          capture_output=True, text=True, timeout=500)  # fmt: skip


def test_find_audio_goes_by_hash_and_remembers_it(corpus_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("SEEDBOX_SUPPORT_DIR", str(tmp_path / "support"))
    c = golden.load_corpus(corpus_dir / "golden.toml")
    index = tmp_path / "index.json"
    found = golden.find_audio(c.tracks.values(), [corpus_dir / "music"], index_file=index)
    assert found["one"].name == "renamed one.wav" and "absent" not in found
    known = json.loads(index.read_text())
    assert len(known) >= 3
    monkeypatch.setattr("seedbox2.library.hashing.content_hash", lambda p: pytest.fail("hashed again"))
    assert golden.find_audio(c.tracks.values(), [corpus_dir / "music"], index_file=index)["two"] == found["two"]


@pytest.fixture(scope="module")
def run_dir(corpus_dir):
    out = corpus_dir / "results/golden/first"
    r = _golden(corpus_dir, "--corpus", str(corpus_dir / "golden.toml"), "--jobs", "1", "--out", str(out),
                "--summary", str(corpus_dir / "summary.md"), "--engines", "seedbox1,seedbox2")  # fmt: skip
    assert r.returncode == 0, r.stderr
    return out


@needs_ffmpeg
def test_run_writes_metrics_report_players_plans_and_waveforms(run_dir):
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["format"] == golden.FORMAT
    rows = {r["id"]: r for r in record["pairs"]}
    res = rows["one-two"]["results"]["seedbox1"]
    assert res["metrics"]["overlap"]["seconds"] > 0
    assert len(res["wave"]["mix"]) == golden.WAVE_COLUMNS and max(res["wave"]["mix"]) > 0
    assert (run_dir / res["mix"]).stat().st_size > 10_000 and (run_dir / res["plan_file"]).is_file()
    assert "unavailable" in rows["one-two"]["results"]["seedbox2"]
    assert rows["gone"]["missing"] == ["absent"] and rows["gone"]["results"] == {}

    page = (run_dir / "report.html").read_text(encoding="utf-8")
    assert f'src="{res["mix"]}"' in page and "<audio" in page and "<svg" in page
    assert res["plan"]["technique"] in page and "the plan as JSON" in page
    assert "Synth &lt;&amp;&gt;" in page and "Synth <&>" not in page  # escaped
    assert "1 pairs not rendered" in page


@needs_ffmpeg
def test_summary_says_what_was_not_rendered(corpus_dir, run_dir):
    text = (corpus_dir / "summary.md").read_text(encoding="utf-8")
    assert "2/3 pairs rendered" in text and "1 pairs not rendered" in text and "gone" in text
    assert "engine unavailable" in text


@needs_ffmpeg
def test_same_engine_twice_shows_no_regressions(corpus_dir, run_dir):
    """A render is deterministic, or nightly comparisons would cry wolf."""
    out = corpus_dir / "results/golden/second"
    r = _golden(corpus_dir, "--corpus", str(corpus_dir / "golden.toml"), "--jobs", "1", "--out", str(out),
                "--baseline", str(run_dir))  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert "No regressions" in r.stdout


@needs_ffmpeg
def test_compare_flags_a_doctored_run_and_writes_the_side_by_side_report(corpus_dir, run_dir, tmp_path):
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    worse = copy.deepcopy(record)
    worse["commit"] = "worse"
    worse["pairs"][0]["results"]["seedbox1"]["metrics"]["loudness"]["clipped_samples"] = 500
    new = tmp_path / "new"
    new.mkdir()
    (new / "run.json").write_text(json.dumps(worse), encoding="utf-8")
    out = tmp_path / "cmp.html"
    r = _golden(corpus_dir, "compare", str(run_dir), str(new), "--out", str(out))
    assert r.returncode == 1
    assert "one-two: Clipped samples" in r.stderr
    page = out.read_text(encoding="utf-8")
    assert 'class="bad"' in page and "seedbox1 @ worse" in page
    assert Path(os.path.relpath(run_dir / "renders", tmp_path)).as_posix() in page  # the baseline's player still points at its render

    same = _golden(corpus_dir, "compare", f"{run_dir}:seedbox1", f"{run_dir}:seedbox1", "--out", str(tmp_path / "s.html"))
    assert same.returncode == 0 and "no regressions" in same.stdout


# --- regressions -----------------------------------------------------------------------------------------------------


def _metrics(**over):
    m = {"overlap": {"seconds": 16.0, "start_s": 1, "end_s": 17}, "duration_s": 30.0,
         "beat": {"median_ms": 2.0, "p95_ms": 5.0}, "bass": {"seconds": 0.0, "db_s": 0.0},
         "vocals": {"seconds": 0.0}, "loudness": {"dip_lu": -1.0, "bump_lu": 0.5, "true_peak_dbtp": -1.5,
                                                  "clipped_samples": 0},
         "masking": {"sub": 0.1, "bass": 0.1, "low_mid": 0.2, "high_mid": 0.2, "high": 0.2},
         "key": {"key_steps": 1, "max_stretch_pct": 1.0}, "checks": [{"id": "beat", "label": "Beats in sync",
                                                                      "status": "pass"}]}  # fmt: skip
    for path, v in over.items():
        node = m
        *head, last = path.split("__")
        for k in head:
            node = node[k]
        node[last] = v
    return m


def _rows(**metrics_by_pair):
    return [{"id": k, "results": {"e": v if "error" in v else {"metrics": v}}}
            for k, v in metrics_by_pair.items()]  # fmt: skip


@pytest.mark.parametrize(("over", "metric"), [
    ({"beat__median_ms": 4.5}, "beat_median_ms"),
    ({"beat__p95_ms": 11.0}, "beat_p95_ms"),
    ({"bass__seconds": 1.0}, "bass_s"),
    ({"bass__db_s": 20.0}, "bass_db_s"),
    ({"vocals__seconds": 1.0}, "vocal_s"),
    ({"loudness__dip_lu": -2.0}, "dip_lu"),
    ({"loudness__bump_lu": 1.5}, "bump_lu"),
    ({"loudness__true_peak_dbtp": -0.5}, "true_peak"),
    ({"loudness__clipped_samples": 1}, "clipped"),
    ({"masking__sub": 0.2}, "mask_sub"),
    ({"masking__bass": 0.2}, "mask_bass"),
    ({"masking__low_mid": 0.3}, "mask_low_mid"),
    ({"masking__high_mid": 0.3}, "mask_high_mid"),
    ({"masking__high": 0.3}, "mask_high"),
    ({"key__key_steps": 2}, "key_steps"),
    ({"key__max_stretch_pct": 2.0}, "stretch_pct"),
])  # fmt: skip
def test_every_metric_flags_a_regression(over, metric):
    out = golden.compare(_rows(p=_metrics()), "e", _rows(p=_metrics(**over)), "e")
    assert metric in {x["metric"] for x in out["regressions"] if x["pair"] == "p"}
    back = golden.compare(_rows(p=_metrics(**over)), "e", _rows(p=_metrics()), "e")
    assert not back["regressions"] and metric in {x["metric"] for x in back["improved"]}


def test_small_changes_within_tolerance_are_not_flagged():
    out = golden.compare(_rows(p=_metrics()), "e", _rows(p=_metrics(beat__median_ms=2.5, masking__bass=0.12)), "e")
    assert out["regressions"] == []


def test_a_small_drift_over_all_pairs_is_flagged_on_the_mean():
    base = _rows(p=_metrics(), q=_metrics())
    new = _rows(p=_metrics(beat__median_ms=3.5), q=_metrics(beat__median_ms=3.5))
    reg = golden.compare(base, "e", new, "e")["regressions"]
    assert [(x["pair"], x["metric"]) for x in reg] == [("(mean)", "beat_median_ms")]


def test_a_worse_check_a_failed_render_and_a_lost_pair_are_regressions():
    worse = _metrics()
    worse["checks"][0]["status"] = "warn"
    base = _rows(p=_metrics(), q=_metrics(), r=_metrics())
    new = _rows(p=worse, q={"error": "ValueError: boom"})
    reg = {(x["pair"], x["metric"]): x for x in golden.compare(base, "e", new, "e")["regressions"]}
    assert ("p", "check_beat") in reg
    assert reg[("q", "render")]["new"] == "ValueError: boom"
    assert reg[("r", "render")]["new"] == "not in this run"


def test_a_pair_new_in_the_run_is_not_a_regression():
    assert golden.compare(_rows(p=_metrics()), "e", _rows(p=_metrics(), q={"error": "x"}), "e")["regressions"] == []


def test_prune_keeps_the_newest_and_the_accepted_run_and_touches_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEDBOX2_EVAL_RESULTS", str(tmp_path))
    root = tmp_path / "golden"
    for name in ("1", "2", "3", "4"):
        (root / name).mkdir(parents=True)
        (root / name / "run.json").write_text("{}")
    (root / "worktree").mkdir()
    golden.accept(root / "1")
    golden.prune(1)
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["1", "4", "worktree"]


def test_peaks_are_bytes_per_column():
    x = np.zeros((2, 1000))
    x[0, 500] = 0.5
    assert golden.peaks(x, 10)[5] == 128 and max(golden.peaks(x, 10)[:5]) == 0 and len(golden.peaks(x, 10)) == 10


# --- the nightly script ----------------------------------------------------------------------------------------------


class _Board(http.server.BaseHTTPRequestHandler):
    posts: list = []
    fail = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).posts.append((self.path, body, self.headers["X-Actor"]))
        self.send_response(500 if type(self).fail else 201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"id": "TICKET1"}).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def board():
    _Board.posts, _Board.fail = [], False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Board)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _fake_python(tmp_path: Path, exit_status: int, summary: str = "the summary\n") -> Path:
    """Stands in for the interpreter: answers the script's helper one-liners for real, fakes the golden run."""
    fake = tmp_path / "python"
    fake.write_text(f"""#!/usr/bin/env bash
if [[ ${{1:-}} == -m ]]; then
  while [[ $# -gt 0 ]]; do [[ $1 == --summary ]] && printf '%b' {summary!r} > "$2"; shift; done
  echo 'run log line'
  exit {exit_status}
fi
exec {sys.executable} "$@"
""")
    fake.chmod(0o755)
    return fake


def _nightly(tmp_path: Path, board: str, fake: Path, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60, env={
        **os.environ, "SEEDBOX2_GOLDEN_PYTHON": str(fake), "SEEDBOX2_GOLDEN_UPDATE": "0", "KANBAN": board,
        "SEEDBOX2_EVAL_RESULTS": str(tmp_path / "results"), **env})  # fmt: skip


@needs_bash
def test_nightly_files_its_ticket_once_and_posts_the_summary(tmp_path, board):
    fake = _fake_python(tmp_path, 0)
    assert _nightly(tmp_path, board, fake).returncode == 0
    assert _nightly(tmp_path, board, fake).returncode == 0
    paths = [p[0] for p in _Board.posts]
    assert paths == ["/api/boards/w118DWMEAfM/tickets", "/api/tickets/TICKET1/comments", "/api/tickets/TICKET1/comments"]
    ticket = _Board.posts[0][1]
    assert ticket["tags"] == ["blocked"] and "assignee" not in ticket  # nothing an agent would claim or be woken by
    assert _Board.posts[1][1] == {"body": "the summary\n"} and _Board.posts[1][2] == "golden-nightly"


@needs_bash
def test_nightly_passes_on_regressions_and_uses_the_given_ticket(tmp_path, board):
    r = _nightly(tmp_path, board, _fake_python(tmp_path, 1), SEEDBOX2_GOLDEN_TICKET="T9")
    assert r.returncode == 1
    assert [p[0] for p in _Board.posts] == ["/api/tickets/T9/comments"]


@needs_bash
def test_nightly_posts_the_log_when_the_run_dies_without_a_summary(tmp_path, board):
    fake = _fake_python(tmp_path, 3)
    fake.write_text(fake.read_text().replace("--summary", "--no-such-flag"))
    r = _nightly(tmp_path, board, fake, SEEDBOX2_GOLDEN_TICKET="T9")
    assert r.returncode == 3
    body = _Board.posts[0][1]["body"]
    assert "failed (exit status 3)" in body and "run log line" in body


@needs_bash
def test_nightly_says_so_when_the_board_is_down(tmp_path, board):
    _Board.fail = True
    r = _nightly(tmp_path, board, _fake_python(tmp_path, 0), SEEDBOX2_GOLDEN_TICKET="T9")
    assert r.returncode == 2 and "couldn't post" in r.stderr
    assert (tmp_path / "results/golden/nightly-summary.md").read_text() == "the summary\n"
