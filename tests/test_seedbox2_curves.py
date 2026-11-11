"""Fader and EQ curves recovered from DJ mixes (#59): `python -m seedbox2.eval curves`, on synthetic audio."""

from __future__ import annotations

import numpy as np
import pytest

from musicreco import mixplan
from seedbox2.eval import curves


@pytest.mark.parametrize("technique,bars", [("bass swap", 8), ("filter sweep", 16), ("echo out", 8)])
def test_solve_recovers_a_templates_curves_from_its_render(technique: str, bars: int) -> None:
    r = curves.synth_case(np.random.default_rng(3), technique, bars)
    assert r["fit_db"] < 5.0
    assert r["band_error_db"]["band_a"] < 3.0
    assert r["band_error_db"]["band_b"] < 3.0


def test_solve_keeps_the_mixer_constraints() -> None:
    rng = np.random.default_rng(0)
    n = 60
    a, b = rng.uniform(0.5, 2.0, (len(curves.SUBS), n)), rng.uniform(0.5, 2.0, (len(curves.SUBS), n))
    t = np.linspace(0, 1, n)
    mix = a * (1 - t) ** 2 + b * t**2
    c = curves.solve(mix, a, b)
    assert np.all(np.diff(c.fader_a) <= 1e-6) and np.all(np.diff(c.fader_b) >= -1e-6)
    assert np.all(c.band_a <= 1 + 1e-6) and np.all(c.band_b <= 1 + 1e-6)
    np.testing.assert_allclose(c.fader_a, 1 - t, atol=0.05)
    np.testing.assert_allclose(c.fader_b, t, atol=0.05)
    assert c.error_db < 0.5


def test_place_finds_a_tracks_rate_and_offset() -> None:
    rng = np.random.default_rng(1)
    m = int(90.0 * curves.SR)
    # a loop alone repeats every beat: random levels every 2 s make the track's position findable
    env = np.repeat(rng.uniform(0.1, 1.0, 46), 2 * curves.SR)[:m]
    noise = np.convolve(rng.standard_normal(m), np.ones(8) / 8, mode="same")
    track = curves._noise_track(rng, 90.0, 124.0)[:m] * env + 0.2 * noise * env[::-1]
    rate, at = 1.02, 40.0  # the DJ plays it 2 % fast from 40 s into the mix
    n = int(200.0 * curves.SR)
    t = np.arange(n) / curves.SR
    u = (t - at) * rate
    mix = np.where((u >= 0) & (u < 88.0), np.interp(u, np.arange(len(track)) / curves.SR, track), 0.0)
    mix = (mix + 0.3 * curves._noise_track(rng, 200.0, 128.0)[:n] * (t > 120)).astype(np.float32)
    p = curves.place(curves.align_features(mix), curves.align_features(track.astype(np.float32)))
    assert p is not None
    assert abs(p.rate - rate) <= 0.0025
    assert abs(p.mix_time(0.0) - at) < 0.5
    assert p.start < at + 10 and p.end > at + 60


def test_template_shapes_read_as_the_templates_do() -> None:
    swap = curves.events(curves.template_shape("bass swap", 16))
    assert 0.3 < swap["swap"] < 0.7  # the lows change hands in the middle
    assert swap["b_high_in"] < swap["swap"] < swap["a_high_out"]
    echo = curves.events(curves.template_shape("echo out", 16))
    assert echo["a_mid_out"] > 0.9  # A stays up to its cut


def test_shape_lanes_play_the_shape_back() -> None:
    sh = curves.template_shape("long blend", 16)
    lanes = curves.shape_lanes(sh, 64.0)
    beats = np.linspace(0.0, 64.0, curves.SHAPE_POINTS)
    for deck, rows in (("a", sh[:3]), ("b", sh[3:])):
        L = lanes[deck]
        vol = mixplan.lane_value(L["volume"], beats)
        for k, name in enumerate(("eq_low", "eq_mid", "eq_high")):
            amp = vol * 10 ** (np.asarray(mixplan.lane_value(L[name], beats)) / 20)
            np.testing.assert_allclose(curves._level(amp), rows[k], atol=0.02)


def test_the_logit_learns_a_separable_choice() -> None:
    rng = np.random.default_rng(2)
    X = rng.standard_normal((120, 3))
    y = (X[:, 0] > 0).astype(int) + 2 * (X[:, 1] > 1.0)
    W = curves.fit_softmax(X, np.minimum(y, 2), 3, l2=0.01, steps=3000, lr=0.5)
    assert np.mean(curves.predict(W, X) == np.minimum(y, 2)) > 0.85


def test_style_of_reads_mixesdb_tags() -> None:
    def tags(*keys: str) -> dict:
        return {"tags": [{"key": f"Category:{k}"} for k in keys]}

    assert curves.style_of(tags("Someone", "Tech House")) == "tech house"
    assert curves.style_of(tags("Drum & Bass", "House")) == "dnb"
    assert curves.style_of(tags("Someone")) == "other"
    assert curves._timestamp_s("[05] Code 61 - Drop The Deal") == 300.0
