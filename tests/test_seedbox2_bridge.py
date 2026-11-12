"""Gaps in a set and what could go between the neighbours (#64): synthetic tracks whose best bridge is known."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from musicreco.models import Track
from musicreco.tunebat import TunebatError
from seedbox2.analysis import vibe
from seedbox2.library import Library
from seedbox2.library.tunebat_try import TunebatTry, same_track
from seedbox2.server.app import create_app
from seedbox2.sets import OrderRequest, TrackFeatures, evaluate_tracks
from seedbox2.sets.bridge import bridge_keys, find_gap, rank_online, wanted
from seedbox2.sets.cost import key_index, key_step
from seedbox2.sets.explain import GAP_SCORE, score_of

from .s2_ready import make_ready


def unit(*values: float) -> np.ndarray:
    v = np.zeros(vibe.clap.DIM, np.float32)
    v[: len(values)] = values
    return v / np.linalg.norm(v)


def features(name: str, **kw) -> TrackFeatures:
    return TrackFeatures(hash=name, title=name, **kw)


def pair_score(a: TrackFeatures, b: TrackFeatures) -> dict:
    return evaluate_tracks([a, b], OrderRequest(arc="flat"))["transitions"][0]


def online(id_: str, bpm: float = 124.0, camelot: str = "9A", name: str | None = None) -> Track:
    return Track.from_api({"id": id_, "n": name or f"Song {id_}", "as": [f"Artist {id_}"], "b": bpm, "c": camelot})


# --- gaps ----------------------------------------------------------------------------------------------------------------


def test_a_clash_left_in_the_order_is_a_gap_and_a_good_move_isnt():
    keys = ["8A", "8A", "2A", "2A"]  # a semitone here only moves the clash
    result = evaluate_tracks([features(str(i), bpm=124.0, camelot=k) for i, k in enumerate(keys)], OrderRequest(arc="flat"))
    assert [t["gap"] for t in result["transitions"]] == [False, True, False]
    assert result["transitions"][1]["flags"] == ["key_clash"]
    good = pair_score(features("a", bpm=124.0, camelot="8A"), features("b", bpm=124.0, camelot="9A"))
    assert not good["gap"] and good["score"] > 90


def test_a_clash_a_semitone_fixes_isnt_a_gap_but_the_shift_counts():
    fixed = pair_score(features("a", bpm=124.0, camelot="8A"), features("b", bpm=124.0, camelot="2A"))
    good = pair_score(features("a", bpm=124.0, camelot="8A"), features("b", bpm=124.0, camelot="9A"))
    assert not fixed["gap"] and fixed["score"] < good["score"]


def test_a_jump_in_tempo_is_a_gap_even_in_the_same_key():
    t = pair_score(features("a", bpm=120.0, camelot="8A"), features("b", bpm=132.0, camelot="8A"))
    assert t["gap"] and t["score"] == 0


def test_what_isnt_known_isnt_a_gap():
    t = pair_score(features("a"), features("b"))
    assert not t["gap"] and t["score"] > GAP_SCORE


def test_a_plus_two_with_a_genre_change_isnt_a_gap():
    t = pair_score(features("a", bpm=124.0, camelot="8A", genre="house"), features("b", bpm=124.0, camelot="10A", genre="techno"))
    assert not t["gap"]


def test_the_score_falls_with_the_cost():
    assert score_of(0.0) == 100
    assert score_of(3.0) == 50
    assert score_of(50.0) == 0


# --- what to look for ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("a", "b", "keys"), [
    ("8A", "9A", ["8A", "9A"]),  # either is one small move at worst
    ("8A", "10A", ["9A"]),
    ("8A", "8B", ["8A", "8B"]),
    ("8A", "2A", []),  # six steps apart: every key leaves a clash
])  # fmt: skip
def test_bridge_keys(a, b, keys):
    assert bridge_keys(key_index(a), key_index(b)) == keys


def test_a_shifted_neighbour_counts_at_the_key_it_is_played_at():
    gap = find_gap([features("a", camelot="8A"), features("b", camelot="2A")], OrderRequest(arc="flat"), 0)
    assert key_step(gap.key_a, gap.key_b) < 3
    assert (gap.key_a, gap.key_b) != (key_index("8A"), key_index("2A"))


def test_wanted_reads_like_the_ticket():
    v = np.eye(4)[0]
    tracks = [features("a", bpm=122.0, camelot="8A", energy=0.4, vibes={"main": v}),
              features("b", bpm=126.0, camelot="10A", energy=0.6, vibes={"main": v})]  # fmt: skip
    want = wanted(find_gap(tracks, OrderRequest(arc="flat"), 0))
    assert want["text"] == "a 124 BPM 9A track with a long intro and a similar vibe"
    assert want["energy"] == [0.4, 0.6]


def test_wanted_follows_half_time():
    tracks = [features("a", bpm=120.0), features("b", bpm=62.0)]
    assert wanted(find_gap(tracks, OrderRequest(arc="flat"), 0))["bpm"] == pytest.approx(121.98, abs=0.1)


def test_no_such_transition():
    from seedbox2.sets.bridge import BridgeError

    with pytest.raises(BridgeError):
        find_gap([features("a"), features("b")], OrderRequest(arc="flat"), 1)


# --- the library ---------------------------------------------------------------------------------------------------------


class Fixture:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
        self.lib = Library.open(tmp_path / "lib.sqlite3")
        self.root = tmp_path / "emb"
        self.app = create_app(library_path=tmp_path / "lib.sqlite3", start_jobs=False)
        self.client = TestClient(self.app)

    def close(self) -> None:
        self.client.close()
        self.lib.close()

    def add(self, h: str, *, bpm: float | None = None, camelot: str | None = None, energy: float | None = None,
            vec: np.ndarray | None = None, intro_bars: int = 0, title: str | None = None, artist: str = "",
            ready: bool | None = None) -> str:  # fmt: skip
        """`ready` (#99), by default once it has a tempo and a key."""
        h = h.ljust(40, "0")
        self.lib.upsert_track(h, 400.0, title=title or h[:4], artist=artist, tags={})
        if bpm is not None:
            self.lib.put_analysis(h, "beats", "t", {"bpm": bpm})
        if camelot is not None:
            self.lib.put_analysis(h, "key", "t", {"camelot": camelot, "key": camelot})
        if energy is not None:
            bars = np.linspace(0, 200, 101)
            curve = np.ones(100)
            curve[:intro_bars] = 0.0
            self.lib.put_analysis(h, "energy", vibe.energy.VERSION, {"energy": energy, "rating": round(energy * 9 + 1),
                                                                     "bar_s": bars.tolist(), "curve": curve.tolist()})  # fmt: skip
        if vec is not None:
            path = vibe.artifact_path(h, self.root)
            windows = [{"name": n, "start_s": 0.0} for n in ("intro", "main", "outro")]
            vibe.write_embeddings(path, windows, np.stack([vec, vec, vec]))
            self.lib.put_artifact(h, vibe.ARTIFACT_KIND, vibe.model_version(), path)
        if ready if ready is not None else bpm is not None and camelot is not None:
            make_ready(self.lib, h)
        return h

    def gap_set(self, a_key: str = "8A", b_key: str = "10A", a_bpm: float = 120.0, b_bpm: float = 130.0) -> tuple[int, str, str]:
        a = self.add("aa", bpm=a_bpm, camelot=a_key, energy=0.4, vec=unit(1, 0))
        b = self.add("bb", bpm=b_bpm, camelot=b_key, energy=0.6, vec=unit(1, 0))
        return self.lib.save_set("Friday", [a, b]), a, b

    def bridges(self, set_id: int, position: int = 0) -> dict:
        r = self.client.get(f"/api/sets/{set_id}/bridge", params={"position": position})
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def fx(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    yield fixture
    fixture.close()


def titles(found: dict) -> list[str]:
    return [t["title"] for t in found["results"]]


def test_the_track_between_both_neighbours_wins(fx):
    set_id, _, _ = fx.gap_set()
    fx.add("c1", title="fits", bpm=125.0, camelot="9A", energy=0.5, vec=unit(1, 0))
    fx.add("c2", title="too slow", bpm=121.0, camelot="9A", energy=0.5, vec=unit(1, 0))
    fx.add("c3", title="clashes", bpm=125.0, camelot="3B", energy=0.5, vec=unit(1, 0))
    fx.add("c4", title="too loud", bpm=125.0, camelot="9A", energy=1.0, vec=unit(1, 0))
    fx.add("c5", title="other vibe", bpm=125.0, camelot="9A", energy=0.5, vec=unit(0, 1))
    found = fx.bridges(set_id)
    names = titles(found)
    assert names[0] == "fits"
    assert names.index("fits") < names.index("too loud")
    assert names.index("fits") < names.index("other vibe")
    assert names[-1] == "clashes"
    assert found["results"][0]["score_in"] > found["gap"]["score"] and found["results"][0]["score_out"] > found["gap"]["score"]
    assert "8A → 9A → 10A" in found["results"][0]["text"] and "same vibe" in found["results"][0]["text"]
    assert found["want"]["text"].startswith("a 125 BPM 9A track")


def test_a_long_intro_breaks_a_tie(fx):
    set_id, _, _ = fx.gap_set()
    fx.add("c1", title="abrupt", bpm=125.0, camelot="9A", energy=0.5)
    fx.add("c2", title="long intro", bpm=125.0, camelot="9A", energy=0.5, intro_bars=20)
    found = fx.bridges(set_id)
    assert titles(found) == ["long intro", "abrupt"]
    assert found["results"][0]["intro_s"] == pytest.approx(40.0)
    assert "long intro" in found["results"][0]["text"]


def test_only_our_own_analysis_counts_and_the_answer_says_how_many_wait(fx):
    set_id, _, _ = fx.gap_set()
    h = fx.add("c1", title="tags only")
    fx.lib.upsert_track(h, 400.0, tags={"file": {"bpm": 125, "key": "9A"}})
    fx.add("c2", title="no key", bpm=125.0)
    found = fx.bridges(set_id)
    assert found["results"] == [] and found["candidates"] == 0 and found["unanalysed"] == 2


def test_the_sets_own_tracks_arent_suggested(fx):
    set_id, a, b = fx.gap_set()
    c = fx.add("c1", bpm=125.0, camelot="9A", energy=0.5)
    d = fx.add("d1", title="in the set", bpm=125.0, camelot="9A", energy=0.5)
    fx.lib.add_to_set(set_id, [d])
    found = fx.bridges(set_id)
    assert [t["hash"] for t in found["results"]] == [c]
    assert not {a, b, d} & {t["hash"] for t in found["results"]}


def test_a_candidate_that_needs_a_shift_says_so(fx):
    set_id, _, _ = fx.gap_set(a_key="8A", b_key="8A", a_bpm=120.0, b_bpm=130.0)
    fx.add("c1", bpm=125.0, camelot="1A", energy=0.5)  # 1A + 1 semitone = 8A
    found = fx.bridges(set_id)
    assert found["results"][0]["shift"] == 1
    assert "8A (1A +1 st)" in found["results"][0]["text"]


def test_bad_requests(fx):
    set_id, _, _ = fx.gap_set()
    assert fx.client.get("/api/sets/999/bridge", params={"position": 0}).status_code == 404
    assert fx.client.get(f"/api/sets/{set_id}/bridge", params={"position": 1}).status_code == 422


def test_the_order_endpoint_flags_gaps(fx):
    set_id, _, _ = fx.gap_set()
    t = fx.client.get(f"/api/sets/{set_id}/order").json()["transitions"][0]
    assert t["gap"] and t["score"] < GAP_SCORE


# --- putting one in ------------------------------------------------------------------------------------------------------


def test_inserting_a_bridge_moves_later_pins_with_their_tracks(fx):
    set_id, a, b = fx.gap_set()
    c = fx.add("cc", bpm=125.0, camelot="9A", energy=0.5)
    d = fx.add("dd", bpm=130.0, camelot="10A", energy=0.6)
    fx.lib.add_to_set(set_id, [d])
    fx.lib.put_set_order(set_id, [a, b, d], {"version": 1, "arc": "flat", "tempo_start": None, "tempo_end": None,
                                             "pins": [{"position": 0, "hash": a}, {"position": 2, "hash": d}]})  # fmt: skip
    r = fx.client.post(f"/api/sets/{set_id}/bridge", json={"position": 0, "hash": c, "from": a, "to": b})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [t["hash"] for t in out["order"]] == [a, c, b, d]
    assert out["settings"]["pins"] == [{"position": 0, "hash": a}, {"position": 3, "hash": d}]
    assert out["settings"]["arc"] == "flat"
    assert not out["transitions"][0]["gap"] and not out["transitions"][1]["gap"]
    assert fx.lib.set_hashes(set_id) == [a, c, b, d]


def test_inserting_into_a_set_that_changed_is_refused(fx):
    set_id, a, b = fx.gap_set()
    c = fx.add("cc", bpm=125.0, camelot="9A")
    assert fx.client.post(f"/api/sets/{set_id}/bridge", json={"position": 0, "hash": c, "from": b, "to": a}).status_code == 409
    assert fx.client.post(f"/api/sets/{set_id}/bridge", json={"position": 0, "hash": "nope", "from": a, "to": b}).status_code == 409
    assert fx.client.post("/api/sets/999/bridge", json={"position": 0, "hash": c, "from": a, "to": b}).status_code == 404
    assert fx.lib.set_hashes(set_id) == [a, b]


# --- tunebat -------------------------------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self, recommended: dict[str, list[Track]], found: list[Track] | None = None, error: Exception | None = None):
        self.recommended = recommended
        self.found = found or []
        self.error = error
        self.calls: list[tuple] = []

    def search(self, term):
        self.calls.append(("search", term))
        if self.error:
            raise self.error
        return type("Page", (), {"items": self.found, "total_count": len(self.found)})()

    def get_track(self, id_):
        self.calls.append(("get_track", id_))
        if self.error:
            raise self.error
        return type("Detail", (), {"recommended": self.recommended.get(id_, [])})()


@pytest.mark.parametrize(("a", "b", "same"), [
    (("Song", "X"), ("song", "x"), True),
    (("Song (Extended Mix)", "X, Y"), ("Song", "Y"), True),
    (("Song", ""), ("Song", "Anyone"), True),
    (("Song", "X"), ("Song", "Z"), False),
    (("Song", "X"), ("Other", "X"), False),
    (("", "X"), ("", "X"), False),
])  # fmt: skip
def test_same_track(a, b, same):
    assert same_track(*a, *b) is same


def gap_for_online(a_bpm: float = 120.0, b_bpm: float = 130.0, b_key: str = "10A"):
    tracks = [features("a", bpm=a_bpm, camelot="8A"), features("b", bpm=b_bpm, camelot=b_key)]
    return find_gap(tracks, OrderRequest(arc="flat"), 0)


def test_online_tracks_rank_by_the_same_cost_and_skip_what_the_gap_beats():
    gap = gap_for_online(122.0, 130.0, "9A")
    results = rank_online(gap, [online("close", 124, "8A"), online("good", 126, "8A"), online("same", 122, "8A")],
                          [online("noBpm", 0, "9A"), online("noKey", 125, "")], [])  # fmt: skip
    assert [r["id"] for r in results] == ["good", "close"], "a track at one neighbour's tempo leaves the gap as it was"
    assert results[0]["from"] == ["from"]


def test_recommended_from_both_comes_first_all_else_equal():
    gap = gap_for_online()
    results = rank_online(gap, [online("one", 125, "9A"), online("both", 125, "9A")], [online("both", 125, "9A")], [])
    assert [r["id"] for r in results] == ["both", "one"] and results[0]["from"] == ["from", "to"]


def test_online_leaves_out_what_the_library_has():
    gap = gap_for_online()
    t = online("mine", 125, "9A", name="Song (Radio Edit)")
    assert rank_online(gap, [t], [], [{"title": "Song", "artist": "Artist mine"}]) == []


def api_with_tunebat(fx, client) -> None:
    fx.app.state.tunebat = TunebatTry(None, lambda *_: None, client=client)


def test_the_endpoint_uses_a_kept_id_or_searches_and_download_knows_the_results(fx, monkeypatch):
    monkeypatch.setattr("seedbox2.library.tunebat_try.MIN_INTERVAL_S", 0.0)
    set_id, a, b = fx.gap_set()
    fx.lib.upsert_track(a, 400.0, tags={"tunebat": {"id": "seedA"}})
    fx.lib.upsert_track(b, 400.0, title="B Song", artist="B")
    client = FakeClient({"seedA": [online("r1", 125, "9A")], "seedB": [online("r2", 125, "9A")]},
                        found=[online("zzz", name="Not it"), Track.from_api({"id": "seedB", "n": "B Song", "as": ["B"]})])  # fmt: skip
    api_with_tunebat(fx, client)
    r = fx.client.get(f"/api/sets/{set_id}/bridge/online", params={"position": 0})
    assert r.status_code == 200, r.text
    out = r.json()
    assert {x["id"] for x in out["results"]} == {"r1", "r2"}
    assert out["seeds"] == [{"hash": a, "id": "seedA"}, {"hash": b, "id": "seedB"}]
    assert client.calls == [("get_track", "seedA"), ("search", "B B Song"), ("get_track", "seedB")]

    client.calls.clear()
    assert fx.client.get(f"/api/sets/{set_id}/bridge/online", params={"position": 0}).status_code == 200
    assert client.calls == [("search", "B B Song")], "a seed's recommendations are only asked for once"

    monkeypatch.setattr(TunebatTry, "_run", lambda self, track: None)
    d = fx.client.post("/api/tunebat/download", json={"id": "r2"})
    assert d.status_code == 202 and d.json()["state"] == "queued"


def test_a_neighbour_tunebat_doesnt_know_is_said(fx, monkeypatch):
    monkeypatch.setattr("seedbox2.library.tunebat_try.MIN_INTERVAL_S", 0.0)
    set_id, a, b = fx.gap_set()
    api_with_tunebat(fx, FakeClient({}))
    out = fx.client.get(f"/api/sets/{set_id}/bridge/online", params={"position": 0}).json()
    assert out["results"] == [] and out["seeds"] == [{"hash": a, "id": None}, {"hash": b, "id": None}]


def test_tunebat_down_is_a_502(fx, monkeypatch):
    monkeypatch.setattr("seedbox2.library.tunebat_try.MIN_INTERVAL_S", 0.0)
    set_id, _, _ = fx.gap_set()
    api_with_tunebat(fx, FakeClient({}, error=TunebatError("HTTP 403")))
    r = fx.client.get(f"/api/sets/{set_id}/bridge/online", params={"position": 0})
    assert r.status_code == 502 and "Tunebat" in r.json()["detail"]


