"""Planning the whole set with lookahead (#61): options per pair, the variety rules' pass, the set's length, the joint
search over order and options, and the way into the `set_plan` job and the API. Synthetic tracks throughout."""

from __future__ import annotations

import itertools
import math
import random
import time

import numpy as np
import pytest

from seedbox2.mix import styles as style_lib
from seedbox2.sets import TEMPLATES, Problem, TrackFeatures
from seedbox2.sets import setplan as sp
from seedbox2.sets.setplan import LENGTH_SLACK_S, Arrays, Option, Rules, SetPlanner, Shape, Weights, step
from seedbox2.sets.solver import solve

GENRES = ["house", "techno", "drum_and_bass"]


def track(i: int, r: random.Random, rng: np.random.Generator) -> TrackFeatures:
    v = rng.normal(size=8)
    v /= np.linalg.norm(v)
    return TrackFeatures(hash=f"t{i}", bpm=r.uniform(118, 134), camelot=f"{r.randint(1, 12)}{r.choice('AB')}",
                         energy=r.randint(0, 9) / 9, vocals=r.random(), genre=r.choice(GENRES),
                         vibes={"main": v, "intro": v, "outro": v})  # fmt: skip


def shape_of(t: TrackFeatures, r: random.Random, duration: float | None = None) -> Shape:
    """A track in `timeline.seedbox1_analysis`'s shape: a 32-bar intro and outro, two 32-bar drops."""
    bpm = float(t.bpm or 124.0)
    bar = 240 / bpm
    dur = duration or r.uniform(300, 420)
    drops = [r.uniform(60, 90), r.uniform(180, 230)]
    sections = [{"kind": "intro", "start_s": 0.0, "end_s": 32 * bar}, *(
        {"kind": "drop", "start_s": d, "end_s": d + 32 * bar} for d in drops),
        {"kind": "outro", "start_s": dur - 32 * bar, "end_s": dur}]  # fmt: skip
    n = int(dur / bar)
    an = {"bpm": bpm, "bar_s": bar, "first_downbeat_s": 0.0, "duration_s": dur, "bar_count": n,
          "energy_per_bar": [t.energy or 0.5] * n, "vocal_per_bar": [float(r.random() > 0.6)] * n,
          "sections": sections, "intro": sections[0], "outro": sections[-1]}  # fmt: skip
    return Shape(t.hash, dur, an, genre=t.genre, energy=t.energy)


def synthetic(n: int, seed: int, arc: str = "peak_time") -> tuple[Problem, list[Shape]]:
    r, rng = random.Random(seed), np.random.default_rng(seed)
    tracks = [track(i, r, rng) for i in range(n)]
    return Problem(tracks, TEMPLATES[arc]), [shape_of(t, r) for t in tracks]


def total(pl: SetPlanner, order: list[int]) -> float:
    return pl.p.cost(order) + pl.best_plan(order)[0]


# --- options ---------------------------------------------------------------------------------------------------------


def test_options_are_styles_that_apply_on_cues_from_the_sections():
    p, shapes = synthetic(2, 1)
    pl = SetPlanner(p, shapes)
    opts = pl.options(0, 1).options
    assert opts and len(opts) <= sp.MAX_OPTIONS
    ctx = sp._context(shapes[0], shapes[1])
    applying = {s.technique for s in style_lib.rank(ctx)}
    a, b = shapes
    for o in opts:
        assert o.technique in applying
        assert sp.A_MIN_SHARE * a.duration_s <= o.a_out_s <= a.duration_s
        assert 0 <= o.b_in_s <= sp.B_MAX_SHARE * b.duration_s
        assert o.loss == pytest.approx(a.duration_s - o.a_out_s + o.b_in_s, abs=1e-3)
        assert o.a_cue in Weights().cue_a and o.b_cue in Weights().cue_b
    assert {o.a_cue for o in opts} > {"outro"}, "a shorter way out of A is on offer, for the set's length"


def test_a_cut_comes_in_on_bs_drop_and_a_blend_lands_its_end_there():
    _, shapes = synthetic(2, 2)
    b = shapes[1]
    drop = b.sections("drop")[0]["start_s"]
    cut = [c for c in sp.b_cues(b, 0.0, cut=True) if c[0] == "drop"]
    blend = [c for c in sp.b_cues(b, 16 * b.bar_s, cut=False) if c[0] == "drop"]
    assert cut[0][1] == pytest.approx(drop, abs=b.bar_s) and cut[0][2] is True
    assert blend[0][1] + 16 * b.bar_s == pytest.approx(drop, abs=b.bar_s) and blend[0][2] is False


