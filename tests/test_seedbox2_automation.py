"""Automation shaped by the audio (#56): cuts only where the decks collide, loudness held through the blend, filter
ranges from the tracks' centroids, effects from the tempo and the phrase, and everything left editable. Metrics on a
real render at the end: less masking and a smaller dip than Seedbox 1's fixed shapes."""

from __future__ import annotations

import numpy as np
import pytest
from scipy import signal
from scipy.io import wavfile

from musicreco.paths import ffmpeg_path
from musicreco import mixrender
from seedbox2.analysis import stems as stem_cache
from seedbox2.eval import mixes
from seedbox2.eval.mixmetrics import measure
from seedbox2.mix import automation, convert, offline
from seedbox2.mix import plan as v3

SR = 44100
BPM = 120.0
needs_ffmpeg = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")


def _kf(value: float, beat: float = 0.0, curve: str = "hold") -> dict:
    return {"beat": beat, "value": value, "curve": curve}


def _deck(track_id: str, start_s: float = 40.0, **lanes: list[dict]) -> dict:
    all_lanes = {name: [_kf(v3.LANE_DEFAULTS[name])] for name in v3.LANES} | lanes
    return {"track_id": track_id, "grid": [{"start_beat": 0.0, "start_s": start_s, "bpm": BPM}],
            "play_from_beat": -32.0, "play_to_beat": 64.0, "lanes": all_lanes}  # fmt: skip


def _plan(a: dict | None = None, b: dict | None = None, **over: object) -> v3.MixPlan:
    """A 16-bar blend: B's fader up over its first 4 bars, A's down over its last 4."""
    a = a or _deck("a", volume=[_kf(1.0, 48.0, "linear"), _kf(0.0, 64.0)])
    b = b or _deck("b", volume=[_kf(0.0, 0.0, "linear"), _kf(1.0, 16.0)])
    return v3.validate({"technique": "long blend", "bars": 16, "window": {"start_beat": -32.0, "end_beat": 96.0},
                        "tempo": [_kf(BPM)], "a": {**a, "play_to_beat": 64.0},
                        "b": {**b, "play_to_beat": 96.0}, **over})  # fmt: skip


