"""Long sets across crates (#62): synthetic crates whose right answer is known by construction."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from seedbox2.analysis import vibe
from seedbox2.library import Library
from seedbox2.server.app import create_app
from seedbox2.sets import TEMPLATES, OrderRequest, Problem, TrackFeatures, order_tracks
from seedbox2.sets.cost import Weights, group_bands
from seedbox2.sets.longset import (
    LIBRARY_TRACKS,
    SourceError,
    bridges,
    build_long_set,
    links_between,
    quotas,
    walk,
)

FLAT = TEMPLATES["flat"]


def track(name: str, bpm: float | None = 124.0, key: str | None = "8A", energy: float | None = 0.5, **kw) -> TrackFeatures:
    return TrackFeatures(hash=name, title=name, bpm=bpm, camelot=key, energy=energy, **kw)


def names(result: dict) -> list[str]:
    return [t["hash"] for t in result["order"]]


# --- the crate order as a soft constraint ----------------------------------------------------------------------------


def test_group_bands_are_each_groups_share_in_order():
    first, last = group_bands([0, 0, 0, 1, 1, 2])
    assert first.tolist() == [0, 0, 0, 3, 3, 5]
    assert last.tolist() == [2, 2, 2, 4, 4, 5]


def test_without_groups_nothing_changes():
    tracks = [track(f"t{i}", key=f"{i % 12 + 1}A") for i in range(6)]
    assert Problem(tracks, FLAT).crate_cost.sum() == 0
    assert "crate" not in order_tracks(tracks)["transitions"][0]["terms"]


def test_the_cost_is_the_positions_outside_the_share():
    p = Problem([track(n) for n in "abcd"], FLAT, groups=[0, 0, 1, 1])
    w = p.w.crate_order
    assert p.crate_cost[0].tolist() == [0, 0, 2 * w, 3 * w]  # a track of the first crate, by position: dearer the further out
    assert p.crate_cost[3].tolist() == [3 * w, 2 * w, 0, 0]
    assert p.cost([0, 1, 2, 3]) < p.cost([2, 3, 0, 1])


def test_crates_hold_their_order_when_nothing_much_is_at_stake():
    tracks = [track(f"a{i}", key="8A", energy=0.2 + i / 20) for i in range(4)] + [track(f"b{i}", key="8A", energy=0.5 + i / 20) for i in range(4)]
    tracks = tracks[4:] + tracks[:4]  # the b crate is given first, against its energy
    groups = [0] * 4 + [1] * 4
    together = order_tracks(tracks, OrderRequest(arc="warm_up"))
    held = order_tracks(tracks, OrderRequest(arc="warm_up"), groups)
    assert names(together)[0].startswith("a")  # free to, it goes with the arc
    assert all(n.startswith("b") for n in names(held)[:4])  # asked to, the crates stay as given
    assert held["transitions"][0]["terms"]["crate"] == 0


NO_FIXES = Weights(key_fix=50.0)  # so a clash can't be cured with a semitone instead


def stray_crates():
    """The stray belongs to the first crate, but its key goes in the middle of the second: staying home is two clashes."""
    return [track(f"a{i}", key="1A") for i in range(3)] + [track("stray", key="4A")] + [track(f"b{i}", key=k) for i, k in enumerate(["2A", "3A", "5A", "6A"])]


def test_a_track_at_home_at_the_border_needs_no_move():
    tracks = [track("stray", key="3A"), track("a0", key="8A"), track("a1", key="8A"), track("b0", key="3A"), track("b1", key="3A")]
    assert names(order_tracks(tracks, OrderRequest(arc="flat", weights=NO_FIXES), [0, 0, 0, 1, 1])).index("stray") == 2


def test_a_track_leaves_its_crate_when_that_saves_more_than_it_costs():
    opener = [{"position": 0, "hash": "a0"}]  # or it would open the set, where it costs one clash and not two
    result = order_tracks(stray_crates(), OrderRequest(arc="flat", weights=NO_FIXES, pins=opener), [0, 0, 0, 0, 1, 1, 1, 1])
    assert names(result).index("stray") >= 4


def test_a_strong_crate_order_keeps_it_home():
    opener = [{"position": 0, "hash": "a0"}]
    strong = order_tracks(stray_crates(), OrderRequest(arc="flat", pins=opener, weights=Weights(key_fix=50.0, crate_order=50.0)), [0, 0, 0, 0, 1, 1, 1, 1])
    assert names(strong).index("stray") <= 3


# --- how many from each ----------------------------------------------------------------------------------------------


def test_quotas_keep_everything_that_fits():
    assert quotas([3, 4], [False, False], 10) == [3, 4]


def test_quotas_let_the_library_fill_what_the_crates_leave():
    assert quotas([10, 1000, 5], [False, True, False], 40) == [10, 25, 5]


def test_quotas_share_out_crates_that_dont_fit():
    out = quotas([100, 50, 50], [False, False, False], 20)
    assert out == [10, 5, 5]
    assert sum(quotas([7, 7, 7], [False, False, False], 10)) == 10
    assert quotas([100, 5000], [False, True], 20) == [20, 0]


def test_a_walk_follows_the_arc_and_stays_in_key():
    rng = np.random.default_rng(1)
    pool = [track(f"t{i:03}", bpm=float(rng.uniform(118, 130)), key=f"{rng.integers(1, 13)}A", energy=float(rng.uniform(0, 1))) for i in range(400)]
    levels = np.linspace(0, 1, 12)
    picked = walk(pool, 12, levels, None, before=None, weights=Weights(), detail=lambda _t: None, energy_range=(0.0, 1.0))
    assert len({t.hash for t in picked}) == 12
    energy = [t.energy for t in picked]
    assert np.corrcoef(energy, levels)[0, 1] > 0.9
    p = Problem(picked, FLAT)
    steps = [p.key_matrix()[i, i + 1] for i in range(11)]
    assert max(steps) < 3  # no clash


def test_a_walk_prefers_analysed_tracks():
    pool = [track(f"bare{i}", bpm=None, key=None, energy=None) for i in range(10)] + [track(f"known{i}") for i in range(5)]
    picked = walk(pool, 3, [0.5] * 3, None, before=None, weights=Weights(), detail=lambda _t: None, energy_range=(0.0, 1.0))
    assert all(t.hash.startswith("known") for t in picked)
    all_of_them = walk(pool, 12, [0.5] * 12, None, before=None, weights=Weights(), detail=lambda _t: None, energy_range=(0.0, 1.0))
    assert len(all_of_them) == 12  # not enough analysed: it takes what there is


# --- links -----------------------------------------------------------------------------------------------------------


def test_the_best_link_is_the_pair_that_mixes_best():
    tracks = [track("a_end_clash", key="1A", bpm=124), track("a_end_good", key="8A", bpm=124), track("b_start_good", key="9A", bpm=125),
              track("b_start_far", key="3B", bpm=140)]  # fmt: skip
    links = links_between(tracks, [(["a_end_clash"], ["a_end_clash", "a_end_good"]), (["b_start_good", "b_start_far"], ["b_start_far"])])
    forward = next(x for x in links if x["from_source"] == 0)
    assert (forward["from"]["hash"], forward["to"]["hash"]) == ("a_end_good", "b_start_good")
    assert forward["fit"] == "good" and "8A → 9A" in forward["text"]
    backward = next(x for x in links if x["from_source"] == 1)
    assert backward["fit"] == "poor" and backward["cost"] > forward["cost"]
    assert links[0] is forward  # cheapest first


def test_a_link_with_nothing_known_doesnt_claim_to_be_good():
    tracks = [track("a", bpm=None, key=None, energy=None), track("b", bpm=None, key=None, energy=None)]
    [one, _] = links_between(tracks, [(["a"], ["a"]), (["b"], ["b"])])
    assert one["fit"] == "unknown"


def test_a_track_isnt_linked_to_itself():
    tracks = [track("both"), track("other", key="9A")]
    links = links_between(tracks, [(["both"], ["both"]), (["both", "other"], ["other"])])
    forward = next(x for x in links if x["from_source"] == 0)
    assert forward["to"]["hash"] == "other"


# --- from the library ------------------------------------------------------------------------------------------------


def add(lib: Library, name: str, bpm: float, key: str, energy: float) -> str:
    h = name.ljust(40, "0")
    lib.upsert_track(h, 240.0, title=name, artist="Someone")
    lib.put_analysis(h, "beats", "1", {"bpm": bpm})
    lib.put_analysis(h, "key", "1", {"camelot": key, "key": "x", "open_key": "y"})
    lib.put_analysis(h, "energy", vibe.energy.VERSION, {"energy": energy, "rating": round(energy * 9) + 1, "bar_s": [0.0, 120.0, 240.0],
                                                        "curve": [energy, energy]})  # fmt: skip
    return h


@pytest.fixture
def lib(tmp_path):
    library = Library.open(tmp_path / "lib.sqlite3")
    yield library
    library.close()


def crate(lib: Library, prefix: str, keys: list[str], start: float = 124.0) -> tuple[int, list[str]]:
    hashes = [add(lib, f"{prefix}{i}", start + i, k, 0.2 + i / 12) for i, k in enumerate(keys)]
    return lib.save_crate(prefix, hashes), hashes


def test_build_across_two_crates_in_the_order_given(lib):
    a, ah = crate(lib, "a", ["8A", "9A", "10A"])
    b, bh = crate(lib, "b", ["10A", "11A", "12A"], 127.0)
    result = build_long_set(lib, [{"kind": "crate", "id": a}, {"kind": "crate", "id": b}], OrderRequest(arc="warm_up"))
    assert set(names(result)) == set(ah + bh)
    assert {t["source"] for t in result["order"][:3]} == {0}  # the first crate first
    assert result["moved"] == 0 and result["left_out"] == 0
    assert [s["name"] for s in result["sources"]] == ["a", "b"]
    assert all("crate" in t["terms"] for t in result["transitions"])


def test_the_order_given_is_the_order_kept(lib):
    a, _ = crate(lib, "a", ["8A", "8A", "8A"])
    b, _ = crate(lib, "b", ["8A", "8A", "8A"])
    forward = build_long_set(lib, [{"kind": "crate", "id": a}, {"kind": "crate", "id": b}])
    back = build_long_set(lib, [{"kind": "crate", "id": b}, {"kind": "crate", "id": a}])
    assert forward["order"][0]["title"].startswith("a") and back["order"][0]["title"].startswith("b")


def test_keep_order_off_lets_them_mix(lib):
    a, _ = crate(lib, "a", ["8A", "8A", "8A"])
    result = build_long_set(lib, [{"kind": "crate", "id": a}], keep_order=False)
    assert "crate" not in result["transitions"][0]["terms"]


def test_a_track_in_two_crates_plays_once_for_the_first(lib):
    a, ah = crate(lib, "a", ["8A", "9A"])
    b = lib.save_crate("b", [ah[0], add(lib, "own", 125, "9A", 0.5)])
    result = build_long_set(lib, [{"kind": "crate", "id": a}, {"kind": "crate", "id": b}])
    assert sorted(names(result)) == sorted({*ah, "own".ljust(40, "0")})
    assert [s["tracks"] for s in result["sources"]] == [2, 1]


def test_a_set_comes_in_as_a_source_too(lib):
    a, _ = crate(lib, "a", ["8A", "9A"])
    s = lib.save_set("friday", [add(lib, "s0", 125, "9A", 0.5), add(lib, "s1", 125, "10A", 0.6)])
    result = build_long_set(lib, [{"kind": "set", "id": s}, {"kind": "crate", "id": a}])
    assert {t["source"] for t in result["order"][:2]} == {0}
    assert result["sources"][0] == {"kind": "set", "id": s, "name": "friday", "tracks": 2, "picked": 2}


def test_the_whole_library_gets_a_track_limit(lib):
    rng = np.random.default_rng(3)
    for i in range(LIBRARY_TRACKS + 40):
        add(lib, f"t{i:03}", float(rng.uniform(120, 128)), f"{rng.integers(1, 13)}A", float(rng.uniform(0, 1)))
    result = build_long_set(lib, [{"kind": "library"}], OrderRequest(arc="warm_up"))
    assert len(result["order"]) == LIBRARY_TRACKS and result["left_out"] == 40
    shorter = build_long_set(lib, [{"kind": "library"}], OrderRequest(arc="warm_up"), max_tracks=10)
    assert len(shorter["order"]) == 10
    assert result["sources"][0]["picked"] == LIBRARY_TRACKS
    assert names(build_long_set(lib, [{"kind": "library"}], OrderRequest(arc="warm_up"), max_tracks=10)) == names(shorter)  # the same every time


def test_the_library_adds_what_a_crate_doesnt_have(lib):
    a, ah = crate(lib, "a", ["8A", "9A"])
    for i in range(5):
        add(lib, f"more{i}", 125, "9A", 0.5)
    result = build_long_set(lib, [{"kind": "crate", "id": a}, {"kind": "library"}], max_tracks=4)
    assert len(result["order"]) == 4 and set(ah) <= set(names(result))
    assert [s["picked"] for s in result["sources"]] == [2, 2]


def test_crates_too_large_for_the_limit_are_shortened_not_refused(lib):
    a, _ = crate(lib, "a", ["8A"] * 8)
    b, _ = crate(lib, "b", ["9A"] * 8, 126.0)
    result = build_long_set(lib, [{"kind": "crate", "id": a}, {"kind": "crate", "id": b}], max_tracks=6)
    assert [s["picked"] for s in result["sources"]] == [3, 3] and result["left_out"] == 10


def test_a_missing_source_says_so(lib):
    with pytest.raises(SourceError):
        build_long_set(lib, [{"kind": "crate", "id": 99}])


def test_bridges_use_each_crates_end_and_start(lib):
    a, ah = crate(lib, "a", ["1A", "2A", "3A", "4A", "5A", "6A", "7A", "8A"])
    b, bh = crate(lib, "b", ["9A", "10A", "11A", "12A", "12B", "11B", "10B", "9B"], 130.0)
    out = bridges(lib, [{"kind": "crate", "id": a}, {"kind": "library"}, {"kind": "crate", "id": b}], edge=3)
    forward = next(x for x in out["links"] if x["from_source"] == 0)
    assert forward["to_source"] == 2  # counted among all the sources, the library included
    assert forward["from"]["hash"] in ah[-3:] and forward["to"]["hash"] in bh[:3]
    assert forward["from"]["hash"] == ah[-1]  # 8A → 9A is the one that works
    assert forward["to"]["hash"] == bh[0]


def test_bridges_with_empty_crates_dont_fall_over(lib):
    a = lib.save_crate("a", [])
    b = lib.save_crate("b", [])
    assert bridges(lib, [{"kind": "crate", "id": a}, {"kind": "crate", "id": b}])["links"] == []


def test_a_clash_between_crates_is_a_poor_link(lib):
    a, _ = crate(lib, "a", ["8A"])
    b, _ = crate(lib, "b", ["11B"])
    link = bridges(lib, [{"kind": "crate", "id": a}, {"kind": "crate", "id": b}])["links"][0]
    assert link["fit"] == "poor"


# --- the API ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    app = create_app(library_path=tmp_path / "lib.sqlite3", start_jobs=False)
    with TestClient(app) as c:
        c.get("/api/sets")
        yield c


def test_the_api_from_crates_to_a_saved_set(client):
    lib = client.app.state.library
    a, _ = crate(lib, "a", ["8A", "9A", "10A"])
    b, _ = crate(lib, "b", ["3B", "4B", "5B"], 126.0)
    sources = [{"kind": "crate", "id": a}, {"kind": "crate", "id": b}]

    r = client.post("/api/sets/build", json={"sources": sources, "arc": "warm_up", "max_tracks": 5})
    assert r.status_code == 200, r.text
    built = r.json()
    assert len(built["order"]) == 5 and built["settings"]["arc"] == "warm_up"
    assert built["left_out"] == 1

    r = client.post("/api/sets/bridges", json={"sources": sources})
    assert r.status_code == 200 and len(r.json()["links"]) == 2

    hashes = [t["hash"] for t in built["order"]]
    extra = add(lib, "extra", 125, "9A", 0.5)
    r = client.post("/api/sets/build/bridge", json={"hashes": hashes, "position": 0, "arc": "warm_up"})
    assert r.status_code == 200 and {x["hash"] for x in r.json()["results"]} <= {extra}  # the set's own never come back
    assert r.json()["candidates"] == 2  # what the crates left out, and the new one
    r = client.post("/api/sets/build/explain", json={"hashes": [hashes[0], extra, *hashes[1:]], "arc": "warm_up"})
    assert r.status_code == 200 and len(r.json()["order"]) == 6

    r = client.post("/api/sets", json={"name": "Long", "hashes": hashes})
    assert r.status_code == 201 and client.get(f"/api/sets/{r.json()['id']}/hashes").json()["hashes"] == hashes
    # the page then keeps the arc with it, so the set opens as it was built
    r = client.put(f"/api/sets/{r.json()['id']}/order", json={"hashes": hashes, "arc": "warm_up", "tempo_start": None, "tempo_end": None, "pins": []})
    assert r.status_code == 200 and r.json()["settings"]["arc"] == "warm_up"


def test_the_api_takes_no_length_as_all_of_them(client):
    lib = client.app.state.library
    a, ah = crate(lib, "a", ["8A", "9A", "10A"])
    r = client.post("/api/sets/build", json={"sources": [{"kind": "crate", "id": a}], "max_tracks": None, "keep_order": False, "arc": "peak_time"})
    assert r.status_code == 200 and sorted(names(r.json())) == sorted(ah)


def test_the_api_refuses_what_it_cant_do(client):
    assert client.post("/api/sets/build", json={"sources": []}).status_code == 422
    assert client.post("/api/sets/build", json={"sources": [{"kind": "tape"}]}).status_code == 422
    assert client.post("/api/sets/build", json={"sources": [{"kind": "crate", "id": 9}]}).status_code == 404
    assert client.post("/api/sets/bridges", json={"sources": [{"kind": "set", "id": 9}]}).status_code == 404
    assert client.post("/api/sets/build/bridge", json={"hashes": ["x", "y"], "position": 0}).status_code == 404
    assert client.post("/api/sets/build/explain", json={"hashes": ["nope"]}).status_code == 404
    assert client.post("/api/sets/build", json={"sources": [{"kind": "library"}], "max_tracks": 100000}).status_code == 422
    assert client.post("/api/sets/build", json={"sources": [{"kind": "library"}], "arc": "nope"}).status_code == 422
