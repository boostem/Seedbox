"""Set ordering (#60): synthetic libraries whose best order is known, by construction or by trying every order."""

from __future__ import annotations

import itertools
import math
import random
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from seedbox2.library import Library
from seedbox2.server.app import create_app
from seedbox2.sets import (
    TEMPLATES,
    OrderRequest,
    Problem,
    TempoJourney,
    TrackFeatures,
    arc_from,
    evaluate_tracks,
    order_tracks,
    solve,
)
from seedbox2.sets.arcs import ArcError, custom
from seedbox2.sets.cost import (
    Weights,
    camelot_name,
    key_index,
    key_step,
    shifted,
    tempo_cost,
    tempo_distances,
)
from seedbox2.sets.features import load_features, vocal_share
from seedbox2.sets.solver import SolverError

from .s2_ready import make_ready

FLAT = TEMPLATES["flat"]
ONLY_KEY = Weights(tempo=0, tempo_breach=0, tempo_unknown=0, half_time=0, journey=0, energy_level=0, energy_step=0, vibe=0,
                   vibe_unknown=0, vocal_run=0, genre=0)  # fmt: skip


def track(name: str, **kw) -> TrackFeatures:
    return TrackFeatures(hash=name, **kw)


def names(result: dict) -> list[str]:
    return [t["hash"] for t in result["order"]]


def shuffled(items: list, seed: int = 7) -> list:
    out = items.copy()
    random.Random(seed).shuffle(out)
    return out


def brute_force(p: Problem, locks: dict[int, int] | None = None) -> float:
    locks = locks or {}
    free = [i for i in range(p.n) if i not in locks.values()]
    slots = [q for q in range(p.n) if q not in locks]
    orders = np.zeros((math.factorial(len(free)), p.n), np.int64)
    orders[:, slots] = np.array(list(itertools.permutations(free)), np.int64)
    for q, i in locks.items():
        orders[:, q] = i
    return min(float(p.true_cost(orders[at : at + 5000]).min()) for at in range(0, orders.shape[0], 5000))


def random_tracks(n: int, seed: int) -> list[TrackFeatures]:
    r = random.Random(seed)
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        v = rng.normal(size=8)
        v /= np.linalg.norm(v)
        out.append(track(f"t{i}", bpm=r.uniform(118, 134), camelot=f"{r.randint(1, 12)}{r.choice('AB')}", energy=r.randint(0, 9) / 9,
                         vocals=r.random(), genre=r.choice(["house", "techno", "drum and bass"]),
                         vibes={"main": v, "intro": v, "outro": v}))  # fmt: skip
    return out


# --- pair costs ------------------------------------------------------------------------------------------------------


def test_camelot_moves_cost_what_a_dj_would_say():
    k = key_index
    assert key_step(k("8A"), k("8A")) == 0
    assert key_step(k("8A"), k("9A")) == key_step(k("8A"), k("7A")) == key_step(k("8A"), k("8B")) == 0.25
    assert key_step(k("12A"), k("1A")) == 0.25, "the wheel wraps"
    assert key_step(k("8A"), k("10A")) == key_step(k("8A"), k("9B")) == 1.0
    assert key_step(k("8A"), k("2A")) == key_step(k("8A"), k("3B")) == 3.0


def test_a_semitone_is_seven_steps_on_the_wheel():
    assert camelot_name(shifted(key_index("8A"), 1)) == "3A"  # A minor up is B flat minor
    assert camelot_name(shifted(key_index("8A"), -1)) == "1A"  # down is A flat minor
    assert camelot_name(shifted(key_index("8B"), 1)) == "3B"


def test_a_semitone_fix_costs_less_than_a_clash_and_more_than_a_good_move():
    w = Weights()
    assert 1.0 < w.key_fix < 3.0


def test_a_fix_stays_on_for_the_whole_track():
    tracks = [track("a", camelot="8A"), track("b", camelot="3A"), track("c", camelot="8A")]
    p = Problem(tracks, FLAT, weights=ONLY_KEY)
    # b played a semitone down... is 8A, which clears both of its clashes for one fix
    shifts = p.best_shifts([0, 1, 2])
    assert shifts.tolist() == [0, -1, 0]
    assert p.cost([0, 1, 2]) == pytest.approx(ONLY_KEY.key_fix)


def test_a_fix_isnt_taken_when_it_would_only_move_the_clash():
    # a run on each side of one clash: clearing it would take a fix on a whole run, dearer than the clash itself
    tracks = [track(k, camelot=k) for k in ("10A", "9A", "8A", "3A", "2A", "1A")]
    p = Problem(tracks, FLAT, weights=ONLY_KEY)
    assert p.best_shifts(list(range(6))).tolist() == [0] * 6
    assert p.cost(list(range(6))) == pytest.approx(3.0 + 4 * 0.25)