def test_a_track_without_beats_gets_one_plain_option():
    p, shapes = synthetic(2, 3)
    shapes[1] = Shape(shapes[1].hash, 200.0)
    opts = SetPlanner(p, shapes).options(0, 1).options
    assert [o.technique for o in opts] == [sp.UNKNOWN]
    assert opts[0].loss == 0.0


# --- the pass --------------------------------------------------------------------------------------------------------


def _toy_rules_and_pairs(seed: int, transitions: int = 4) -> tuple[Rules, list[Arrays], list[np.ndarray]]:
    """A few styles and random options: small enough to try every choice."""
    rng = np.random.default_rng(seed)
    kinds = ["blend", "blend", "cut", "echo"]
    rules = Rules([(f"s{i}", k) for i, k in enumerate(kinds)] + [(sp.UNKNOWN, "blend")], 2, Weights())
    pairs = []
    for _ in range(transitions):
        opts = [Option(int(rng.integers(0, 4)), "x", kinds[0], int(rng.choice([8, 16])), "end", 0.0, "start", 0.0,
                       float(rng.normal()), 0.0) for _ in range(5)]  # fmt: skip
        opts = [Option(o.style, "x", kinds[o.style], o.bars, o.a_cue, 0.0, o.b_cue, 0.0, o.fit, 0.0) for o in opts]
        pairs.append(Arrays.of(opts))
    return rules, pairs, [rng.normal(size=pr.fit.size) * 0.3 for pr in pairs]


def _brute(rules: Rules, pairs: list[Arrays], extra: list[np.ndarray]) -> float:
    best = math.inf
    for choice in itertools.product(*(range(pr.fit.size) for pr in pairs)):
        state, cost = rules.start, 0.0
        for pr, e, o in zip(pairs, extra, choice, strict=True):
            rule = int(pr.rule[o])
            cost += rules.PEN[rule, state] + pr.fit[o] + e[o]
            state = int(rules.NEXT[rule, state])
        best = min(best, cost)
    return best


@pytest.mark.parametrize("seed", range(6))
def test_the_pass_finds_the_cheapest_choice_of_every_one(seed):
    rules, pairs, extra = _toy_rules_and_pairs(seed)
    chain = sp.Chain(rules, pairs, extra)
    assert chain.cost == pytest.approx(_brute(rules, pairs, extra))
    # the backward tables agree with the forward ones, and a window re-run gives the whole pass's cost
    assert float((chain.alpha[0] + chain.beta[0]).min()) == pytest.approx(chain.cost)
    assert chain.window(1, pairs[1:3], extra[1:3]) == pytest.approx(chain.cost)
    swapped = [pairs[2], pairs[1]]
    assert chain.window(1, swapped, [extra[2], extra[1]]) == pytest.approx(
        _brute(rules, [pairs[0], *swapped, pairs[3]], [extra[0], extra[2], extra[1], extra[3]]))


def test_rules_never_let_a_style_play_three_times_running():
    rules = Rules([("a", "blend"), ("b", "cut"), (sp.UNKNOWN, "blend")], 4, Weights())
    a = np.full(rules.n_states, math.inf)
    a[rules.start] = 0.0
    only_a = Arrays.of([Option(0, "a", "blend", 8, "end", 0.0, "start", 0.0, 0.0, 0.0)])
    for _ in range(2):
        a, _ = step(rules, a, only_a, np.zeros(1))
    assert np.isfinite(a).any()
    a, _ = step(rules, a, only_a, np.zeros(1))
    assert not np.isfinite(a).any()


@pytest.mark.parametrize("seed", range(4))
def test_a_planned_set_keeps_the_variety_rules(seed):
    p, shapes = synthetic(24, seed)
    pl = SetPlanner(p, shapes, long_every=3)
    result = pl.plan_order(list(range(24)))
    opts = [t.option for t in result.transitions]
    assert len(opts) == 23
    for x, y, z in zip(opts, opts[1:], opts[2:]):
        assert not (x.style == y.style == z.style), "a style three times running"
    offers = [any(o.long for o in pl.options(a, b).options) for a, b in itertools.pairwise(result.order)]
    since = 0
    for k, o in enumerate(opts):
        since = 0 if o.long else since + 1
        if since >= 3:  # only where none of the last three pairs could be a long blend (a tempo gap, say)
            assert not any(offers[k - 2 : k + 1]), f"no long blend in the 3 transitions up to {k}"


