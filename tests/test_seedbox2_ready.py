"""Only ready tracks go into sets and mixes (#99): every endpoint that builds, orders, plans, plays or exports a set
refuses a track that isn't ready with a 409 that says which and what it lacks; a failed track can be used anyway; one
track still plays on its own."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seedbox2.library import Library, ready
from seedbox2.server.app import create_app

from .s2_ready import make_ready
from .test_seedbox2_sets import _analysed, _Ctx


@pytest.fixture
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(tmp_path / "music"))
    app = create_app(library_path=tmp_path / "lib.sqlite3", start_jobs=False)
    with TestClient(app) as client:
        client.get("/api/sets")
        lib = app.state.library
        a, b, d = _analysed(lib, "a"), _analysed(lib, "b", bpm=126.0), _analysed(lib, "d", bpm=125.0)  # ready
        c = "c" * 40
        lib.upsert_track(c, 300.0, title="Still going", artist="X")  # just downloaded: nothing analysed yet
        lib.put_analysis(c, "beats", "old", {"bpm": 125.0})  # an old version: not this one
        song = tmp_path / "c.wav"
        song.write_bytes(b"RIFF")
        with lib.db.write() as w:
            w.execute("INSERT INTO track_paths (path, hash, size, mtime_ns) VALUES (?, ?, 1, 1)", (str(song), c))
        yield client, lib, a, b, c, d


def refused(r, *hashes: str) -> list[dict]:
    """The 409's tracks, checked to be these."""
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert [t["hash"] for t in detail["tracks"]] == list(hashes)
    assert "isn't ready to mix" in detail["message"] or "aren't ready to mix" in detail["message"]
    return detail["tracks"]


def fail(lib: Library, hash_: str, kind: str = "beats") -> None:
    job = lib.add_job(kind, {"hash": hash_})
    lib.update_job(job["id"], state="failed", error="boom")


def test_the_error_names_the_track_and_what_it_lacks(fx):
    client, lib, a, b, c, _ = fx
    tracks = refused(client.post("/api/sets", json={"name": "S", "hashes": [a, c]}), c)
    assert tracks[0]["title"] == "Still going" and tracks[0]["state"] == "new"
    assert tracks[0]["missing"] == ["beats", "key", "energy", "sections", "vocals", "waveform", "stems"]
    assert lib.sets() == []  # nothing made
    assert client.post("/api/sets", json={"name": "S", "hashes": [a, b]}).status_code == 201


def test_adding_to_a_set(fx):
    client, lib, a, b, c, _ = fx
    set_id = client.post("/api/sets", json={"name": "S", "hashes": [a]}).json()["id"]
    refused(client.post(f"/api/sets/{set_id}/tracks", json={"hashes": [b, c]}), c)
    assert lib.set_hashes(set_id) == [a]
    assert client.post(f"/api/sets/{set_id}/tracks", json={"hashes": [b]}).json()["hashes"] == [a, b]


@pytest.mark.parametrize("call", [
    ("POST", "/api/sets/{id}/order", {}),
    ("PUT", "/api/sets/{id}/order", {"hashes": "set"}),
    ("POST", "/api/sets/{id}/timeline", {"hashes": "set"}),
    ("POST", "/api/sets/{id}/setplan", {}),
    ("GET", "/api/sets/{id}/session", None),
    ("POST", "/api/sets/{id}/export", {"format": "wav"}),
    ("GET", "/api/sets/{id}/bridge?position=0", None),
    ("GET", "/api/sets/{id}/bridge/online?position=0", None),
], ids=lambda c: f"{c[0]} {c[1].removeprefix('/api/sets/{id}/')}")  # fmt: skip
def test_a_set_with_a_track_that_isnt_ready(fx, call):
    client, lib, a, b, c, _ = fx
    method, path, body = call
    bad, good = lib.save_set("bad", [a, c, b]), lib.save_set("good", [a, b])

    def send(set_id: int, hashes: list[str]):
        json = {k: hashes if v == "set" else v for k, v in body.items()} if body is not None else None
        return client.request(method, path.replace("{id}", str(set_id)), json=json)

    refused(send(bad, [a, c, b]), c)
    if "online" in path:
        return  # the good set would ask tunebat
    r = send(good, [a, b])
    if path.endswith("/export"):  # as before: refused only for the transitions without a plan yet
        assert r.status_code == 409 and r.json()["detail"]["pairs"] == [{"from": a, "to": b}]
    else:
        assert r.status_code == 200, r.text


def test_reading_a_set_as_it_stands_still_works(fx):
    client, lib, a, b, c, _ = fx
    set_id = lib.save_set("S", [a, c, b])
    assert client.get(f"/api/sets/{set_id}").status_code == 200
    assert client.get(f"/api/sets/{set_id}/order").status_code == 200
    assert client.get(f"/api/sets/{set_id}/hashes").json()["hashes"] == [a, c, b]


def test_putting_a_bridge_in(fx):
    client, lib, a, b, c, d = fx
    set_id = lib.save_set("S", [a, b])
    refused(client.post(f"/api/sets/{set_id}/bridge", json={"position": 0, "hash": c, "from": a, "to": b}), c)
    assert lib.set_hashes(set_id) == [a, b]
    r = client.post(f"/api/sets/{set_id}/bridge", json={"position": 0, "hash": d, "from": a, "to": b})
    assert r.status_code == 200 and lib.set_hashes(set_id) == [a, d, b]


def test_a_bridge_search_only_offers_ready_tracks(fx):
    client, lib, a, b, c, d = fx
    set_id = lib.save_set("S", [a, b])
    found = client.get(f"/api/sets/{set_id}/bridge", params={"position": 0}).json()
    assert c not in {t["hash"] for t in found["results"]} and found["unanalysed"] == 1