def test_tempo_prefers_under_five_percent_and_has_a_limit_around_eight():
    w = Weights(tempo=1.0)
    costs = tempo_cost(np.array([0.0, 2.5, 5.0, 6.5, 8.0, 8.1]), w)
    assert costs[0] == 0 and costs[1] == pytest.approx(0.25) and costs[2] == pytest.approx(1.0)
    assert np.all(np.diff(costs) > 0)
    assert costs[4] == pytest.approx(3.0)
    assert costs[5] > costs[4] + w.tempo_breach * 0.9, "over the limit is a breach"


def test_tempo_distance_knows_half_and_double_time():
    pct, factor = tempo_distances(np.array([128.0, 128.0, 87.0]), np.array([126.0, 64.0, 174.0]))
    assert pct[0] == pytest.approx(1.5625) and factor[0] == 1
    assert pct[1] == pytest.approx(0) and factor[1] == 2
    assert pct[2] == pytest.approx(0) and factor[2] == 0.5


def test_vocal_share_comes_from_the_vocals_analysis():
    assert vocal_share({"active": "1100"}) == 0.5
    assert vocal_share({"active": ""}) is None and vocal_share(None) is None


# --- arcs ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["warm_up", "peak_time", "sunset", "rollercoaster", "flat"])
def test_every_template_stays_in_range(name):
    levels = TEMPLATES[name].sample(50)
    assert levels.min() >= 0 and levels.max() <= 1


def test_template_shapes():
    assert TEMPLATES["warm_up"].direction() == 1
    assert TEMPLATES["sunset"].direction() == -1
    assert TEMPLATES["flat"].direction() == 0
    assert np.argmax(TEMPLATES["peak_time"].sample(100)) in range(55, 85)
    assert np.all(np.diff(TEMPLATES["warm_up"].sample(20)) > 0)
    assert np.all(np.diff(TEMPLATES["sunset"].sample(20)) < 0)
    swings = np.sign(np.diff(TEMPLATES["rollercoaster"].sample(60)))
    assert np.count_nonzero(np.diff(swings)) >= 4


def test_a_drawn_curve_tolerates_a_stroke_that_goes_back():
    arc = custom([[0.5, 0.2], [0.0, 1.0], [1.0, 1.0], [0.5, 0.0]])
    assert arc.at(0.5) == 0.0, "the last point at a time wins"
    assert arc.at(0.25) == pytest.approx(0.5)
    assert arc_from(None).name == "peak_time" and arc_from("sunset").name == "sunset"


@pytest.mark.parametrize("bad", [[], [[0.5, 0.5]], [[0, 0.5], [0, 0.6]], [[0, 2], [1, 0]], [[-1, 0], [1, 0]], [[0, float("nan")], [1, 0]], [[0], [1]]])
def test_a_bad_curve_is_refused(bad):
    with pytest.raises(ArcError):
        custom(bad)
    with pytest.raises(ArcError):
        arc_from("no such arc")


# --- known best orders -----------------------------------------------------------------------------------------------


def test_a_walk_round_the_wheel_is_found_from_a_shuffle():
    wheel = [track(f"{n}A", camelot=f"{n}A") for n in range(1, 13)]
    p_in = shuffled(wheel)
    pin = {"position": 0, "hash": "1A"}
    result = order_tracks(p_in, OrderRequest(arc="flat", pins=[pin], weights=ONLY_KEY))
    got = names(result)
    forward, backward = [f"{n}A" for n in range(1, 13)], ["1A", *[f"{n}A" for n in range(12, 1, -1)]]
    assert got in (forward, backward)
    assert result["cost"] == pytest.approx(11 * 0.25)
    assert result["cost"] < result["cost_before"]


def test_a_tempo_journey_sorts_the_tempos_the_way_it_goes():
    ladder = [track(f"b{b}", bpm=float(b)) for b in range(120, 129)]
    up = order_tracks(shuffled(ladder), OrderRequest(arc="flat", tempo_start=120, tempo_end=128))
    assert [t["bpm"] for t in up["order"]] == [float(b) for b in range(120, 129)]
    down = order_tracks(shuffled(ladder), OrderRequest(arc="flat", tempo_start=128, tempo_end=120))
    assert [t["bpm"] for t in down["order"]] == [float(b) for b in range(128, 119, -1)]
    assert up["tempo"] == {"start": 120, "end": 128, "auto": False}
    assert max(t["tempo_pct"] for t in up["transitions"]) < 1


def test_the_journey_is_a_slow_drift_not_a_jump():
    # two tracks at each tempo; the drift plays them in pairs and never goes back
    tracks = [track(f"{b}{x}", bpm=float(b)) for b in (120, 122, 124, 126, 128) for x in "ab"]
    got = [t["bpm"] for t in order_tracks(shuffled(tracks), OrderRequest(arc="flat", tempo_start=120, tempo_end=128))["order"]]
    assert got == sorted(got)


