"""Learnt cue points and transition lengths (#55): musicreco.cuescore and `python -m seedbox2.eval cues`."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from musicreco import cuescore, transitions
from musicreco.cuescore import CueModel
from seedbox2.eval import cues

from .fixtures import HOUSE, HOUSE_B, PAIRS, VOCAL_INTRO, VOCAL_OUTRO, analysis


@pytest.fixture(scope="module")
def synth_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cues") / "synth.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in cues.synth(40)))
    return path


@pytest.fixture(scope="module")
def trained(synth_file: Path) -> tuple[list[cues.Transition], CueModel]:
    ts = cues.read(synth_file)
    return ts, cues.train(ts, log=lambda _: None)


def _grids(a, b):
    return transitions.make_grid(analysis(a)), transitions.make_grid(analysis(b))


def test_without_a_model_the_penalties_are_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(cuescore.MODEL_ENV, raising=False)
    monkeypatch.setenv(cuescore.SUPPORT_DIR_ENV, str(tmp_path))  # no cue_model.json there
    assert cuescore.active() is None
    for _, a, b in PAIRS:
        ga, gb = _grids(a, b)
        if ga is None or gb is None or abs(ga.bar_s / gb.bar_s - 1) > 0.2:
            continue
        with cuescore.using(None):
            ref = [(p.a0, p.b0, p.length, p.fit) for p in transitions.blend_windows(ga, gb, 16, False)]
        assert [(p.a0, p.b0, p.length, p.fit) for p in transitions.blend_windows(ga, gb, 16, False)] == ref


def test_the_installed_model_is_found_and_a_broken_one_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(cuescore.MODEL_ENV, raising=False)
    monkeypatch.setenv(cuescore.SUPPORT_DIR_ENV, str(tmp_path))
    CueModel(weights={"len32": 2.0}).save(tmp_path / cuescore.MODEL_FILE)
    assert cuescore.active() is not None and cuescore.active().weights["len32"] == 2.0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(cuescore.MODEL_ENV, str(bad))
    assert cuescore.active() is None


def test_learnt_score_drives_the_choice() -> None:
    ga, gb = _grids(HOUSE, HOUSE_B)
    with cuescore.using(CueModel(weights={"len32": 5.0, "len8": -5.0})):
        assert transitions._best_blend(ga, gb, False).length == 32
    with cuescore.using(CueModel(weights={"len8": 5.0, "len32": -5.0})):
        assert transitions._best_blend(ga, gb, False).length == 8


def test_hard_limits_hold_whatever_the_model_likes() -> None:
    """A model that loves vocal over vocal still gets no blend with more clash than _plan allows, on phrases only."""
    loves_clash = CueModel(weights={"vocal_soft": 50.0, "vocal_a": 5.0, "vocal_b": 5.0})
    ga, gb = _grids(VOCAL_OUTRO, VOCAL_INTRO)
    with cuescore.using(loves_clash):
        plan = transitions._plan(ga, gb, False)
        best = transitions._best_blend(ga, gb, False)
    assert best is not None and len(best.clash) > 0  # the model wants the clash
    assert len(plan.clash) <= transitions._max_clash(plan.length)  # the planner doesn't take it
    rng = np.random.default_rng(1)
    for i in range(30):
        a, b = cues.synth_track(rng, f"a{i}"), cues.synth_track(rng, f"b{i}")
        ga, gb = transitions.make_grid(a), transitions.make_grid(b)
        with cuescore.using(loves_clash):
            p = transitions._plan(ga, gb, False)
        assert len(p.clash) <= transitions._max_clash(p.length)
        if p.length:
            assert p.a0 in ga.phrases and (p.b0 in gb.phrases or p.b0 == gb.intro[0])


def test_length_prior_is_a_distribution_with_fallbacks(trained) -> None:
    _, model = trained
    for dist in model.length_prior.values():
        assert sum(dist.values()) == pytest.approx(1.0)
        assert set(dist) == set(cuescore.LENGTHS)
    for e in (0.3, 0.6, 0.9):
        b = cuescore.energy_bucket(e)
        coarse = model.length_prior.get(f"*/{b}", model.length_prior["*/*"])
        assert model.prior(32, e, "unknown genre") == coarse[32]
        assert model.prior(32, e, "house") == model.length_prior.get(f"house/{b}", coarse)[32]
    # the synthetic DJ prefers 32 bars, and the prior learns it
    assert model.length_prior["*/*"][32] > cuescore.DEFAULT_LENGTH_PRIOR[32]


def test_learns_the_synthetic_dj(trained) -> None:
    """On held-out synthetic mixes the learnt model agrees with the DJ about as often as the DJ's own (noiseless)
    taste does, and more often than the hand-set penalties, which have a different taste."""
    ts, model = trained
    test = [t for t in ts if cues.held_out(t.mix, 0.2)]
    assert len(test) > 30
    learnt = cues.agreement(test, model)["both_within_8"]
    hand = cues.agreement(test, None)["both_within_8"]
    oracle = cues.agreement(test, CueModel(weights=cues.SYNTH_DJ))["both_within_8"]
    assert learnt > hand + 0.05
    assert learnt >= oracle - 0.1
    for k, w in cues.SYNTH_DJ.items():  # the strong preferences come back with their signs
        if abs(w) >= 1.2:
            assert np.sign(model.weights[k]) == np.sign(w), k


def test_model_is_small_and_round_trips(trained, tmp_path: Path) -> None:
    _, model = trained
    path = tmp_path / "m.json"
    model.save(path)
    assert path.stat().st_size < 1_000_000  # the ticket's limit; it is a few KB
    back = CueModel.load(path)
    x = dict.fromkeys(cuescore.FEATURES, 0.5)
    assert back.fit(x) == pytest.approx(model.fit(x), abs=1e-2)
    assert back.length_prior.keys() == model.length_prior.keys()


def test_dj_pick_reads_above_the_planner_threshold(trained) -> None:
    """Platt scaling: the DJ's own candidate scores >= 50 (the planner's minimum blend fit), and above the rest."""
    ts, model = trained
    above, others = [], []
    for t in ts[:60]:
        plans = cues.candidates(t)
        y = cues.nearest(t, plans)
        if y is None:
            continue
        fits = [model.fit(cuescore.features(t.ga, t.gb, p.a0, p.b0, p.length, False, p.a_loop, p.b_loop, model))
                for p in plans]  # fmt: skip
        above.append(fits[y])
        others.extend(f for i, f in enumerate(fits) if i != y)
    assert np.mean([f >= 50 for f in above]) > 0.9 and np.mean(above) > np.mean(others) + 10


def test_cli_train_and_eval(synth_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, rep = tmp_path / "model.json", tmp_path / "report.json"
    assert cues.main(["train", str(synth_file), "--out", str(out), "--json", str(rep)]) == 0
    assert "learnt" in capsys.readouterr().out
    r = json.loads(rep.read_text())
    assert r["learnt"]["transitions"] > 0 and r["target"] == 0.74
    assert cues.main(["eval", str(synth_file), "--model", str(out)]) == 0


def test_read_skips_tracks_without_analysis(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    rec = {"mix": "m", "a": {"analysis": analysis(HOUSE)}, "b": {"analysis": {}},
           "a_in_s": 0, "b_cue_s": 0, "length_s": 1}
    p.write_text(json.dumps(rec) + "\n")
    logs: list[str] = []
    assert cues.read(p, log=logs.append) == [] and "1 no analysis" in logs[0]


@pytest.fixture(scope="module")
def trained_net(trained) -> tuple[list[cues.Transition], CueModel]:
    ts, _ = trained
    return ts, cues.train(ts, log=lambda _: None, kind="net")


def test_network_is_onnx_small_and_learns(trained_net, tmp_path: Path) -> None:
    """The network model: one JSON file carrying an ONNX graph (well under the 1 MB limit) that onnxruntime runs, and
    it learns the synthetic DJ's taste better than the hand-set penalties."""
    ts, model = trained_net
    assert model.net is not None and model.info["kind"] == "net"
    path = tmp_path / "net.json"
    model.save(path)
    assert path.stat().st_size < 1_000_000
    back = CueModel.load(path)
    t = ts[0]
    xs = [cuescore.features(t.ga, t.gb, p.a0, p.b0, p.length, False, p.a_loop, p.b_loop, back)
          for p in cues.candidates(t)]  # fmt: skip
    assert back.utilities(xs) == pytest.approx(model.utilities(xs), abs=1e-5)
    assert [back.utility(x) for x in xs[:3]] == pytest.approx(back.utilities(xs)[:3], abs=1e-5)
    test = [t for t in ts if cues.held_out(t.mix, 0.2)]
    assert cues.agreement(test, model)["both_within_8"] > cues.agreement(test, None)["both_within_8"]


def test_onnx_graph_matches_the_numpy_network(trained) -> None:
    import onnxruntime as ort

    ts, model = trained
    rows = cues.design(ts[:40], model, cuescore.NET_INPUTS)
    net, _ = cues.fit_net(rows, steps=50)
    sess = ort.InferenceSession(net.onnx(), providers=["CPUExecutionProvider"])
    x = np.concatenate([r[0] for r in rows[:5]]).astype(np.float32)
    (u,) = sess.run(None, {"x": x})
    assert u.reshape(-1) == pytest.approx(net(x.astype(np.float64)), abs=1e-4)


def test_a_network_for_other_inputs_is_refused(trained_net, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, model = trained_net
    d = model.to_json()
    d["net_inputs"] = d["net_inputs"][:-1]
    path = tmp_path / "old.json"
    path.write_text(json.dumps(d))
    with pytest.raises(ValueError):
        CueModel.load(path)
    monkeypatch.setenv(cuescore.MODEL_ENV, str(path))
    assert cuescore.active() is None  # the planner keeps its penalties
