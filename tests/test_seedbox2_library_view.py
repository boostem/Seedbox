"""The library view (#73): table rows, key notation, overview waveforms, drag-and-drop into crates and sets."""

from __future__ import annotations

import json
import struct
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from seedbox2.analysis import waveform as wf
from seedbox2.library import Library, view
from seedbox2.library.hashing import ContentHash


@pytest.mark.parametrize(("key", "camelot"), [
    ("8A", "8A"), ("08a", "8A"), ("12B", "12B"), ("13A", None), ("Am", "8A"), ("A minor", "8A"), ("C", "8B"),
    ("C major", "8B"), ("F#m", "11A"), ("Gbm", "11A"), ("Dbmaj", "3B"), ("Bb", "6B"), ("Ebm", "2A"), ("1m", "8A"),
    ("1d", "8B"), ("6d", "1B"), ("", None), (None, None), ("xyz", None), ("A dorian", None),
])  # fmt: skip
def test_keys_in_any_notation_become_camelot_codes(key, camelot):
    assert view.to_camelot(key) == camelot


def _track(**tags: Any) -> dict:
    return {"hash": "h", "duration": 200.0, "title": "T", "artist": "A", "album": "", "source": "local",
            "added_at": "2026-11-04T00:00:00Z", "tags": tags}  # fmt: skip


def test_rows_use_our_analysis_and_fall_back_to_tag_hints():
    row = view.describe(_track(file={"bpm": "124", "key": "Am", "genre": "House", "comment": "warm deep"}))
    assert (row["bpm"], row["bpm_from"], row["key"], row["key_from"]) == (124.0, "tags", "8A", "tags")
    assert row["genre"] == "House" and "warm deep" in row["text"] and row["energy"] is None
    assert row["status"]["state"] == "new" and row["status"]["waveform"] is False

    row = view.describe(_track(file={"bpm": 124, "key": "Am"}, rekordbox={"bpm": 125.5, "key": "5A"}))
    assert (row["bpm"], row["key"]) == (125.5, "5A")  # Rekordbox's analysis beats the file's tags

    seedbox1 = _track(camelot="3B", bpm=174, energy_rating=7, tunebat={"energy": 0.8, "cover_url": "https://x/y.jpg"})
    row = view.describe(seedbox1)
    assert (row["bpm"], row["key"], row["energy"], row["artwork"]) == (174.0, "3B", 0.7, True)

    row = view.describe(_track(file={"bpm": 120, "key": "Am"}),
                        analyses={"beats": {"bpm": 121.98}, "key": {"camelot": "9A"}, "energy": {"energy": 0.42}},
                        artifacts=["waveform:v1"], waveform_version="v1")  # fmt: skip
    assert (row["bpm"], row["bpm_from"], row["key"], row["key_from"], row["energy"]) == (121.98, "analysis", "9A",
                                                                                        "analysis", 0.42)  # fmt: skip
    assert row["status"]["state"] == "new" and row["status"]["analyses"] == ["beats", "energy", "key"]  # not ready
    assert row["status"]["missing"] == ["sections", "vocals", "stems"]


ALL = ("beats", "key", "energy", "sections", "vocals")


def test_status_follows_the_jobs():
    def state(jobs: dict, arts=("waveform:v1",)) -> dict:
        return view.describe(_track(), artifacts=arts, jobs=jobs, waveform_version="v1", finished=ALL)["status"]

    assert state({}, arts=("waveform:v1", "stems:htdemucs"))["state"] == "done"
    assert state({})["state"] == "new"  # no stems: not ready yet
    assert state({}, arts=("waveform:v0",))["state"] == "new"  # an old version is not a waveform
    assert state({"stems": {"state": "queued", "progress": 0}})["state"] == "queued"
    s = state({"stems": {"state": "running", "progress": 0.4}, "beats": {"state": "queued", "progress": 0}})
    assert (s["state"], s["stems"], s["stems_progress"]) == ("analysing", "running", 0.4)
    assert state({"beats": {"state": "failed", "progress": 0}})["state"] == "failed"
    assert state({}, arts=("waveform:v1", "stems:htdemucs"))["stems"] is True