def test_without_a_journey_the_tempo_follows_the_arc():
    ladder = [track(f"b{b}", bpm=float(b)) for b in range(120, 129)]
    rising = order_tracks(shuffled(ladder), OrderRequest(arc="warm_up"))
    falling = order_tracks(shuffled(ladder), OrderRequest(arc="sunset"))
    assert rising["tempo"]["auto"] and rising["tempo"]["start"] < rising["tempo"]["end"]
    assert falling["tempo"]["start"] > falling["tempo"]["end"]
    assert [t["bpm"] for t in rising["order"]] == sorted(t["bpm"] for t in rising["order"])
    assert [t["bpm"] for t in falling["order"]] == sorted((t["bpm"] for t in falling["order"]), reverse=True)


def test_half_time_tracks_join_the_journey_at_their_double():
    tracks = [track("a", bpm=120.0), track("half", bpm=62.0), track("c", bpm=128.0)]
    result = order_tracks(tracks, OrderRequest(arc="flat", tempo_start=120, tempo_end=128))
    assert names(result) == ["a", "half", "c"]
    assert "half time" in result["transitions"][0]["text"] or "double time" in result["transitions"][0]["text"]


def energy_set(values: list[int]) -> list[TrackFeatures]:
    return [track(f"e{i}", energy=(v - 1) / 9) for i, v in enumerate(values)]


def ratings(result: dict) -> list[int]:
    return [t["rating"] for t in result["order"]]


def test_warm_up_climbs_and_sunset_falls():
    tracks = shuffled(energy_set(list(range(1, 11))))
    assert ratings(order_tracks(tracks, OrderRequest(arc="warm_up"))) == list(range(1, 11))
    assert ratings(order_tracks(tracks, OrderRequest(arc="sunset"))) == list(range(10, 0, -1))


def test_peak_time_peaks_two_thirds_in():
    got = ratings(order_tracks(shuffled(energy_set(list(range(1, 11)))), OrderRequest(arc="peak_time")))
    assert got.index(10) in (6, 7)
    assert got[0] < got[got.index(10)] and got[-1] < 10


def test_rollercoaster_goes_up_and_down_more_than_once():
    got = ratings(order_tracks(shuffled(energy_set([1, 1, 2, 3, 3, 4, 5, 7, 8, 8, 9, 9, 10, 10, 2, 6])), OrderRequest(arc="rollercoaster")))
    changes = np.count_nonzero(np.diff(np.sign(np.diff(got))))
    assert changes >= 3


def test_a_drawn_curve_is_followed():
    valley = [[0, 1], [0.5, 0], [1, 1]]
    got = ratings(order_tracks(shuffled(energy_set(list(range(1, 10)))), OrderRequest(arc=valley)))
    assert got.index(1) == 4
    assert got[0] >= 8 and got[-1] >= 8


def test_an_arc_scales_to_the_sets_own_range():
    mild = [track(f"m{r}", energy=r / 9) for r in (5, 6, 7, 8)]
    got = order_tracks(shuffled(mild), OrderRequest(arc="warm_up"))
    assert [t["energy"] for t in got["order"]] == sorted(t["energy"] for t in got["order"])
    assert got["order"][0]["target_energy"] == pytest.approx(5 / 9 + 3 / 9 * TEMPLATES["warm_up"].at(0), abs=0.002)


def test_vocal_tracks_dont_run_together():
    tracks = [track(f"v{i}", vocals=0.9) for i in range(4)] + [track(f"i{i}", vocals=0.0) for i in range(4)]
    result = order_tracks(tracks, OrderRequest(arc="flat"))
    got = [t["hash"][0] for t in result["order"]]
    assert "vvv" not in "".join(got)
    assert all("vocal_run" not in t["flags"] for t in result["transitions"])


def test_genres_stay_together_when_nothing_else_differs():
    tracks = [track(f"{g}{i}", genre=g) for i in range(3) for g in ("techno", "ambient")]
    result = order_tracks(tracks, OrderRequest(arc="flat"))
    assert sum("genre_change" in t["flags"] for t in result["transitions"]) == 1


def test_related_genres_arent_a_change():
    tracks = [track("a", genre="Deep House"), track("b", genre="tech-house")]
    assert Problem(tracks, FLAT).genre_change[0, 1] == 0


def test_the_vibe_groups_similar_tracks():
    x, y = np.eye(4)[0], np.eye(4)[1]
    tracks = [track(f"x{i}", vibes={"main": x}) for i in range(3)] + [track(f"y{i}", vibes={"main": y}) for i in range(3)]
    result = order_tracks(shuffled(tracks), OrderRequest(arc="flat"))
    got = "".join(t["hash"][0] for t in result["order"])
    assert got in ("xxxyyy", "yyyxxx")
    assert sum("vibe shift" in t["text"] for t in result["transitions"]) == 1


