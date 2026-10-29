"""seedbox2.eval: loaders, annotations, metrics and the `analysis` command, on fixture files and a synthetic track."""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from seedbox2.eval import annotate, datasets, metrics, run
from seedbox2.eval.analysers import Estimate, grid, spans
from seedbox2.eval.datasets import Reference, normalise_key

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(("text", "key"), [
    ("A minor", "A minor"), ("Am", "A minor"), ("F#m", "Gb minor"), ("Bbm", "Bb minor"), ("Eb", "Eb major"),
    ("c min", "C minor"), ("Bmaj", "B major"), ("8A", "A minor"), ("8B", "C major"), ("12B", "E major"),
    ("", None), ("-", None), ("X minor", None), ("C dorian", None),
])
def test_normalise_key(text, key):
    assert normalise_key(text) == key


def test_grid_reaches_back_to_zero_and_marks_downbeats():
    beats, downbeats = grid(1.25, 120.0, 5.0)
    assert beats == [0.25, 0.75, 1.25, 1.75, 2.25, 2.75, 3.25, 3.75, 4.25, 4.75]
    assert downbeats == [1.25, 3.25]


def test_spans_merge_neighbours():
    on = np.array([True, True, False, True])
    starts, ends = np.array([0.0, 2, 4, 6]), np.array([2.0, 4, 6, 8])
    assert spans(on, starts, ends) == [(0.0, 4.0), (6.0, 8.0)]


def _ref(**kw):
    return Reference("t", "x", Path("x.wav"), **kw)


def test_perfect_estimate_scores_100():
    beats, downbeats = grid(0.5, 125.0, 120.0)
    sections = [(0.5, "intro"), (31.22, "body"), (61.94, "breakdown"), (100.34, "end")]
    ref = _ref(bpm=125.0, key="A minor", beats=beats, downbeats=downbeats, sections=sections,
               vocals=[(31.22, 61.94)], duration_s=120.0)
    est = Estimate(120.0, bpm=125.0, key="A minor", beats=beats, downbeats=downbeats, sections=sections,
                   vocals=[(31.22, 61.94)])
    assert metrics.score(ref, est) == {"beat_f": 100.0, "downbeat_f": 100.0, "tempo_acc1": 100.0,
                                       "tempo_acc2": 100.0, "octave_err": 0.0, "key_score": 100.0,
                                       "boundary_f": 100.0, "vocal_f": 100.0}


def test_tempo_octave_errors():
    assert metrics.tempo_scores(174.0, 87.0) == {"tempo_acc1": 0.0, "tempo_acc2": 1.0, "octave_err": 1.0}
    assert metrics.tempo_scores(174.0, 116.0) == {"tempo_acc1": 0.0, "tempo_acc2": 0.0, "octave_err": 0.0}
    assert metrics.tempo_scores(128.0, 130.0)["tempo_acc1"] == 1.0  # within 4 %


def test_key_fifth_and_relative():
    assert metrics.score(_ref(key="A minor"), Estimate(10, key="E minor")) == {"key_score": 50.0}
    assert metrics.score(_ref(key="A minor"), Estimate(10, key="C major")) == {"key_score": 30.0}


def test_boundaries_within_one_bar_hit():
    ref = _ref(bpm=120.0, sections=[(0.0, "a"), (32.0, "b"), (64.0, "c"), (96.0, "end")], duration_s=100)
    near = Estimate(100, sections=[(0.0, "a"), (33.5, "b"), (64.2, "c"), (96.0, "end")])  # bar = 2 s
    far = Estimate(100, sections=[(0.0, "a"), (36.0, "b"), (64.2, "c"), (96.0, "end")])
    assert metrics.score(ref, near)["boundary_f"] == 100.0
    assert metrics.score(ref, far)["boundary_f"] == 50.0


def test_vocal_frames():
    assert metrics.vocal_f([(0, 10)], [(5, 15)], 20) == pytest.approx(0.5)
    assert metrics.vocal_f([], [], 20) == 1.0  # an instrumental called instrumental
    assert metrics.vocal_f([], [(0, 1)], 20) == 0.0


def test_missing_fields_are_not_scored():
    assert metrics.score(_ref(bpm=120.0, key="C major"), Estimate(10, bpm=None, key=None)) == {}


# ---------------------------------------------------------------------------------------------------------------
# loaders


