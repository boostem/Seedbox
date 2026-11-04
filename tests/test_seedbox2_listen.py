"""The blind listening test (#86): which renders get compared, what the client may see, the summary, pooling."""

from __future__ import annotations

import json
import random
import wave
from pathlib import Path

import numpy as np
import pytest

from seedbox2.eval import listening as ls


def _wav(path: Path, value: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(int(value).to_bytes(2, "little", signed=True) * 800)
    return path


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    f = tmp_path / ls.FOLDER
    _wav(f / "one" / "seedbox1-0.3.0--bass swap.wav", 1)
    _wav(f / "one" / "seedbox2-2.0.0--bass swap.wav", 2)
    _wav(f / "two" / "seedbox1-0.3.0--echo out.wav", 3)
    _wav(f / "two" / "human--DJ Someone.wav", 4)
    _wav(f / "alone" / "seedbox1-0.3.0.wav", 5)  # one render: nothing to compare it with
    return f


def _row(pair: str, a: tuple[str, str], b: tuple[str, str], choice: str, rater: str = "sam", scores=None, uid=None) -> dict:
    return {"subject": pair, "choice": choice, "created_at": "2026-11-04T12:00:00Z", "data": {
        "uid": uid or f"{pair}{a}{b}{choice}{rater}{random.random()}", "rater": rater,
        "a": {"version": a[0], "style": a[1]}, "b": {"version": b[0], "style": b[1]},
        "scores": scores or {}, "comment": ""}}  # fmt: skip


def test_scan_reads_version_and_style_from_the_file_name(folder: Path):
    (folder / "one" / "notes.txt").write_text("not audio")
    _wav(folder / "one" / ".hidden.wav")
    _wav(folder / "one" / "seedbox1-0.3.0--bass swap.mp3")  # the same render again
    versions = ls.scan(folder)
    assert {(v.pair, v.version, v.style) for v in versions} == {
        ("alone", "seedbox1-0.3.0", ""), ("one", "seedbox1-0.3.0", "bass swap"), ("one", "seedbox2-2.0.0", "bass swap"),
        ("two", "seedbox1-0.3.0", "echo out"), ("two", "human", "DJ Someone")}  # fmt: skip
    assert len(ls.comparisons(versions)) == 2
    assert ls.scan(folder / "missing") == []


def test_pick_goes_for_the_least_heard_comparison_and_shuffles(folder: Path):
    versions = ls.scan(folder)
    rated = [_row("one", ("seedbox1-0.3.0", "bass swap"), ("seedbox2-2.0.0", "bass swap"), "a", "sam")]
    for seed in range(20):
        a, b = ls.pick(versions, rated, "sam", random.Random(seed))
        assert a.pair == b.pair == "two"  # sam has done "one"
    assert ls.progress(versions, rated, "sam") == {"done": 1, "total": 2}
    assert ls.progress(versions, rated, "ali") == {"done": 0, "total": 2}

    rated.append(_row("two", ("human", "DJ Someone"), ("seedbox1-0.3.0", "echo out"), "b", "ali"))
    for seed in range(20):
        assert ls.pick(versions, rated, "ali", random.Random(seed))[0].pair == "one"  # ali's first answer was "two"

    first = {ls.pick(versions, [], "sam", random.Random(seed))[0].version for seed in range(40)}
    assert {"seedbox1-0.3.0", "seedbox2-2.0.0", "human"} <= first  # either side can play first


def test_pick_has_nothing_without_two_renders_of_a_pair(tmp_path: Path):
    _wav(tmp_path / "alone" / "seedbox1.wav")
    assert ls.pick(ls.scan(tmp_path), []) is None


@pytest.mark.parametrize(("wins", "n", "low", "high"), [
    (5, 10, 0.2366, 0.7634), (0, 5, 0.0, 0.4345), (5, 5, 0.5655, 1.0), (80, 100, 0.7112, 0.8666), (0, 0, 0.0, 1.0),
])  # fmt: skip
def test_wilson_interval_matches_the_published_formula(wins, n, low, high):
    assert ls.wilson(wins, n) == pytest.approx((low, high), abs=1e-3)


def test_summary_counts_a_trial_only_where_its_renders_differ():
    s1, s2, human = "seedbox1-0.3.0", "seedbox2-2.0.0", "human"
    rows = [
        _row("p", (s1, "bass swap"), (s2, "bass swap"), "b", scores={"a": {"smoothness": 2}, "b": {"smoothness": 4}}),
        _row("p", (s2, "bass swap"), (s1, "bass swap"), "a", rater="ali"),
        _row("q", (s2, "echo out"), (human, "DJ"), "b"),
        _row("q", (s1, "echo out"), (s1, "bass swap"), "a"),  # same version: a style result only
        _row("r", (s2, ""), (s1, ""), "a"),
    ]
    out = ls.summary(rows)
    assert out["ratings"] == 5 and out["raters"] == 2
    versions = {v["name"]: v for v in out["versions"]}
    assert (versions[s2]["wins"], versions[s2]["trials"]) == (3, 4)
    assert (versions[s1]["wins"], versions[s1]["trials"]) == (0, 3)
    assert (versions[human]["wins"], versions[human]["trials"]) == (1, 1)
    assert versions[s2]["win_rate"] == 0.75
    assert (versions[s2]["ci_low"], versions[s2]["ci_high"]) == pytest.approx(ls.wilson(3, 4), abs=1e-4)
    assert versions[s2]["scores"] == {"smoothness": 4.0} and versions[s1]["scores"] == {"smoothness": 2.0}
    assert out["versions"][0]["name"] == human  # sorted by win rate

    styles = {v["name"]: v for v in out["styles"]}
    assert set(styles) == {"echo out", "DJ", "bass swap"}  # "" against "" compares no styles
    assert (styles["echo out"]["wins"], styles["echo out"]["trials"]) == (1, 2)
    assert (styles["bass swap"]["wins"], styles["bass swap"]["trials"]) == (0, 1)

    matchups = {(m["name"], m["opponent"]): m for m in out["matchups"]}
    assert (matchups[(s1, s2)]["wins"], matchups[(s1, s2)]["trials"]) == (0, 3)
    assert (matchups[(human, s2)]["wins"], matchups[(human, s2)]["trials"]) == (1, 1)
    assert (out["slot_a"]["wins"], out["slot_a"]["trials"]) == (3, 5)


def test_summary_of_nothing():
    out = ls.summary([])
    assert out["versions"] == [] and out["styles"] == [] and out["matchups"] == []
    assert out["slot_a"]["win_rate"] is None


@pytest.mark.parametrize("doc", [
    None, [], {"format": "other"}, {"format": ls.FORMAT, "version": 99, "ratings": []}, {"format": ls.FORMAT, "version": 1},
    {"format": ls.FORMAT, "version": 1, "ratings": ["x"]},
])  # fmt: skip
def test_import_rejects_what_is_not_a_ratings_file(doc):
    with pytest.raises(ValueError):
        ls.parse_import(doc, [])


@pytest.mark.parametrize("change", [
    lambda r: r.update(choice="c"),
    lambda r: r.update(subject=""),
    lambda r: r.update(created_at="yesterday"),
    lambda r: r["data"].pop("uid"),
    lambda r: r["data"].pop("a"),
    lambda r: r["data"]["a"].update(version=""),
    lambda r: r["data"].update(scores={"a": {"smoothness": 6}}),
    lambda r: r["data"].update(scores={"a": {"smoothness": True}}),
    lambda r: r["data"].update(scores={"a": {"taste": 3}}),
    lambda r: r["data"].update(scores={"c": {"energy": 3}}),
    lambda r: r["data"].update(comment=5),
])  # fmt: skip
def test_import_is_all_or_nothing_on_a_bad_rating(change):
    good, bad = _row("p", ("x", ""), ("y", ""), "a"), _row("p", ("x", ""), ("y", ""), "a")
    change(bad)
    with pytest.raises(ValueError, match="rating 2"):
        ls.parse_import({"format": ls.FORMAT, "version": 1, "ratings": [good, bad]}, [])


# --- the API -----------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(folder: Path):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    with TestClient(create_app(library_path=folder.parent / "library.sqlite3")) as c:
        yield c


def _answer(client, trial_id: str, choice: str = "a", **body) -> dict:
    r = client.post(f"/api/listen/trials/{trial_id}/rating", json={"choice": choice, **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_nothing_the_client_gets_before_answering_names_the_render(client):
    trial = client.get("/api/listen/next", params={"rater": "sam"}).json()
    assert set(trial) == {"id", "pair", "progress"}
    assert trial["pair"] in ("one", "two")
    assert trial["progress"] == {"done": 0, "total": 2}
    for slot in "ab":
        r = client.get(f"/api/listen/trials/{trial['id']}/{slot}")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
        assert r.headers["cache-control"] == "no-store"
        assert "content-disposition" not in r.headers
        for text in ("seedbox", "human", "swap", "echo", "DJ"):
            assert text not in json.dumps(trial) and text not in str(r.headers)
    status = client.get("/api/listen/status").json()
    assert (status["pairs"], status["renders"], status["comparisons"], status["ratings"]) == (3, 5, 2, 0)
    for text in ("seedbox1", "seedbox2", "human"):
        assert text not in json.dumps({k: v for k, v in status.items() if k != "folder"})
    assert client.get(f"/api/listen/trials/{trial['id']}/c").status_code == 422
    assert client.get("/api/listen/trials/nope/a").status_code == 404


def test_audio_seeks(client):
    trial = client.get("/api/listen/next").json()
    r = client.get(f"/api/listen/trials/{trial['id']}/a", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10


def test_a_rating_lands_in_the_ratings_table_with_what_was_played(client, folder: Path):
    from seedbox2.library import Library

    trial = client.get("/api/listen/next").json()
    a = client.get(f"/api/listen/trials/{trial['id']}/a").content
    assert _answer(client, trial["id"], "b", rater=" sam ", comment=" nice bass ", scores={
        "a": {"smoothness": 2, "energy": 3, "dj": 1}, "b": {"smoothness": 5}})["ratings"] == 1  # fmt: skip
    # one answer per trial, and the trial is over
    assert client.post(f"/api/listen/trials/{trial['id']}/rating", json={"choice": "a"}).status_code == 404
    assert client.get(f"/api/listen/trials/{trial['id']}/a").status_code == 404

    client.close()
    lib = Library.open(folder.parent / "library.sqlite3")
    try:
        (row,) = lib.ratings(ls.TEST)
    finally:
        lib.close()
    assert row["subject"] == trial["pair"] and row["choice"] == "b"
    data = row["data"]
    assert data["rater"] == "sam" and data["comment"] == "nice bass" and data["uid"]
    assert data["scores"] == {"a": {"smoothness": 2, "energy": 3, "dj": 1}, "b": {"smoothness": 5}}
    # the row says what slot A really was
    name = ls.label(data["a"]["version"], data["a"]["style"])
    assert (folder / trial["pair"] / f"{name}.wav").read_bytes() == a


@pytest.mark.parametrize("body", [
    {}, {"choice": "c"}, {"choice": "a", "scores": {"a": {"smoothness": 0}}}, {"choice": "a", "scores": {"a": {"energy": 6}}},
    {"choice": "a", "comment": "x" * (ls.MAX_TEXT + 1)},
])  # fmt: skip
def test_a_bad_rating_does_not_end_the_trial(client, body):
    trial = client.get("/api/listen/next").json()
    assert client.post(f"/api/listen/trials/{trial['id']}/rating", json=body).status_code == 422
    assert client.get(f"/api/listen/trials/{trial['id']}/a").status_code == 200
    assert client.get("/api/listen/status").json()["ratings"] == 0


def test_no_renders_is_a_404_the_page_can_explain(tmp_path: Path):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    with TestClient(create_app(library_path=tmp_path / "library.sqlite3")) as c:
        status = c.get("/api/listen/status").json()
        assert status["comparisons"] == 0 and status["folder"] == str(tmp_path / ls.FOLDER)
        assert c.get("/api/listen/next").status_code == 404
        assert c.get("/api/listen/summary").json()["ratings"] == 0


def test_summary_export_and_import_pool_several_peoples_results(client, folder: Path, tmp_path: Path):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    for _ in range(3):
        _answer(client, client.get("/api/listen/next", params={"rater": "sam"}).json()["id"], rater="sam",
                scores={"a": {"dj": 4}})  # fmt: skip
    exported = client.get("/api/listen/export").json()
    assert exported["format"] == ls.FORMAT and len(exported["ratings"]) == 3
    assert str(folder) not in json.dumps(exported)  # no paths in a file people pass around

    # a friend's machine, with ratings of its own
    with TestClient(create_app(library_path=tmp_path / "friend" / "library.sqlite3")) as friend:
        rows = [_row("one", ("seedbox1-0.3.0", ""), ("seedbox2-2.0.0", ""), "b", "ali")]
        doc = {"format": ls.FORMAT, "version": 1, "ratings": rows}
        assert friend.post("/api/listen/import", json=doc).json() == {"added": 1, "duplicates": 0, "ratings": 1}
        assert friend.post("/api/listen/import", json=exported).json() == {"added": 3, "duplicates": 0, "ratings": 4}
        assert friend.post("/api/listen/import", json=exported).json() == {"added": 0, "duplicates": 3, "ratings": 4}
        assert friend.get("/api/listen/summary").json()["raters"] == 2
        pooled = friend.get("/api/listen/export").json()
    assert [r["created_at"] for r in pooled["ratings"][1:]] == [r["created_at"] for r in exported["ratings"]]

    assert client.post("/api/listen/import", json={"format": "other"}).status_code == 400
    assert client.post("/api/listen/import", json=pooled).json() == {"added": 1, "duplicates": 3, "ratings": 4}
    assert client.get("/api/listen/summary").json()["raters"] == 2


def test_only_listening_ratings_are_counted(client, folder: Path):
    from seedbox2.library import Library

    lib = Library.open(folder.parent / "other.sqlite3")
    try:
        lib.add_rating("other-test", "x", "y")
        lib.add_ratings(ls.TEST, [_row("p", ("x", ""), ("y", ""), "a")])
        assert len(lib.ratings(ls.TEST)) == 1 and len(lib.ratings()) == 2
    finally:
        lib.close()


# --- rendering ---------------------------------------------------------------------------------------------------------


def test_the_listen_command_writes_what_the_page_plays(tmp_path: Path, monkeypatch, capsys):
    from seedbox2.eval import mixes
    from seedbox2.eval.mixmetrics import Render

    tracks = [mixes.Pair("house: into house", tmp_path / "a.wav", tmp_path / "b.wav")]
    monkeypatch.setattr(mixes, "synthetic_pairs", lambda folder: tracks)
    forced = []

    def fake(pair):
        forced.append(pair.technique)
        if pair.technique == "broken":
            raise ValueError("no such technique")
        mix = np.array([[2.0, -2.0, 0.5], [0.0, 0.0, 0.0]])
        return Render(mix=mix, decks={}, sr=8000, plan={"technique": pair.technique or "bass swap"})

    def unavailable(pair):
        raise mixes.EngineUnavailable("not yet")

    monkeypatch.setitem(mixes.ENGINES, "seedbox1", fake)
    monkeypatch.setitem(mixes.ENGINES, "seedbox2", unavailable)
    out = tmp_path / "out"
    assert ls.main(["--folder", str(out), "--techniques", "echo out, broken"]) == 0
    assert forced == [None, "echo out", "broken"]
    versions = ls.scan(out)
    assert sorted(v.style for v in versions) == ["bass swap", "echo out"]
    assert {v.pair for v in versions} == {"house- into house"}  # a name that works as a folder everywhere
    assert all(v.version.startswith("seedbox1-") for v in versions)
    from scipy.io import wavfile

    sr, pcm = wavfile.read(next(v.path for v in versions))
    assert sr == 8000 and pcm.dtype == np.int16 and pcm.max() == 32767 and pcm.min() == -32767
    assert "skipping seedbox2" in capsys.readouterr().err

    monkeypatch.setitem(mixes.ENGINES, "seedbox1", unavailable)
    assert ls.main(["--folder", str(tmp_path / "none")]) == 1  # nothing written is a failure


def test_an_export_saved_to_a_file_is_importable_and_is_not_taken_for_a_render(client, folder: Path):
    _answer(client, client.get("/api/listen/next").json()["id"])
    before = client.get("/api/listen/status").json()["renders"]
    saved = client.post("/api/listen/export/save", json={"rater": "sam/../x"}).json()
    path = Path(saved["path"])
    assert saved["ratings"] == 1 and path.parent == folder and path.name.startswith("ratings-sam-")
    assert client.get("/api/listen/status").json()["renders"] == before
    assert client.post("/api/listen/import", json=json.loads(path.read_text())).json()["duplicates"] == 1