def test_a_journey_outweighs_small_jumps_back_and_forth():
    # the weights' reason: on sets of random tracks at 120-128, ask for 120 to 128 and it should stay near that line
    r = random.Random(5)
    tracks = [track(f"t{i}", bpm=round(r.uniform(120, 128), 1), camelot=f"{r.randint(5, 11)}A", energy=r.randint(1, 9) / 9) for i in range(20)]
    result = order_tracks(tracks, OrderRequest(arc="peak_time", tempo_start=120, tempo_end=128))
    away = [abs(t["bpm"] / t["target_bpm"] - 1) * 100 for t in result["order"]]
    assert sum(away) / len(away) < 2.0
    assert max(t["tempo_pct"] for t in result["transitions"]) < 5.0


def test_it_goes_round_a_tempo_breach_when_it_can():
    tracks = [track("a", bpm=120.0), track("b", bpm=124.0), track("c", bpm=128.0), track("d", bpm=132.0), track("e", bpm=136.0)]
    bad = [tracks[0], tracks[4], tracks[1], tracks[3], tracks[2]]
    result = order_tracks(bad, OrderRequest(arc="flat", tempo_start=128, tempo_end=128))
    assert all(t["tempo_pct"] <= 8 for t in result["transitions"])
    assert not any("tempo_over_limit" in t["flags"] for t in result["transitions"])


def test_a_breach_nobody_can_avoid_is_named():
    result = order_tracks([track("a", bpm=120.0), track("b", bpm=150.0)], OrderRequest(arc="flat"))
    flags = result["transitions"][0]["flags"]
    assert "tempo_over_limit" in flags and "over the limit" in result["transitions"][0]["text"]


@pytest.mark.parametrize("seed", range(12))
def test_it_finds_the_best_order_on_small_random_sets(seed):
    p = Problem(random_tracks(7, seed), TEMPLATES["peak_time"])
    assert solve(p).cost == pytest.approx(brute_force(p), abs=1e-6)


@pytest.mark.parametrize("seed", range(3))
def test_it_finds_the_best_order_with_pins(seed):
    p = Problem(random_tracks(7, 100 + seed), TEMPLATES["warm_up"])
    locks = {0: 3, 3: 1, 6: 5}
    got = solve(p, locks)
    assert {q: got.order[q] for q in locks} == locks
    assert got.cost == pytest.approx(brute_force(p, locks), abs=1e-6)


@pytest.mark.parametrize("seed", range(4))
def test_it_comes_close_where_it_cant_try_everything(seed):
    p = Problem(random_tracks(9, 200 + seed), TEMPLATES["rollercoaster"])
    assert solve(p).cost <= brute_force(p) * 1.03


def test_true_cost_is_cost_at_the_best_shifts():
    p = Problem(random_tracks(9, 4), TEMPLATES["peak_time"])
    orders = np.array([random.Random(i).sample(range(9), 9) for i in range(20)], np.int64)
    assert p.true_cost(orders) == pytest.approx([p.cost(o.tolist()) for o in orders])


# --- pins ------------------------------------------------------------------------------------------------------------


def test_pins_hold_on_a_long_set_and_the_rest_still_improves():
    tracks = random_tracks(40, 3)
    pins = [{"position": 0, "hash": "t17"}, {"position": 20, "hash": "t3"}, {"position": 39, "hash": "t25"}]
    result = order_tracks(tracks, OrderRequest(pins=pins))
    got = names(result)
    assert got[0] == "t17" and got[20] == "t3" and got[39] == "t25"
    assert sorted(got) == sorted(t.hash for t in tracks)
    assert [t["pinned"] for t in result["order"]].count(True) == 3
    assert result["cost"] < result["cost_before"]


def test_a_track_that_comes_back_can_be_pinned_at_both_places():
    tracks = [track("a", energy=0.1), track("b", energy=0.5), track("a", energy=0.1), track("c", energy=0.9)]
    pins = [{"position": 0, "hash": "a"}, {"position": 3, "hash": "a"}]
    got = names(order_tracks(tracks, OrderRequest(pins=pins)))
    assert got[0] == got[3] == "a" and sorted(got) == ["a", "a", "b", "c"]


@pytest.mark.parametrize("pins", [
    [{"position": 9, "hash": "a"}],
    [{"position": 0, "hash": "nope"}],
    [{"position": 0, "hash": "a"}, {"position": 1, "hash": "a"}],
    [{"position": 0, "hash": "a"}, {"position": 0, "hash": "b"}],
])  # fmt: skip
def test_impossible_pins_are_refused(pins):
    with pytest.raises(SolverError):
        order_tracks([track("a"), track("b")], OrderRequest(pins=pins))