def test_ready_and_missing():
    def status(finished=(), arts=(), jobs=None, stems_required=True) -> dict:
        s = view.describe(_track(), artifacts=arts, jobs=jobs, waveform_version="v1", finished=finished,
                          stems_required=stems_required)["status"]  # fmt: skip
        return {k: s[k] for k in ("state", "ready", "missing")}

    every = ("waveform:v1", "stems:htdemucs")
    assert status() == {"state": "new", "ready": False,
                        "missing": ["beats", "key", "energy", "sections", "vocals", "waveform", "stems"]}  # fmt: skip
    light = status(ALL, ("waveform:v1",))  # the light analyses only: stems still to come
    assert light == {"state": "new", "ready": False, "missing": ["stems"]}
    assert status(ALL, every) == {"state": "done", "ready": True, "missing": []}
    assert status(ALL, ("waveform:v1",), stems_required=False) == {"state": "done", "ready": True, "missing": []}
    assert status(ALL[:-1], ("waveform:v1",), stems_required=False)["missing"] == ["vocals"]
    assert status(ALL, ("waveform:v0", "stems:x"))["missing"] == ["waveform"]  # an old waveform version
    failed = status(ALL, every, {"vocals": {"state": "failed", "progress": 0}})  # data from an older run, then it failed
    assert failed == {"state": "failed", "ready": False, "missing": ["vocals"]}
    failed = status(ALL[1:], every, {"beats": {"state": "failed", "progress": 0}})
    assert failed == {"state": "failed", "ready": False, "missing": ["beats"]}
    rerun = status(ALL, every, {"key": {"state": "running", "progress": 0.5}})  # still has the data it re-analyses
    assert rerun == {"state": "analysing", "ready": True, "missing": []}
    queued = status(ALL, ("waveform:v1",), {"stems": {"state": "queued", "progress": 0}})
    assert queued == {"state": "queued", "ready": False, "missing": ["stems"]}


@pytest.fixture
def lib(tmp_path):
    lib = Library.open(tmp_path / "library.sqlite3")
    yield lib
    lib.close()


def _add(lib: Library, tmp_path: Path, name: str, **meta: Any) -> str:
    f = tmp_path / name
    f.write_bytes(name.encode())
    return lib.add_file(f, content=ContentHash(name.ljust(40, "0"), 180.0), **meta)["hash"]


def test_view_rows_for_the_whole_library(lib, tmp_path):
    a = _add(lib, tmp_path, "a", title="Alpha", artist="X", tags={"file": {"bpm": 128, "key": "Fm"}})
    b = _add(lib, tmp_path, "b", title="Beta", artist="Y")
    lib.put_analysis(a, "beats", "1", {"bpm": 127.9, "beats": [0.1, 0.5]})
    lib.put_analysis(a, "beats", "2", {"bpm": 128.02})
    lib.put_artifact(a, "waveform", "v1", tmp_path / "a.sbwf.gz")
    old = lib.add_job("waveform", {"hash": b})
    lib.update_job(old["id"], state="failed", error="boom")
    lib.add_job("waveform", {"hash": b})  # retried: queued
    done = lib.add_job("beats", {"hash": a})
    lib.update_job(done["id"], state="done")
    lib.add_job("render", {"pair": [a, b]})  # not about one track

    rows = {r["hash"]: r for r in lib.view_rows(waveform_version="v1")}
    assert list(rows) == [a, b]
    assert (rows[a]["bpm"], rows[a]["bpm_from"], rows[a]["key"]) == (128.02, "analysis", "4A")
    assert rows[a]["status"] == {"state": "new", "ready": False, "missing": ["key", "energy", "sections", "vocals",
                                 "stems"], "waveform": True, "stems": False, "stems_progress": None,
                                 "analyses": ["beats"], "jobs": {}}  # fmt: skip
    assert rows[b]["status"]["state"] == "queued" and rows[b]["status"]["jobs"] == {"waveform": "queued"}

    for analyser in ALL:
        lib.put_analysis(a, analyser, "1", {})
    assert lib.view_rows(waveform_version="v1", hashes=[a])[0]["status"]["missing"] == ["stems"]
    assert lib.view_rows(waveform_version="v1", hashes=[a], stems_required=False)[0]["status"]["state"] == "done"
    lib.put_artifact(a, "stems", "htdemucs", tmp_path / "stems")
    status = lib.view_rows(waveform_version="v1", hashes=[a])[0]["status"]
    assert (status["state"], status["ready"], status["missing"]) == ("done", True, [])
    failed = lib.add_job("sections", {"hash": a})
    lib.update_job(failed["id"], state="failed", error="boom")
    status = lib.view_rows(waveform_version="v1", hashes=[a])[0]["status"]
    assert (status["state"], status["ready"], status["missing"]) == ("failed", False, ["sections"])