def test_cuts_go_to_the_peaks():
    # the same pair all through the set; the arc climbs from nothing to its peak. Cuts may be cheap, but only up there.
    p, shapes = synthetic(1, 5)
    t = p.tracks[0]
    r = random.Random(1)
    tracks = [TrackFeatures(hash=f"t{i}", bpm=126.0, camelot="8A", energy=i / 15, genre="house") for i in range(16)]
    shapes = [shape_of(x, r, duration=360.0) for x in tracks]
    del t
    p = Problem(tracks, TEMPLATES["warm_up"])
    pl = SetPlanner(p, shapes, weights=Weights(cut_peak=4.0))
    picked = [tr.option for tr in pl.plan_order(list(range(16))).transitions]
    cuts = [k for k, o in enumerate(picked) if o.kind == "cut"]
    assert cuts, "the set's peak gets its cuts"
    assert min(cuts) >= 8, f"no cut in the quiet first half: {cuts}"


# --- the set's length ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("minutes", [65, 75, 85])
def test_the_set_comes_out_at_its_target_length(minutes):
    p, shapes = synthetic(16, 8)
    pl = SetPlanner(p, shapes, target_s=minutes * 60)
    result = pl.plan_order(list(range(16)))
    assert abs(result.seconds - minutes * 60) <= LENGTH_SLACK_S + 30
    # how much of each track plays adds up to it: each transition's overlap is played once
    overlaps = sum(t.option.bars * shapes[i].bar_s for t, i in zip(result.transitions, result.order, strict=False))
    plays = sum(b - a for a, b in result.plays)
    assert plays - overlaps == pytest.approx(result.seconds, abs=5.0)
    for (a, b), i in zip(result.plays, result.order, strict=True):
        assert 0 <= a < b <= shapes[i].duration_s


def test_without_a_target_the_tracks_play_out():
    p, shapes = synthetic(10, 9)
    loose = SetPlanner(p, shapes).plan_order(list(range(10)))
    short = SetPlanner(p, shapes, target_s=30 * 60).plan_order(list(range(10)))
    assert short.seconds < loose.seconds
    assert loose.target_s is None


# --- order and options together --------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(3))
def test_the_joint_plan_is_never_worse_than_60s_order_with_its_best_plan(seed):
    p, shapes = synthetic(14, seed)
    pl = SetPlanner(p, shapes, target_s=60 * 60)
    result = pl.solve()
    base = solve(p).order
    assert result.cost <= total(pl, base) + 1e-6
    assert result.cost == pytest.approx(total(pl, result.order))
    assert sorted(result.order) == list(range(14))
    assert result.found_by in ("as it stands", "solver", "lookahead")


def test_the_lookahead_can_beat_60s_order_when_the_plan_matters():
    # #60 can't see the transitions: with keys and tempos all alike, it has nothing to go on, and the plan everything
    better = 0
    for seed in range(4):
        r, rng = random.Random(seed), np.random.default_rng(seed)
        tracks = [TrackFeatures(hash=f"t{i}", bpm=126.0, camelot="8A", energy=0.5, genre=r.choice(GENRES)) for i in range(10)]
        shapes = [shape_of(t, r) for t in tracks]
        del rng
        p = Problem(tracks, TEMPLATES["flat"])
        pl = SetPlanner(p, shapes, target_s=50 * 60)
        result = pl.solve()
        better += result.cost < total(pl, solve(p).order) - 1e-6
    assert better >= 2


def test_pins_hold_in_the_joint_search():
    p, shapes = synthetic(12, 4)
    pl = SetPlanner(p, shapes)
    result = pl.solve({0: 5, 11: 2, 6: 9})
    assert result.order[0] == 5 and result.order[11] == 2 and result.order[6] == 9


def test_the_same_set_always_gets_the_same_plan():
    p, shapes = synthetic(12, 6)
    one = SetPlanner(p, shapes, target_s=3600).solve()
    two = SetPlanner(p, shapes, target_s=3600).solve()
    assert one.order == two.order
    assert [t.option for t in one.transitions] == [t.option for t in two.transitions]


def test_thirty_tracks_plan_within_the_budget():
    p, shapes = synthetic(30, 21)
    start = time.perf_counter()
    result = SetPlanner(p, shapes, target_s=120 * 60).solve()
    elapsed = time.perf_counter() - start
    assert elapsed < 4.0, f"{elapsed:.2f} s: generous for a loaded CI machine; the budget is 2 s and it takes about 1"
    assert len(result.transitions) == 29