# --- explanations ----------------------------------------------------------------------------------------------------


def test_the_explanation_says_what_the_move_is():
    v = np.eye(3)[0]
    w = np.eye(3)[1]
    tracks = [track("a", energy=4 / 9, camelot="8A", bpm=124.0, vibes={"main": v}),
              track("b", energy=5 / 9, camelot="9A", bpm=124.0, vibes={"main": v}),
              track("c", energy=5 / 9, camelot="9A", bpm=124.0, vibes={"main": w})]  # fmt: skip
    t = evaluate_tracks(tracks, OrderRequest(arc="flat"))["transitions"]
    assert t[0]["text"] == "energy +1, 8A → 9A, same vibe"
    assert t[0]["key_move"] == "+1 on the wheel" and t[0]["energy_change"] == 1
    assert t[1]["text"] == "energy level, 9A → 9A, vibe shift"
    assert set(t[0]["terms"]) == {"key", "tempo", "energy", "journey", "vibe", "genre", "vocals"}


def test_the_explanation_shows_a_semitone_fix():
    tracks = [track("a", camelot="8A"), track("b", camelot="3A"), track("c", camelot="8A")]
    result = evaluate_tracks(tracks, OrderRequest(arc="flat"))
    assert result["order"][1]["shift"] == -1
    assert "8A → 8A (3A -1 st)" in result["transitions"][0]["text"] and "key_fix" in result["transitions"][0]["flags"]
    assert "8A (3A -1 st) → 8A" in result["transitions"][1]["text"]


def test_the_explanation_shows_a_clash_and_big_tempo_changes():
    tracks = [track("a", camelot="8A", bpm=120.0), track("b", camelot="2B", bpm=127.0), track("c", camelot="2B", bpm=128.0)]
    t = evaluate_tracks(tracks, OrderRequest(arc="flat", weights=Weights(key_fix=9.0)))["transitions"]
    assert "key_clash" in t[0]["flags"] and "tempo_wide" in t[0]["flags"] and "tempo +5.8 %" in t[0]["text"]
    assert "tempo" not in t[1]["text"], "a small tempo change isn't worth a word"


def test_the_cost_of_the_transitions_adds_up():
    tracks = random_tracks(12, 11)
    result = order_tracks(tracks)
    # everything but the opener's position is in a transition's terms
    p = Problem(tracks, TEMPLATES["peak_time"])
    opener = result["order"][0]["from_position"]
    fix = p.w.key_fix if result["order"][0]["shift"] else 0.0
    assert sum(t["cost"] for t in result["transitions"]) + p.slot[opener, 0] + fix == pytest.approx(result["cost"], abs=0.02)


# --- what isn't known, edges, speed ----------------------------------------------------------------------------------


def test_nothing_known_keeps_the_order_as_it_was():
    tracks = [track(f"t{i}") for i in range(8)]
    result = order_tracks(tracks)
    assert names(result) == [t.hash for t in tracks]
    assert result["unknown"] == {"bpm": 8, "key": 8, "energy": 8, "vibe": 8, "vocals": 8, "genre": 8}
    assert result["tempo"] is None
    assert "energy unknown, key unknown, tempo unknown" in result["transitions"][0]["text"]


def test_a_known_move_beats_an_unknown_one():
    tracks = [track("a", camelot="8A"), track("x"), track("b", camelot="9A")]
    p = Problem(tracks, FLAT, weights=ONLY_KEY)
    # a-b is known and good, so the unknown goes at an end
    assert solve(p).order[1] != 1


def test_a_partly_analysed_set_still_gets_an_order():
    tracks = random_tracks(10, 5)
    tracks[2].bpm = None
    tracks[4].camelot = None
    tracks[6].energy = None
    tracks[7].vibes = {}
    tracks[8].vocals = None
    tracks[9].genre = None
    result = order_tracks(tracks)
    assert sorted(names(result)) == sorted(t.hash for t in tracks)
    assert np.isfinite(result["cost"])


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_tiny_sets(n):
    tracks = random_tracks(n, 2)
    result = order_tracks(tracks)
    assert sorted(names(result)) == sorted(t.hash for t in tracks)
    assert len(result["transitions"]) == max(n - 1, 0)


def test_all_pinned_is_the_pinned_order():
    tracks = random_tracks(4, 1)
    pins = [{"position": i, "hash": f"t{3 - i}"} for i in range(4)]
    assert names(order_tracks(tracks, OrderRequest(pins=pins))) == ["t3", "t2", "t1", "t0"]


def test_the_same_set_always_gets_the_same_order():
    tracks = random_tracks(30, 9)
    assert names(order_tracks(tracks)) == names(order_tracks(tracks))


def test_it_is_never_worse_than_the_order_it_was_given():
    for seed in range(4):
        result = order_tracks(random_tracks(25, 50 + seed))
        assert result["cost"] <= result["cost_before"]