def _noise(lo: float, hi: float, level_db: float = -20.0, seconds: float = 120.0, seed: int = 0) -> np.ndarray:
    """Band-limited noise at this RMS level, stereo."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    sos = signal.butter(4, [lo, hi], "bandpass", fs=SR, output="sos")
    x = signal.sosfilt(sos, rng.standard_normal(n))
    x *= 10 ** (level_db / 20) / np.sqrt(np.mean(x**2))
    return np.stack([x, x])


def _audio(x: np.ndarray, **over: object) -> automation.DeckAudio:
    return automation.DeckAudio(x, SR, **over)


def _value(deck: v3.Deck, lane: str, beat: float) -> float:
    return float(v3.lane_value(getattr(deck.lanes, lane), beat))


MIDS, HIGHS = (400.0, 1500.0), (4000.0, 9000.0)


# --- masking ---------------------------------------------------------------------------------------------------------


def test_decks_in_different_bands_get_no_cuts_at_all():
    plan = _plan(b=_deck("b", volume=[_kf(0.0, 0.0, "linear"), _kf(1.0, 16.0)], eq_mid=[_kf(-15.0)], eq_high=[_kf(-12.0)]))
    out = automation.adapt(plan, _audio(_noise(*MIDS)), _audio(_noise(*HIGHS, seed=1)))
    assert out.report["cuts_db"] == {"a.eq_mid": 0.0, "a.eq_high": 0.0, "b.eq_mid": 0.0, "b.eq_high": 0.0}
    for beat in (8.0, 24.0, 40.0):  # Seedbox 1 would hold B's mids at -15 here whatever the tracks do
        assert _value(out.plan.b, "eq_mid", beat) == pytest.approx(0.0, abs=0.3)


def test_colliding_decks_are_cut_by_as_much_as_needed_and_the_index_falls():
    gentle = automation.adapt(_plan(), _audio(_noise(*MIDS)), _audio(_noise(*MIDS, level_db=-28.0, seed=1)))
    hard = automation.adapt(_plan(), _audio(_noise(*MIDS)), _audio(_noise(*MIDS, seed=1)))
    # B is already 8 dB under A in the bars A leads, so it hardly needs anything
    assert gentle.report["cuts_db"]["b.eq_mid"] < hard.report["cuts_db"]["b.eq_mid"]
    assert hard.report["cuts_db"]["b.eq_mid"] >= 5.0
    assert hard.report["masking_before"]["mid"] > 0.6
    assert hard.report["masking"]["mid"] <= automation.TARGET_MASKING + 0.08


def test_the_cut_follows_the_lead():
    out = automation.adapt(_plan(), _audio(_noise(*MIDS)), _audio(_noise(*MIDS, seed=1))).plan
    assert _value(out.b, "eq_mid", 20.0) < -3.0  # A leads: B is under it
    assert _value(out.a, "eq_mid", 20.0) >= -1.0
    assert _value(out.a, "eq_mid", 44.0) < -3.0  # B leads: A is under it
    assert _value(out.b, "eq_mid", 44.0) >= -1.0
    assert _value(out.b, "eq_mid", 70.0) == pytest.approx(0.0, abs=0.01)  # and after the blend the deck is as it was


def test_a_planned_duck_under_a_vocal_clash_stays():
    duck = [_kf(0.0, 16.0, "linear"), _kf(-18.0, 24.0, "hold"), _kf(-18.0, 40.0, "linear"), _kf(0.0, 48.0)]
    plan = _plan(b=_deck("b", volume=[_kf(0.0, 0.0, "linear"), _kf(1.0, 16.0)], eq_mid=duck))
    out = automation.adapt(plan, _audio(_noise(*MIDS)), _audio(_noise(*MIDS, seed=1))).plan
    assert _value(out.b, "eq_mid", 30.0) <= -16.0


def test_a_cut_has_no_blend_to_touch():
    plan = _plan(a=_deck("a", volume=[_kf(1.0, 0.0), _kf(0.0, 0.0)]), b=_deck("b", volume=[_kf(0.0, 0.0), _kf(1.0, 0.0)]))
    out = automation.adapt(plan, _audio(_noise(*MIDS)), _audio(_noise(*MIDS, seed=1)))
    assert "cuts_db" not in out.report and out.plan.b.lanes.eq_mid == plan.b.lanes.eq_mid


# --- stems -----------------------------------------------------------------------------------------------------------


def _stems(main: str, x: np.ndarray) -> dict[str, np.ndarray]:
    quiet = np.zeros_like(x)
    return {name: x if name == main else quiet for name in ("drums", "bass", "vocals", "other")}


def test_a_band_that_is_one_stem_has_that_stem_cut():
    a = _noise(*MIDS)
    b = _noise(*MIDS, seed=1)
    out = automation.adapt(_plan(), _audio(a, stems=_stems("other", a)), _audio(b, stems=_stems("vocals", b)))
    lanes = out.plan.b.stem_lanes
    assert out.report["stem_cuts_db"]["b.vocals"] > 3.0
    assert float(v3.lane_value(lanes.gain_vocals, 20.0)) < 0.7
    assert float(v3.lane_value(lanes.gain_drums, 20.0)) == 1.0
    assert out.report["stem_masking"]["other/vocals"]["mid"] < out.report["masking_before"]["mid"]
    # the stem took the cut, so the EQ has less left to do
    assert out.report["cuts_db"]["b.eq_mid"] < out.report["stem_cuts_db"]["b.vocals"]


def test_stem_masking_is_reported_per_pair_and_band():
    a, b = _noise(*MIDS), _noise(*HIGHS, seed=1)
    stems_a = {"drums": b * 0, "bass": b * 0, "vocals": a, "other": b * 0}
    stems_b = {"drums": b, "bass": b * 0, "vocals": b * 0, "other": b * 0}
    report = automation.adapt(_plan(), _audio(a, stems=stems_a), _audio(b, stems=stems_b)).report
    assert set(report["stem_masking"]) == {f"{x}/{y}" for x in stems_a for y in stems_b}
    assert report["stem_masking"]["vocals/drums"]["mid"] == pytest.approx(0.0, abs=0.05)


def test_a_deck_without_stems_gets_no_stem_lanes():
    out = automation.adapt(_plan(), _audio(_noise(*MIDS)), _audio(_noise(*MIDS, seed=1))).plan
    assert out.a.stem_lanes is None and out.b.stem_lanes is None


# --- loudness --------------------------------------------------------------------------------------------------------


def test_gain_riding_fills_a_dip_and_leaves_the_tracks_alone():
    # both faders down in the middle: Seedbox 1 would leave that hole
    a = _deck("a", volume=[_kf(1.0, 8.0, "linear"), _kf(0.4, 16.0, "hold"), _kf(0.4, 40.0, "linear"), _kf(0.0, 64.0)])
    b = _deck("b", volume=[_kf(0.0, 0.0, "linear"), _kf(0.4, 16.0, "hold"), _kf(0.4, 40.0, "linear"), _kf(1.0, 64.0)])
    x, y = _noise(*MIDS), _noise(*HIGHS, seed=1)
    out = automation.adapt(_plan(a, b), _audio(x), _audio(y))
    assert out.report["dip_lu_before"] < -3.0
    assert out.report["dip_lu"] >= -1.0
    assert out.report["ride_db"][1] > 2.0
    assert _value(out.plan.a, "eq_mid", 30.0) > 2.0
    assert _value(out.plan.a, "eq_mid", -16.0) == pytest.approx(0.0)  # before the blend: as planned
    assert _value(out.plan.b, "eq_high", 90.0) == pytest.approx(0.0)  # after it: as planned


def test_gain_riding_takes_a_bump_down_on_the_faders():
    x, y = _noise(*MIDS), _noise(*HIGHS, seed=1)
    a = _deck("a", volume=[_kf(1.0, 56.0, "linear"), _kf(0.0, 64.0)])
    b = _deck("b", volume=[_kf(0.0, 0.0, "linear"), _kf(1.0, 4.0)])
    out = automation.adapt(_plan(a, b), _audio(x), _audio(y))
    assert out.report["bump_lu_before"] > 1.5
    assert out.report["bump_lu"] <= 1.0
    assert _value(out.plan.a, "volume", 30.0) < 0.95


def test_a_kill_stays_a_kill_under_a_boost():
    a = _deck("a", volume=[_kf(1.0, 8.0, "linear"), _kf(0.4, 16.0, "hold"), _kf(0.4, 40.0, "linear"), _kf(0.0, 64.0)])
    b = _deck("b", volume=[_kf(0.0, 0.0, "linear"), _kf(0.4, 16.0, "hold"), _kf(0.4, 40.0, "linear"), _kf(1.0, 64.0)],
              eq_low=[_kf(-30.0, 0.0), _kf(-30.0, 60.0, "linear"), _kf(0.0, 61.0)])  # fmt: skip
    out = automation.adapt(_plan(a, b), _audio(_noise(*MIDS)), _audio(_noise(*HIGHS, seed=1))).plan
    assert _value(out.b, "eq_low", 30.0) == -30.0


def test_a_hole_in_the_lows_is_closed_at_the_hand_off():
    low = (50.0, 150.0)
    a = _deck("a", eq_low=[_kf(0.0, 0.0), _kf(0.0, 40.0, "linear"), _kf(-30.0, 41.0)],
              volume=[_kf(1.0, 56.0, "linear"), _kf(0.0, 64.0)])  # fmt: skip
    b = _deck("b", eq_low=[_kf(-30.0, 0.0), _kf(-30.0, 48.0, "linear"), _kf(0.0, 49.0)],
              volume=[_kf(0.0, 0.0, "linear"), _kf(1.0, 16.0)])  # fmt: skip
    x, y = _noise(*low), _noise(*low, seed=1)
    out = automation.adapt(_plan(a, b), _audio(x), _audio(y))
    assert out.report["lows_moved_to_beat"] == pytest.approx(41.0, abs=1.0)
    assert _value(out.plan.b, "eq_low", 44.0) > -3.0


def test_a_kill_opens_where_the_other_deck_has_no_lows_to_clash_with():
    low = (50.0, 150.0)
    b = _deck("b", eq_low=[_kf(-30.0, 0.0), _kf(-30.0, 48.0, "linear"), _kf(0.0, 49.0)],
              volume=[_kf(0.0, 0.0, "linear"), _kf(1.0, 16.0)])  # fmt: skip
    a = _deck("a", volume=[_kf(1.0, 56.0, "linear"), _kf(0.0, 64.0)])
    free = automation.adapt(_plan(a, b), _audio(_noise(*MIDS)), _audio(_noise(*low, seed=1)))
    assert free.report["lows_opened_bars"]["b"] >= automation.MIN_FREE_BARS
    assert _value(free.plan.b, "eq_low", 24.0) > -3.0
    clash = automation.adapt(_plan(a, b), _audio(_noise(*low)), _audio(_noise(*low, seed=1)))
    assert "lows_opened_bars" not in clash.report
    assert _value(clash.plan.b, "eq_low", 24.0) == -30.0


def test_measured_loudness_rides_the_blend_and_leaves_pins_and_the_rest_alone():
    plan = _plan(_deck("a", volume=[_kf(1.0, 48.0, "linear"), _kf(0.0, 64.0)]) | {"pinned": ["eq_high"]})
    beats = np.arange(16.0, 32.0, 0.25)
    out = automation.ride_measured(plan, beats, np.full(beats.size, -3.0))  # 3 LU under the line in bars 5-8
    assert _value(out.a, "eq_mid", 24.0) == pytest.approx(2.1, abs=0.2)
    assert _value(out.a, "eq_high", 24.0) == 0.0
    assert _value(out.a, "eq_mid", 8.0) == 0.0 and _value(out.a, "eq_mid", 40.0) == 0.0
    down = automation.ride_measured(plan, beats, np.full(beats.size, 3.0))
    assert _value(down.b, "volume", 24.0) == pytest.approx(10 ** (-2.1 / 20), abs=0.02)
    assert automation.ride_measured(plan, beats, np.full(beats.size, 0.2)) is None


# --- filter ----------------------------------------------------------------------------------------------------------


def test_the_prediction_uses_the_renderers_numbers():
    assert automation.FILTER_LP_HZ == (mixrender.FILTER_LP_MIN_HZ, mixrender.FILTER_LP_MAX_HZ)
    assert automation.FILTER_HP_HZ == (mixrender.FILTER_HP_MIN_HZ, mixrender.FILTER_HP_MAX_HZ)
    assert (automation.XOVER_LOW_HZ, automation.XOVER_HIGH_HZ) == (mixrender.XOVER_LOW_HZ, mixrender.XOVER_HIGH_HZ)
    assert (automation.DECK_LUFS, automation.LEVEL_THRESHOLD_DB, automation.LEVEL_RATIO, automation.MAKEUP_DB) == (
        mixrender.DECK_LUFS, mixrender.LEVEL_THRESHOLD_DB, mixrender.LEVEL_RATIO, mixrender.MAKEUP_DB,
    )  # fmt: skip


def test_filter_values_and_cutoffs_are_inverse():
    for v in (-1.0, -0.6, -0.1, 0.1, 0.35, 0.85, 1.0):
        assert automation.filter_value(automation.filter_hz(v), v) == pytest.approx(v, abs=1e-9)
    assert automation.filter_hz(1.0) == pytest.approx(10000.0)
    assert automation.filter_hz(-1.0) == pytest.approx(80.0)


def test_filter_range_follows_the_tracks_centroid():
    sweep = [_kf(0.0, 32.0, "ease"), _kf(0.35, 64.0)]
    plan = _plan(a=_deck("a", filter=sweep, volume=[_kf(1.0, 48.0, "linear"), _kf(0.0, 64.0)]))
    other = _audio(_noise(*MIDS, seed=1))
    dark = automation.adapt(plan, _audio(_noise(100.0, 400.0)), other)
    bright = automation.adapt(plan, _audio(_noise(3000.0, 8000.0)), other)
    assert dark.report["a_centroid_hz"] < 300 < 3000 < bright.report["a_centroid_hz"]
    dark_peak = _value(dark.plan.a, "filter", 64.0)
    bright_peak = _value(bright.plan.a, "filter", 64.0)
    assert dark_peak < 0.35 < bright_peak
    assert automation.filter_hz(bright_peak) / automation.filter_hz(dark_peak) > 2.0
    assert _value(dark.plan.a, "filter", 32.0) == 0.0  # dry is still dry


def test_a_low_pass_stays_a_low_pass():
    plan = _plan(b=_deck("b", filter=[_kf(-0.6, 0.0, "ease"), _kf(0.0, 32.0)],
                         volume=[_kf(0.0, 0.0, "linear"), _kf(1.0, 16.0)]))  # fmt: skip
    out = automation.adapt(plan, _audio(_noise(*MIDS)), _audio(_noise(3000.0, 8000.0, seed=1))).plan
    assert -1.0 <= _value(out.b, "filter", 0.0) < -0.3


def test_a_plan_without_a_filter_keeps_none():
    out = automation.adapt(_plan(), _audio(_noise(*MIDS)), _audio(_noise(*MIDS, seed=1)))
    assert "a_centroid_hz" not in out.report and all(k.value == 0.0 for k in out.plan.a.lanes.filter)


# --- sends -----------------------------------------------------------------------------------------------------------


def _clicks(per_beat: float, bpm: float = BPM, seconds: float = 120.0) -> np.ndarray:
    n = int(seconds * SR)
    x = np.zeros(n)
    hit = np.exp(-np.arange(int(0.03 * SR)) / (0.005 * SR)) * np.random.default_rng(3).standard_normal(int(0.03 * SR))
    for i in np.arange(0, n - hit.size, 60.0 / bpm / per_beat * SR).astype(int):
        x[i: i + hit.size] += hit * 0.5
    return np.stack([x, x])


def _with_sends() -> dict:
    return _deck("a", echo_send=[_kf(0.0, 60.0, "linear"), _kf(0.6, 64.0)], reverb_send=[_kf(0.0, 56.0, "linear"), _kf(0.6, 64.0)],
                 volume=[_kf(1.0, 48.0, "linear"), _kf(0.0, 64.0)])  # fmt: skip


def test_sends_follow_how_busy_the_outgoing_phrase_is():
    other = _audio(_noise(*MIDS, seed=1))
    sparse = automation.adapt(_plan(_with_sends()), _audio(_clicks(1)), other)
    busy = automation.adapt(_plan(_with_sends()), _audio(_clicks(4)), other)
    assert sparse.report["out_density"] < busy.report["out_density"]
    assert _value(sparse.plan.a, "echo_send", 64.0) > 0.6 > _value(busy.plan.a, "echo_send", 64.0)
    assert sparse.plan.a.sends.reverb_size > busy.plan.a.sends.reverb_size
    assert _value(sparse.plan.a, "echo_feedback", 0.0) > _value(busy.plan.a, "echo_feedback", 0.0)
    assert sparse.plan.a.sends.echo_beats >= busy.plan.a.sends.echo_beats


def test_echo_time_follows_the_tempo():
    other = _audio(_noise(*MIDS, seed=1))
    for bpm in (80.0, 120.0, 174.0):
        plan = _plan(_with_sends(), tempo=[_kf(bpm)])
        out = automation.adapt(plan, _audio(_clicks(2, bpm)), other).plan
        ms = out.a.sends.echo_beats * 60000.0 / bpm
        assert 170.0 <= ms <= 620.0, bpm
        assert out.a.sends.echo_beats in automation.ECHO_DIVISIONS
    slow = automation.adapt(_plan(_with_sends(), tempo=[_kf(80.0)]), _audio(_clicks(2, 80.0)), other).plan
    fast = automation.adapt(_plan(_with_sends(), tempo=[_kf(174.0)]), _audio(_clicks(2, 174.0)), other).plan
    assert slow.a.sends.echo_beats < fast.a.sends.echo_beats


# --- editable --------------------------------------------------------------------------------------------------------


def test_the_result_is_a_valid_plan_of_plain_keyframes_small_enough_to_edit():
    out = automation.adapt(_plan(), _audio(_noise(*MIDS)), _audio(_noise(*MIDS, seed=1))).plan
    assert v3.validate(out.model_dump()) == out
    assert len(out.b.lanes.eq_mid) <= 32
    assert any("collide" in n for n in out.notes)


def test_an_edited_plan_comes_back_untouched():
    plan = _plan(edited=True)
    out = automation.adapt(plan, _audio(_noise(*MIDS)), _audio(_noise(*MIDS, seed=1)))
    assert out.plan is plan and out.report == {"skipped": "edited"}


def test_pinned_lanes_and_send_settings_are_left_alone_and_the_rest_fits_around_them():
    mine = [_kf(-3.0, 0.0), _kf(-3.0, 64.0)]
    a = _with_sends() | {"pinned": ["eq_mid", "echo_beats"], "sends": {"echo_beats": 1.5}}
    a["lanes"] = a["lanes"] | {"eq_mid": mine}
    plan = _plan(a)
    out = automation.adapt(plan, _audio(_noise(*MIDS)), _audio(_noise(*MIDS, seed=1))).plan
    assert out.a.lanes.eq_mid == plan.a.lanes.eq_mid
    assert out.a.sends.echo_beats == 1.5
    assert out.a.pinned == ["eq_mid", "echo_beats"]
    assert out.b.lanes.eq_mid != plan.b.lanes.eq_mid


def test_only_lanes_and_send_settings_can_be_pinned():
    with pytest.raises(ValueError, match="pinned"):
        _plan(_deck("a") | {"pinned": ["bpm"]})


def test_with_pins_carries_what_a_person_set_onto_a_new_plan():
    mine = [_kf(-7.0, 0.0)]
    old = _plan(_deck("a", eq_mid=mine) | {"pinned": ["eq_mid", "gain_vocals", "reverb_size"],
                                           "sends": {"reverb_size": 0.2},
                                           "stem_lanes": {n: [_kf(0.5 if n == "gain_vocals" else 1.0)]
                                                          for n in v3.STEM_LANES}})  # fmt: skip
    new = automation.with_pins(_plan(), old)
    assert new.a.lanes.eq_mid == old.a.lanes.eq_mid and new.a.sends.reverb_size == 0.2
    assert new.a.stem_lanes.gain_vocals == old.a.stem_lanes.gain_vocals
    assert new.a.pinned == old.a.pinned and new.b.pinned == []
    assert new.a.lanes.volume == _plan().a.lanes.volume


def test_adapting_is_quick():
    import time

    a, b = _audio(_noise(*MIDS, seconds=300.0)), _audio(_noise(*MIDS, seconds=300.0, seed=1))
    t0 = time.perf_counter()
    automation.adapt(_plan(), a, b)
    assert time.perf_counter() - t0 < 1.5


# --- through the renderer --------------------------------------------------------------------------------------------


def test_cached_stems_are_found_by_name_only_when_all_four_are_there(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path))
    folder = stem_cache.folder_for("abcdef")
    folder.mkdir(parents=True)
    for name in ("drums", "bass", "vocals"):
        (folder / f"{name}.flac").write_bytes(b"x")
    assert offline.cached_stems("abcdef") == {} and offline.cached_stems("") == {}
    (folder / "other.flac").write_bytes(b"x")
    found = offline.cached_stems("abcdef")
    assert found["bass"] == folder / "bass.flac" and set(found) == {"drums", "bass", "vocals", "other"}


@needs_ffmpeg
def test_stem_lanes_are_rendered(tmp_path):
    x, y = _noise(*MIDS, seconds=70.0), _noise(*HIGHS, seconds=70.0, seed=1)
    paths = {}
    for name, sig in (("a", x), ("b", y), ("a_vocals", x), ("silent", x * 0)):
        paths[name] = tmp_path / f"{name}.wav"
        wavfile.write(paths[name], SR, sig.T.astype(np.float32))
    stems = {"vocals": paths["a_vocals"], "drums": paths["silent"], "bass": paths["silent"], "other": paths["silent"]}
    plan = _plan(_deck("a") | {"stem_lanes": {n: [_kf(1.0)] for n in v3.STEM_LANES} | {"gain_vocals": [_kf(1.0, 0.0), _kf(0.0, 8.0)]}},
                 _deck("b", volume=[_kf(0.0)]))  # fmt: skip
    audio_a, audio_b = offline.deck_audio(plan, paths["a"], paths["b"], stems, None)
    r = offline.render(plan, paths["a"], paths["b"], audio_a, audio_b)
    level = lambda t: 10 * np.log10(np.mean(r["stems"]["A"][:, int(t * SR): int((t + 1) * SR)] ** 2) + 1e-12)  # noqa: E731
    beat_s = 60.0 / BPM
    assert level(16.0 * beat_s) > -40.0  # beat -16 sits 16 beats into the window
    assert level(44.0 * beat_s) < -70.0  # the vocals stem is off, from beat 8 on


@needs_ffmpeg
def test_the_engine_mixes_with_less_masking_and_a_smaller_dip_than_seedbox_one(tmp_path, monkeypatch):
    monkeypatch.setattr(mixes, "SYNTHETIC", mixes.SYNTHETIC[:1])
    (pair,) = mixes.synthetic_pairs(tmp_path)
    old = measure(mixes.render_seedbox1(pair))
    r = mixes.render_seedbox2(pair)
    new = measure(r)
    assert new["loudness"]["dip_lu"] > old["loudness"]["dip_lu"] + 1.0
    assert new["loudness"]["dip_lu"] >= -1.0 and new["loudness"]["bump_lu"] <= 1.0
    for band in ("low_mid", "high_mid", "high"):
        assert new["masking"][band] < old["masking"][band] - 0.1
    assert new["beat"]["median_ms"] < 5 and new["loudness"]["clipped_samples"] == 0
    assert r.plan["adaptive"]["cuts_db"]
    convert.from_seedbox1(r.plan)  # what comes out still reads as a plan