def test_the_required_analyses_are_the_analysers_names():
    from seedbox2.analysis import beats, energy, key, sections, vocals

    assert view.REQUIRED == ALL == tuple(m.ANALYSER for m in (beats, key, energy, sections, vocals))


def test_dragging_tracks_into_crates_and_sets(lib, tmp_path):
    a, b, c = (_add(lib, tmp_path, n) for n in "abc")
    crate = lib.save_crate("Warmup", [a])
    assert lib.add_to_crate(crate, [b, a, b, c]) == 2
    assert lib.crate_hashes(crate) == [a, b, c]
    with pytest.raises(KeyError):
        lib.add_to_crate(999, [a])
    with pytest.raises(ValueError, match="no track"):
        lib.add_to_crate(crate, ["nope"])
    assert lib.crate_hashes(crate) == [a, b, c]  # nothing half-written

    s = lib.save_set("Friday", [c])
    assert lib.add_to_set(s, [a, c]) == 2
    assert lib.set_hashes(s) == [c, a, c]  # a set may come back to a track
    assert lib.set_hashes(999) is None and lib.crate_hashes(999) is None


def test_overview_cache_and_packing(tmp_path):
    overview = np.zeros((3, 2, wf.OVERVIEW_COLUMNS), np.uint8)
    overview[0, 0, :] = 200  # low peaks
    overview[2, 0, 5] = 77
    w = wf.Waveform(1.0, np.zeros((3, 2, 150), np.uint8), overview)
    path = tmp_path / "x.sbwf.gz"
    wf.write(w, path)
    cache = view.OverviewCache(size=1)
    row = cache.get(path)
    assert row is not None and len(row) == 3 * wf.OVERVIEW_COLUMNS and row[0] == 200 and row[2 * 1200 + 5] == 77
    assert cache.get(path) is row  # cached
    assert cache.get(tmp_path / "missing.sbwf.gz") is None
    (tmp_path / "bad.sbwf.gz").write_bytes(b"not gzip")
    assert cache.get(tmp_path / "bad.sbwf.gz") is None

    data = view.pack_overviews([row, None], columns=wf.OVERVIEW_COLUMNS, bands=3)
    assert struct.unpack_from(view.OVERVIEW_HEADER, data) == (b"SBOV", 1200, 3, 2)
    assert data[12] == 1 and data[13:13 + 3600] == row and data[13 + 3600] == 0 and len(data) == 12 + 1 + 3600 + 1


# --- the API ---------------------------------------------------------------------------------------------------------