def test_a_two_hour_set_orders_in_about_a_second():
    tracks = random_tracks(30, 21)
    start = time.perf_counter()
    order_tracks(tracks)
    assert time.perf_counter() - start < 3.0, "generous for a loaded CI machine: it takes about 0.4 s"


def test_a_better_order_is_found_on_a_big_structured_set():
    # 48 tracks with a perfect order: the wheel twice round, tempo and energy rising with it
    perfect = [track(f"t{i:02}", camelot=f"{i % 12 + 1}A", bpm=120.0 + i * 8 / 47, energy=i / 47) for i in range(48)]
    result = order_tracks(shuffled(perfect), OrderRequest(arc="warm_up", tempo_start=120, tempo_end=128))
    p = Problem(shuffled(perfect), TEMPLATES["warm_up"], TempoJourney(120, 128))
    ideal = p.cost([shuffled(perfect).index(t) for t in perfect])
    assert result["cost"] <= ideal * 1.1
    moves = [t["key_move"] for t in result["transitions"]]
    assert moves.count("clash") == 0


# --- the library and the API -----------------------------------------------------------------------------------------


def seed_library(lib: Library, n: int = 5) -> list[str]:
    hashes = []
    for i in range(n):
        h = f"{i:040x}"
        lib.upsert_track(h, 240.0, title=f"Song {i}", artist="Someone")
        lib.put_analysis(h, "beats", "1", {"bpm": 120.0 + i})
        lib.put_analysis(h, "key", "1", {"camelot": f"{i + 1}A", "key": "A minor", "open_key": "1m"})
        lib.put_analysis(h, "energy", "1", {"energy": i / 9, "rating": i + 1})
        hashes.append(h)
    make_ready(lib, *hashes)
    return hashes


@pytest.fixture
def lib(tmp_path):
    library = Library.open(tmp_path / "lib.sqlite3")
    yield library
    library.close()


def test_tags_arent_taken_for_analysis(lib):
    lib.upsert_track("a" * 40, 200.0, title="Tagged", tags={"file": {"bpm": 128, "key": "8A", "genre": "House"}, "tunebat": {"energy": 7}})
    f = load_features(lib, ["a" * 40])["a" * 40]
    assert f.bpm is None and f.camelot is None and f.energy is None, "a tag's guess isn't our analysis"
    assert f.genre == "House", "no analysis gives a genre, so the tag is what there is"


def test_features_read_the_analysis_and_the_key_override(lib):
    h = seed_library(lib, 1)[0]
    lib.put_analysis(h, "vocals", "1", {"active": "1110"})
    f = load_features(lib, [h])[h]
    assert (f.bpm, f.camelot, f.energy, f.vocals) == (120.0, "1A", 0.0, 0.75)
    lib.put_analysis(h, "key_override", "1", {"camelot": "5B", "key": "x", "open_key": "y"})
    assert load_features(lib, [h])[h].camelot == "5B"


def test_a_missing_key_analysis_doesnt_become_a_hint(lib):
    lib.upsert_track("b" * 40, 200.0, title="Tagged", tags={"file": {"key": "8A"}})
    lib.put_analysis("b" * 40, "key", "1", {"camelot": None, "key": None})
    assert load_features(lib, ["b" * 40])["b" * 40].camelot is None


@pytest.fixture
def client(tmp_path):
    app = create_app(library_path=tmp_path / "lib.sqlite3", start_jobs=False)
    with TestClient(app) as c:
        yield c


def lib_of(client) -> Library:
    from seedbox2.library import Library as L

    state = client.app.state
    if state.library is None:
        client.get("/api/sets")
    assert isinstance(state.library, L)
    return state.library


def test_order_over_http(client):
    lib = lib_of(client)
    hashes = seed_library(lib)
    shuffle = [hashes[i] for i in (3, 0, 4, 1, 2)]
    set_id = client.post("/api/sets", json={"name": "Friday", "hashes": shuffle}).json()["id"]

    arcs = client.get("/api/sets/arcs").json()
    assert [a["name"] for a in arcs] == ["warm_up", "peak_time", "sunset", "rollercoaster", "flat"]

    now = client.get(f"/api/sets/{set_id}/order").json()
    assert [t["hash"] for t in now["order"]] == shuffle and now["settings"]["arc"] is None

    body = {"arc": "warm_up", "tempo_start": 120, "tempo_end": 124, "pins": [{"position": 0, "hash": hashes[0]}]}
    solved = client.post(f"/api/sets/{set_id}/order", json=body).json()
    assert [t["hash"] for t in solved["order"]] == hashes
    assert solved["cost"] < solved["cost_before"]
    assert solved["order"][0]["pinned"] and "transitions" in solved
    assert client.get(f"/api/sets/{set_id}/hashes").json()["hashes"] == shuffle, "solving saves nothing"

    applied = client.put(f"/api/sets/{set_id}/order", json={**body, "hashes": hashes})
    assert applied.status_code == 200
    assert client.get(f"/api/sets/{set_id}/hashes").json()["hashes"] == hashes
    again = client.get(f"/api/sets/{set_id}/order").json()
    assert again["settings"]["arc"] == "warm_up" and again["settings"]["pins"] == body["pins"]
    assert again["settings"]["tempo_end"] == 124 and again["order"][0]["pinned"]
    assert again["cost"] == solved["cost"]