def test_moving_one_track_plans_again_from_the_cache(monkeypatch):
    p, shapes = synthetic(30, 22)
    pl = SetPlanner(p, shapes, target_s=120 * 60)
    order = pl.solve().order
    calls = []
    real = sp.pair_options
    monkeypatch.setattr(sp, "pair_options", lambda *a, **k: calls.append(1) or real(*a, **k))
    moved = list(order)
    moved.insert(4, moved.pop(20))
    start = time.perf_counter()
    again = pl.plan_order(moved)
    elapsed = time.perf_counter() - start
    assert len(calls) <= 3, "only the three new pairs are worked out"
    assert elapsed < 0.5, f"{elapsed:.3f} s"
    assert again.order == moved


# --- the library, the job and the API --------------------------------------------------------------------------------


def _analysed(library, name: str, bpm: float = 124.0, duration: float = 300.0) -> str:
    from tests.test_seedbox2_sets import _analysed as make

    return make(library, name, bpm, duration)


@pytest.fixture
def lib(tmp_path):
    from seedbox2.library import Library

    library = Library.open(tmp_path / "lib.sqlite3")
    yield library
    library.close()


def test_the_set_plan_reads_the_library(lib):
    hashes = [_analysed(lib, c, bpm=122.0 + i) for i, c in enumerate("abcd")]
    out = sp.plan_hashes(lib, hashes, target_s=15 * 60)
    assert out["order"] == hashes
    assert [(t["from"], t["to"]) for t in out["transitions"]] == list(itertools.pairwise(hashes))
    assert all(t["technique"] in style_lib.TECHNIQUES for t in out["transitions"])
    assert len(out["plays"]) == 4 and out["seconds"] > 0
    reordered = sp.plan_hashes(lib, hashes, reorder=True)
    assert sorted(reordered["order"]) == sorted(hashes)


def test_the_job_plans_the_pair_on_the_set_plans_pick(lib):
    from seedbox2.mix import convert
    from seedbox2.sets.timeline import JOB, set_plan_job, timeline
    from tests.test_seedbox2_sets import _Ctx, _Jobs

    a, b = _analysed(lib, "a"), _analysed(lib, "b", bpm=125.0)
    jobs = _Jobs()
    timeline(lib, [a, b], jobs, picks=lambda: sp.picks(lib, [a, b]))
    (kind, payload), = jobs.submitted
    assert kind == JOB and payload["from"] == a and payload["to"] == b
    pick = payload["pick"]
    assert style_lib.by_id(pick["style"]) is not None
    set_plan_job(payload, _Ctx(lib))
    plan = convert.load(lib.plan(a, b)["plan"], from_track=a, to_track=b)
    assert plan.technique == pick["technique"]


def test_a_pick_that_doesnt_fit_falls_back_to_the_pairs_own_plan(lib):
    from seedbox2.sets.timeline import plan_pair

    a, b = _analysed(lib, "a"), _analysed(lib, "b")
    own = plan_pair(lib, a, b)
    odd = plan_pair(lib, a, b, {"style": "no such style", "bars": 16, "a_out_s": 200.0, "b_in_s": 0.0})
    assert odd.technique == own.technique


def test_a_broken_set_plan_doesnt_stop_the_timeline(lib):
    from seedbox2.sets.timeline import timeline
    from tests.test_seedbox2_sets import _Jobs

    a, b = _analysed(lib, "a"), _analysed(lib, "b")
    jobs = _Jobs()

    def broken() -> dict:
        raise RuntimeError("no")

    timeline(lib, [a, b], jobs, picks=broken)
    assert jobs.submitted == [("set_plan", {"from": a, "to": b})]


def test_setplan_over_http(tmp_path):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app
    from tests.test_seedbox2_sets import lib_of

    app = create_app(library_path=tmp_path / "lib.sqlite3", start_jobs=False)
    with TestClient(app) as client:
        library = lib_of(client)
        hashes = [_analysed(library, c, bpm=122.0 + i) for i, c in enumerate("abc")]
        set_id = client.post("/api/sets", json={"name": "Friday", "hashes": hashes}).json()["id"]
        r = client.post(f"/api/sets/{set_id}/setplan", json={"target_minutes": 12})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["order"] == hashes and body["target_s"] == 720 and len(body["transitions"]) == 2
        r = client.post(f"/api/sets/{set_id}/setplan", json={"reorder": True, "lookahead": 3,
                                                             "pins": [{"position": 0, "hash": hashes[2]}]})  # fmt: skip
        assert r.status_code == 200 and r.json()["order"][0] == hashes[2]
        assert client.post(f"/api/sets/{set_id}/setplan", json={"hashes": hashes[:2]}).status_code == 409
        assert client.post(f"/api/sets/{set_id}/setplan", json={"lookahead": 9}).status_code == 422
        assert client.post("/api/sets/999/setplan", json={}).status_code == 404