def _touch(p: Path, text: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_giantsteps_loaders(tmp_path):
    t = tmp_path / "gst"
    _touch(t / "annotations/tempo/1.LOFI.bpm", "126")
    _touch(t / "annotations_v2/tempo/1.LOFI.bpm", "127.0")
    _touch(t / "annotations/tempo/2.LOFI.bpm", "140")  # no audio: skipped
    _touch(t / "annotations/tempo/3.LOFI.bpm", "100")
    _touch(t / "annotations_v2/tempo/3.LOFI.bpm", "0.0")  # no steady tempo: skipped
    _touch(t / "audio/1.LOFI.mp3")
    _touch(t / "audio/3.LOFI.mp3")
    refs = datasets.load("giantsteps-tempo", t)
    assert [(r.track, r.bpm) for r in refs] == [("1.LOFI", 127.0)]

    k = tmp_path / "gsk"
    _touch(k / "annotations/key/9.LOFI.key", "Eb minor")
    _touch(k / "audio/9.LOFI.mp3")
    assert [(r.track, r.key) for r in datasets.load("giantsteps-key", k)] == [("9.LOFI", "Eb minor")]


def test_harmonix_loader(tmp_path, monkeypatch):
    h = tmp_path / "hx"
    _touch(h / "dataset/beats_and_downbeats/0001_x.txt", "0.0\t1\t1\n0.5\t2\t1\n1.0\t3\t1\n1.5\t4\t1\n2.0\t1\t2\n")
    _touch(h / "dataset/segments/0001_x.txt", "0.0 intro\n8.5 verse\n20.0 end\n")
    _touch(h / "dataset/metadata.csv", "File,Title,BPM\n0001_x,X,120\n")
    _touch(h / "dataset/beats_and_downbeats/0002_y.txt", "0.0\t1\t1\n")
    audio = tmp_path / "mine"
    _touch(audio / "0001_x.flac")
    monkeypatch.setenv("SEEDBOX2_HARMONIX_AUDIO", str(audio))
    [r] = datasets.load("harmonix", h)
    assert (r.track, r.bpm, r.downbeats) == ("0001_x", 120.0, [0.0, 2.0])
    assert r.beats == [0.0, 0.5, 1.0, 1.5, 2.0]
    assert r.sections == [(0.0, "intro"), (8.5, "verse"), (20.0, "end")]


def test_corpus_loader_and_annotation_roundtrip(tmp_path):
    beats, downbeats = grid(0.42, 126.0, 60.0)
    est = Estimate(60.0, bpm=126.0, key="A minor", beats=beats, downbeats=downbeats,
                   sections=[(0.42, "intro"), (30.9, "body"), (58.0, "end")], vocals=[(30.9, 45.0)],
                   cues=[(0.42, "first downbeat")])
    ann = annotate.to_annotation(est)
    assert ann["grid"] == {"first_downbeat_s": 0.42, "bpm": 126.0} and "beats" not in ann
    (tmp_path / "sub").mkdir()
    annotate.write_annotation(tmp_path / "sub/song.json", ann)
    _touch(tmp_path / "sub/song.wav")
    _touch(tmp_path / "orphan.json", "{}")  # no audio: skipped
    [ref] = datasets.load("corpus", tmp_path)
    assert ref.track == "sub/song" and ref.audio.name == "song.wav"
    assert metrics.score(ref, est)["beat_f"] == 100.0
    assert (ref.key, ref.vocals, ref.extra) == ("A minor", [(30.9, 45.0)], {"verified": False})


def test_rekordbox_roundtrip(tmp_path):
    audio = tmp_path / "a b.mp3"
    _touch(audio)
    beats, downbeats = grid(1.0, 128.0, 90.0)
    ann = annotate.to_annotation(Estimate(90.0, bpm=128.0, key="F# minor", beats=beats, downbeats=downbeats,
                                          sections=[(1.0, "intro"), (31.0, "drop")]))
    xml = annotate.to_rekordbox([(audio, ann)])
    assert "Tonality=\"Gbm\"" in xml and "a%20b.mp3" in xml
    # the DJ moves the grid to the beat 2 mark at 1.2 s and the drop cue by a bar
    fixed = xml.replace('Inizio="1.000"', 'Inizio="1.669"').replace('Battito="1"', 'Battito="2"') \
        .replace('Start="31.000"', 'Start="32.875"')
    [(path, fix)] = annotate.from_rekordbox(fixed).items()
    assert path == audio.resolve()
    assert fix["grid"] == {"first_downbeat_s": round(1.669 + 3 * 60 / 128, 4), "bpm": 128.0}
    assert fix["key"] == "Gb minor"
    assert fix["sections"] == [{"start_s": 1.0, "label": "intro"}, {"start_s": 32.875, "label": "drop"}]
    merged = annotate.merge(ann, fix)
    assert merged["grid"] == fix["grid"] and "beats" not in merged and merged["vocals"] is None


def test_rekordbox_piecewise_grid():
    xml = """<DJ_PLAYLISTS><COLLECTION><TRACK Location="file://localhost/x.mp3" TotalTime="4">
      <TEMPO Inizio="0.0" Bpm="120" Battito="1"/><TEMPO Inizio="2.0" Bpm="60" Battito="1"/></TRACK></COLLECTION>
      </DJ_PLAYLISTS>"""
    [fix] = annotate.from_rekordbox(xml).values()
    assert fix["beats"] == [0.0, 0.5, 1.0, 1.5, 2.0, 3.0] and fix["downbeats"] == [0.0, 2.0]


# ---------------------------------------------------------------------------------------------------------------
# runs


def _summary(**metrics_):
    return {"summary": {"gs": {"seedbox1": {"tracks": 10, "errors": 0, **metrics_}}}}


def test_regressions_over_the_threshold_only():
    base = _summary(tempo_acc1=60.0, octave_err=2.0, beat_f=80.0)
    ok = _summary(tempo_acc1=58.0, octave_err=4.0, beat_f=90.0)["summary"]
    assert run.regressions(ok, base, 2.0) == []
    bad = _summary(tempo_acc1=57.9, octave_err=4.5)["summary"]
    assert run.regressions(bad, base, 2.0) == [
        "gs/seedbox1 beat_f: no longer scored (baseline 80.0)",
        "gs/seedbox1 tempo_acc1: 57.9, baseline 60.0 (+2.1 points worse)",
        "gs/seedbox1 octave_err: 4.5, baseline 2.0 (+2.5 points worse)",
    ]


def test_table_shows_dashes_for_unscored_metrics():
    rows = [{"dataset": "gs", "track": "a", "analyser": "x", "metrics": {"beat_f": 80.0}},
            {"dataset": "gs", "track": "b", "analyser": "x", "error": "ValueError: bad"},
            {"dataset": "gs", "track": "a", "analyser": "y", "metrics": {"key_score": 50.0}}]
    text = run.table(run.summarise(rows))
    assert text.splitlines() == ["gs", "  analyser     tracks  beat_f  key_score",
                                 "  x         2 (1 err)    80.0          —",
                                 "  y                 1       —       50.0"]


def _click_track(path: Path, bpm: float = 128.0, seconds: float = 40.0, sr: int = 22050) -> None:
    """A four-on-the-floor kick with off-beat hats, starting on a downbeat at 0.5 s."""
    x = np.zeros(int(seconds * sr))
    period = 60.0 / bpm
    t = np.arange(int(0.15 * sr)) / sr
    kick = np.sin(2 * np.pi * (50 + 100 * np.exp(-t * 30)) * t) * np.exp(-t * 18)
    hat = np.random.default_rng(0).standard_normal(int(0.03 * sr)) * np.exp(-np.arange(int(0.03 * sr)) / 80)
    for i in range(int((seconds - 1) / period)):
        a = int((0.5 + i * period) * sr)
        x[a:a + kick.size] += kick * (1.0 if i % 4 == 0 else 0.8)
        h = int((0.5 + (i + 0.5) * period) * sr)
        x[h:h + hat.size] += 0.2 * hat
    wavfile.write(path, sr, (x / np.max(np.abs(x)) * 0.8 * 32767).astype(np.int16))


def test_analysis_command_on_a_synthetic_corpus(tmp_path):
    pytest.importorskip("musicreco.structure")
    from musicreco.paths import ffmpeg_path
    if ffmpeg_path() is None:
        pytest.skip("no ffmpeg")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _click_track(corpus / "click.wav")
    beats, downbeats = grid(0.5, 128.0, 40.0)
    annotate.write_annotation(corpus / "click.json", {"format": annotate.FORMAT, "duration_s": 40.0, "bpm": 128.0,
                                                      "grid": {"first_downbeat_s": 0.5, "bpm": 128.0}})
    out = tmp_path / "run.json"
    base = tmp_path / "base.json"
    cmd = [sys.executable, "-m", "seedbox2.eval", "analysis", "--dataset", f"corpus={corpus}", "--jobs", "1",
           "--out", str(out)]
    res = subprocess.run([*cmd, "--write-baseline", str(base)], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "corpus" in res.stdout and "seedbox1" in res.stdout
    record = json.loads(out.read_text())
    s = record["summary"]["corpus"]["seedbox1"]
    assert s["tracks"] == 1 and s["errors"] == 0
    assert s["tempo_acc1"] == 100.0 and s["beat_f"] >= 90.0
    assert record["tracks"][0]["estimate"]["bpm"] == pytest.approx(128.0, abs=0.5)
    assert "tracks" not in json.loads(base.read_text())

    # a baseline 5 points better than this run fails it
    worse = json.loads(base.read_text())
    worse["summary"]["corpus"]["seedbox1"]["beat_f"] = min(100.0, s["beat_f"] + 5) if s["beat_f"] < 96 else 105.0
    base.write_text(json.dumps(worse))
    res = subprocess.run([*cmd, "--baseline", str(base)], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 1 and "beat_f" in res.stderr

    # annotate writes an editable file next to the audio and keeps an existing one
    (corpus / "click.json").unlink()
    res = subprocess.run([sys.executable, "-m", "seedbox2.eval", "annotate", str(corpus / "click.wav"),
                          "--rekordbox", str(tmp_path / "rb.xml")], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    ann = json.loads((corpus / "click.json").read_text())
    assert ann["verified"] is False and ann["grid"]["bpm"] == pytest.approx(128.0, abs=0.5)
    assert "POSITION_MARK" in (tmp_path / "rb.xml").read_text()