def test_order_refuses_what_makes_no_sense(client):
    lib = lib_of(client)
    hashes = seed_library(lib, 3)
    set_id = client.post("/api/sets", json={"name": "F", "hashes": hashes}).json()["id"]
    url = f"/api/sets/{set_id}/order"
    assert client.post("/api/sets/99/order", json={}).status_code == 404
    assert client.get("/api/sets/99/order").status_code == 404
    assert client.put("/api/sets/99/order", json={"hashes": []}).status_code == 404
    assert client.post(url, json={"arc": "nope"}).status_code == 422
    assert client.post(url, json={"arc": [[0, 5], [1, 0]]}).status_code == 422
    assert client.post(url, json={"tempo_start": 120}).status_code == 422
    assert client.post(url, json={"pins": [{"position": 0, "hash": "nope"}]}).status_code == 422
    assert client.put(url, json={"hashes": hashes[:2]}).status_code == 409, "the set has other tracks"
    pins = [{"position": 1, "hash": hashes[0]}]
    assert client.put(url, json={"hashes": hashes, "pins": pins}).status_code == 422, "the pin isn't where this order puts it"
    assert client.get(f"/api/sets/{set_id}/hashes").json()["hashes"] == hashes


def test_a_pin_on_a_track_since_moved_is_dropped(client):
    lib = lib_of(client)
    hashes = seed_library(lib, 3)
    set_id = client.post("/api/sets", json={"name": "F", "hashes": hashes}).json()["id"]
    pins = [{"position": 0, "hash": hashes[0]}]
    assert client.put(f"/api/sets/{set_id}/order", json={"hashes": hashes, "pins": pins}).status_code == 200
    lib.save_set("F", list(reversed(hashes)), set_id=set_id)
    assert client.get(f"/api/sets/{set_id}/order").json()["settings"]["pins"] == []


def test_a_track_rated_1_has_an_energy_of_0_not_none(lib):
    h = seed_library(lib, 1)[0]  # the first is at energy 0
    row = lib.view_rows(hashes=[h])[0]
    assert row["energy"] == 0.0 and row["energy_from"] == "analysis"


# --- the whole-set timeline (#75) -------------------------------------------------------------------------------------


def _analysed(library: Library, name: str, bpm: float = 124.0, duration: float = 300.0) -> str:
    from seedbox2.analysis import beats, energy

    hash_ = name * 40
    library.upsert_track(hash_, duration, title=name, artist="X")
    library.put_analysis(hash_, beats.ANALYSER, beats.VERSION,
                         {"segments": [{"start_beat": 0.0, "start_s": 0.5, "bpm": bpm}], "bpm": bpm})  # fmt: skip
    bars = np.arange(0.5, duration, 240 / bpm).round(3).tolist()
    n = len(bars) - 1
    library.put_analysis(hash_, energy.ANALYSER, energy.VERSION, {
        "bar_s": bars, "curve": [min(1.0, i / 20, (n - i) / 20) for i in range(n)], "features": {"bass": [0.8] * n},
        "rating": 5, "energy": 0.4,
    })  # fmt: skip
    make_ready(library, hash_)
    return hash_


class _Jobs:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []

    def submit(self, kind: str, payload: dict, **_) -> dict:
        self.submitted.append((kind, payload))
        return {"id": "x", "state": "queued"}


class _Ctx:
    def __init__(self, library: Library) -> None:
        self.library = library


def test_a_pair_without_a_plan_is_pending_and_goes_to_the_job(lib):
    from seedbox2.sets.timeline import JOB, timeline

    a, b = _analysed(lib, "a"), _analysed(lib, "b")
    jobs = _Jobs()
    t = timeline(lib, [a, b], jobs)
    assert [x["state"] for x in t["transitions"]] == ["pending"]
    assert jobs.submitted == [(JOB, {"from": a, "to": b})]
    assert timeline(lib, [a, b])["transitions"][0]["state"] == "pending"  # without jobs it only reads


def test_a_track_without_beats_waits_for_them(lib):
    from seedbox2.sets.timeline import timeline

    a = _analysed(lib, "a")
    b = "b" * 40
    lib.upsert_track(b, 200.0, title="b")
    jobs = _Jobs()
    t = timeline(lib, [a, b], jobs)
    assert t["transitions"][0]["state"] == "waiting"
    assert jobs.submitted == [("beats", {"hash": b})]