def _call(url: str, method: str = "GET", body: Any = None) -> tuple[int, bytes, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - localhost
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_the_library_view_api(tmp_path, monkeypatch):
    from seedbox2.server import start_server

    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(tmp_path / "music"))
    (tmp_path / "music").mkdir()
    lib = Library.open(tmp_path / "data" / "library.sqlite3")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    a = _add(lib, tmp_path, "a", title="Alpha", tags={"file": {"key": "Am"}, "artwork": str(cover)})
    b = _add(lib, tmp_path, "b", title="Beta")
    overview = np.full((3, 2, wf.OVERVIEW_COLUMNS), 100, np.uint8)
    wf.write(wf.Waveform(1.0, np.zeros((3, 2, 150), np.uint8), overview), wf.artifact_path(a))
    lib.put_artifact(a, "waveform", wf.MODEL_VERSION, wf.artifact_path(a))
    lib.close()
    handle = start_server(library_path=tmp_path / "data" / "library.sqlite3")
    api = handle.url + "/api"
    try:
        status, body, _ = _call(api + "/library/view")
        rows = json.loads(body)["items"]
        assert status == 200 and [r["title"] for r in rows] == ["Alpha", "Beta"] and rows[0]["key"] == "8A"
        assert [(r["status"]["state"], r["status"]["ready"]) for r in rows] == [("new", False), ("new", False)]
        assert rows[0]["status"]["missing"] == ["beats", "key", "energy", "sections", "vocals", "stems"]
        from seedbox2.library.service import SETTINGS_FILE, Settings

        Settings(tmp_path / "data" / SETTINGS_FILE).set("stems", {"enabled": False})  # stems off: not needed
        rows = json.loads(_call(api + "/library/view", "POST", {"hashes": [a]})[1])["items"]
        assert rows[0]["status"]["missing"] == ["beats", "key", "energy", "sections", "vocals"]
        status, body, _ = _call(api + "/library/view", "POST", {"hashes": [b]})
        assert status == 200 and [r["hash"] for r in json.loads(body)["items"]] == [b]

        status, data, headers = _call(api + "/library/overviews", "POST", {"hashes": [b, a]})
        assert status == 200 and headers["Content-Type"] == "application/octet-stream"
        assert data[:4] == b"SBOV" and data[12] == 0 and data[13] == 1 and set(data[14:]) == {100}
        assert _call(api + "/library/overviews", "POST", {"hashes": ["x"] * 501})[0] == 422

        status, data, headers = _call(api + f"/tracks/{a}/artwork")
        assert status == 200 and headers["Content-Type"] == "image/png" and data == cover.read_bytes()
        assert _call(api + f"/tracks/{b}/artwork")[0] == 404

        status, body, _ = _call(api + "/library/crates", "POST", {"name": "Peak time", "hashes": [a]})
        crate = json.loads(body)
        assert status == 201 and [t["hash"] for t in crate["tracks"]] == [a]
        status, body, _ = _call(api + f"/library/crates/{crate['id']}/tracks", "POST", {"hashes": [b, a]})
        assert status == 200 and json.loads(body) == {"added": 1, "hashes": [a, b]}
        assert _call(api + "/library/crates/999/tracks", "POST", {"hashes": [a]})[0] == 404
        assert _call(api + f"/library/crates/{crate['id']}/tracks", "POST", {"hashes": ["nope"]})[0] == 422
        assert json.loads(_call(api + f"/library/crates/{crate['id']}/hashes")[1]) == {"hashes": [a, b]}

        status, body, _ = _call(api + "/sets", "POST", {"name": "Friday"})
        set_id = json.loads(body)["id"]
        assert json.loads(_call(api + f"/sets/{set_id}/tracks", "POST", {"hashes": [b, a]})[1])["hashes"] == [b, a]
        assert json.loads(_call(api + f"/sets/{set_id}/hashes")[1]) == {"hashes": [b, a]}

        assert json.loads(_call(api + "/library/smart-crates")[1]) == {"crates": []}
        smart = [{"id": "s1", "name": "Warm 120-124", "filter": {"bpmMin": 120, "bpmMax": 124}}]
        assert _call(api + "/library/smart-crates", "PUT", {"crates": smart})[0] == 200
        assert json.loads(_call(api + "/library/smart-crates")[1]) == {"crates": smart}
        assert _call(api + "/library/smart-crates", "PUT", {"crates": [{"name": 3}]})[0] == 422
    finally:
        handle.stop()


def test_view_rows_of_some_tracks(lib, tmp_path):
    hashes = [_add(lib, tmp_path, f"t{n}", title=f"T{n}") for n in range(5)]
    lib.add_job("waveform", {"hash": hashes[3]})
    rows = lib.view_rows(hashes=[hashes[3], hashes[1], "unknown"])
    assert sorted(r["hash"] for r in rows) == sorted([hashes[1], hashes[3]])
    assert {r["hash"]: r["status"]["state"] for r in rows}[hashes[3]] == "queued"
    assert lib.view_rows(hashes=[]) == []
    assert len(lib.view_rows(hashes=hashes * 400)) == 5  # past SQLite's parameter limit