def test_building_a_long_set(fx):
    client, lib, a, b, c, d = fx
    crate = lib.save_crate("Fresh", [a, c])
    refused(client.post("/api/sets/build", json={"sources": [{"kind": "crate", "id": crate}]}), c)
    refused(client.post("/api/sets/bridges", json={"sources": [{"kind": "crate", "id": crate}]}), c)
    refused(client.post("/api/sets/build/explain", json={"hashes": [a, c, b]}), c)
    refused(client.post("/api/sets/build/bridge", json={"hashes": [a, c], "position": 0}), c)
    built = client.post("/api/sets/build", json={"sources": [{"kind": "library"}]})  # the library: its ready tracks
    assert built.status_code == 200 and {t["hash"] for t in built.json()["order"]} == {a, b, d}
    ok = lib.save_crate("Done", [a, b])
    assert client.post("/api/sets/build", json={"sources": [{"kind": "crate", "id": ok}]}).status_code == 200
    assert client.post("/api/sets/bridges", json={"sources": [{"kind": "crate", "id": ok}]}).status_code == 200
    assert client.post("/api/sets/build/explain", json={"hashes": [a, b]}).status_code == 200
    assert client.post("/api/sets/build/bridge", json={"hashes": [a, b], "position": 0}).status_code == 200


def test_a_pair_plan_for_the_live_engine(fx):
    from .test_seedbox2_mix_plan import _plan

    client, lib, a, b, c, _ = fx
    lib.put_plan(a, c, {"from_id": a, "to_id": c}, user_edited=False)
    refused(client.get(f"/api/mixes/plans/{a}/{c}/live"), c)
    lib.put_plan(a, b, _plan(version=3, technique="bass swap", bars=16), user_edited=False)
    assert client.get(f"/api/mixes/plans/{a}/{b}/live").status_code == 200


def test_the_jobs_check_again(fx):
    from seedbox2.sets.export import set_export_job
    from seedbox2.sets.timeline import set_plan_job

    _, lib, a, _, c, _ = fx
    with pytest.raises(ready.NotReady):
        set_plan_job({"from": a, "to": c}, _Ctx(lib))
    assert lib.plan(a, c) is None
    ctx = _Ctx(lib)
    ctx.release_sessions = lambda: None
    ctx.settings = None
    with pytest.raises(ready.NotReady):
        set_export_job({"set_id": 1, "hashes": [a, c], "format": "wav", "name": "S"}, ctx)


def test_one_track_still_plays_on_its_own(fx):
    client, _, _, _, c, _ = fx
    r = client.get(f"/api/tracks/{c}/audio")
    assert r.status_code == 200 and r.json()["sources"] == ["mix"]


def test_a_failed_track_can_be_used_anyway(fx):
    client, lib, a, b, c, _ = fx
    fail(lib, c)
    row = client.post("/api/library/view", json={"hashes": [c]}).json()["items"][0]
    assert row["status"]["state"] == "failed" and row["status"]["usable"] is False and row["status"]["use_anyway"] is False
    refused(client.post("/api/sets", json={"name": "S", "hashes": [a, c]}), c)

    # for one request
    made = client.post("/api/sets", json={"name": "S", "hashes": [a, c], "use_anyway": [c]})
    assert made.status_code == 201
    set_id = made.json()["id"]
    refused(client.post(f"/api/sets/{set_id}/order", json={}), c)  # it isn't kept
    assert client.post(f"/api/sets/{set_id}/order", json={"use_anyway": [c]}).status_code == 200
    assert client.get(f"/api/sets/{set_id}/session", params={"use_anyway": c}).status_code == 200

    # kept with the track
    row = client.put(f"/api/library/tracks/{c}/use-anyway", json={"use_anyway": True}).json()
    assert row["hash"] == c and row["status"]["use_anyway"] is True and row["status"]["usable"] is True
    assert row["status"]["ready"] is False  # used anyway, still not ready
    assert client.post(f"/api/sets/{set_id}/order", json={}).status_code == 200
    assert client.post(f"/api/sets/{set_id}/timeline", json={"hashes": [a, c]}).status_code == 200
    assert client.put(f"/api/library/tracks/{c}/use-anyway", json={"use_anyway": False}).json()["status"]["usable"] is False
    refused(client.post(f"/api/sets/{set_id}/order", json={}), c)
    assert client.put(f"/api/library/tracks/{'e' * 40}/use-anyway", json={"use_anyway": True}).status_code == 404


def test_use_anyway_doesnt_skip_an_analysis_still_to_come(fx):
    client, lib, a, _, c, _ = fx
    fail(lib, c)
    lib.add_job("key", {"hash": c})  # retried, or still going: wait for it
    client.put(f"/api/library/tracks/{c}/use-anyway", json={"use_anyway": True})
    tracks = refused(client.post("/api/sets", json={"name": "S", "hashes": [a, c], "use_anyway": [c]}), c)
    assert tracks[0]["state"] == "queued"


def test_stems_off_means_a_track_is_ready_without_them(fx):
    client, lib, a, _, _, _ = fx
    e = "e" * 40
    lib.upsert_track(e, 300.0, title="e")
    make_ready(lib, e)
    lib.delete_artifacts(e, "stems")
    refused(client.post("/api/sets", json={"name": "S", "hashes": [a, e]}), e)
    assert client.post("/api/analysis/stems/settings", json={"enabled": False}).status_code == 200
    assert client.post("/api/sets", json={"name": "S", "hashes": [a, e]}).status_code == 201