def test_the_job_plans_the_pair_from_our_analysis(lib):
    from seedbox2.mix import convert
    from seedbox2.sets.timeline import set_plan_job, timeline

    a, b = _analysed(lib, "a"), _analysed(lib, "b", bpm=126.0)
    out = set_plan_job({"from": a, "to": b}, _Ctx(lib))
    assert out["planned"] is True
    row = lib.plan(a, b)
    assert row["user_edited"] == 0
    plan = convert.load(row["plan"], from_track=a, to_track=b)
    assert plan.version == 3
    assert (plan.a.track_id, plan.b.track_id) == (a, b)
    assert plan.a.grid[0].bpm == pytest.approx(124.0)  # on our grid, not a guessed one
    item = timeline(lib, [a, b])["transitions"][0]
    assert item["state"] == "ready"
    assert item["technique"] == plan.technique
    # a blend: near A's end and B's start, as long on both files, within the pitch between the tempos
    assert item["a_in_s"] > 200 and item["b_in_s"] < 60
    assert item["seconds"] > 0
    assert (item["a_out_s"] - item["a_in_s"]) == pytest.approx(item["seconds"], rel=0.03)
    assert (item["b_out_s"] - item["b_in_s"]) == pytest.approx(item["seconds"], rel=0.03)


def test_the_job_leaves_a_saved_plan_alone(lib):
    from seedbox2.sets.timeline import plan_pair, set_plan_job

    a, b = _analysed(lib, "a"), _analysed(lib, "b")
    edited = plan_pair(lib, a, b).model_copy(update={"edited": True, "notes": ["mine"]})
    lib.put_plan(a, b, edited.model_dump(), user_edited=True)
    assert set_plan_job({"from": a, "to": b}, _Ctx(lib))["planned"] is False
    assert lib.plan(a, b)["plan"]["notes"] == ["mine"]


def test_a_failed_plan_says_so_until_it_is_tried_again(lib):
    from seedbox2.sets.timeline import JOB, timeline

    a, b = _analysed(lib, "a"), _analysed(lib, "b")
    job = lib.add_job(JOB, {"from": a, "to": b})
    lib.update_job(job["id"], state="failed", error="no good")
    jobs = _Jobs()
    item = timeline(lib, [a, b], jobs)["transitions"][0]
    assert (item["state"], item["error"]) == ("failed", "no good")
    assert jobs.submitted == []
    assert timeline(lib, [a, b], jobs, retry=True)["transitions"][0]["state"] == "pending"
    assert jobs.submitted == [(JOB, {"from": a, "to": b})]


def test_an_unreadable_plan_is_failed_not_a_500(lib):
    from seedbox2.sets.timeline import timeline

    a, b = _analysed(lib, "a"), _analysed(lib, "b")
    lib.put_plan(a, b, {"not": "a plan"}, user_edited=False)
    assert timeline(lib, [a, b])["transitions"][0]["state"] == "failed"


def test_timeline_tracks_have_sections_and_a_thin_curve(lib):
    from seedbox2.sets.timeline import timeline

    a, b = _analysed(lib, "a", duration=600.0), _analysed(lib, "b")
    t = timeline(lib, [a, b, a])  # a track can come back
    assert [x["hash"] for x in t["tracks"]] == [a, b, a]
    view = t["tracks"][0]
    assert view["duration_s"] == 600.0
    assert view["bar_s"] == pytest.approx(240 / 124, abs=1e-3)
    assert {s["kind"] for s in view["sections"]} >= {"intro", "outro"}
    assert 0 < len(view["curve"]) <= 64
    assert [p[0] for p in view["curve"]] == sorted(p[0] for p in view["curve"])
    assert len(t["transitions"]) == 2


def test_timeline_of_a_track_with_no_analysis_still_answers(lib):
    from seedbox2.sets.timeline import timeline

    a = "a" * 40
    lib.upsert_track(a, 180.0, title="a")
    view = timeline(lib, [a])["tracks"][0]
    assert (view["sections"], view["curve"], view["bar_s"]) == ([], [], None)


def test_timeline_route(client):
    lib = lib_of(client)
    a, b = _analysed(lib, "a"), _analysed(lib, "b")
    set_id = client.post("/api/sets", json={"name": "S", "hashes": [a, b]}).json()["id"]
    r = client.post(f"/api/sets/{set_id}/timeline", json={"hashes": [b, a]})
    assert r.status_code == 200
    assert r.json()["transitions"][0]["from"] == b
    assert client.post(f"/api/sets/{set_id}/timeline", json={"hashes": [a]}).status_code == 409
    assert client.post("/api/sets/999/timeline", json={"hashes": []}).status_code == 404
