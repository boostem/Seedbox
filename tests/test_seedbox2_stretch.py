"""Seedbox 2's time stretch and pitch shift (#52): the analytic time map holds beats within 1 ms over 32 bars, without a
pilot click track, and the plan's key shift and key lock come out at the right pitch."""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from seedbox2.mix import plan as v3
from seedbox2.mix import stretch as st

from .test_seedbox2_mix_plan import _deck, _kf, _plan

SR = 44100
BEATS = 128  # 32 bars
HIT = 32


def _click_track(bpm: float, start_s: float, beats: int = BEATS, seconds: float | None = None) -> np.ndarray:
    n = int((seconds or start_s + beats * 60 / bpm + 2) * SR)
    x = np.zeros((2, n), np.float32)
    for b in range(beats):
        p = round((start_s + b * 60 / bpm) * SR)
        x[:, p : p + HIT] = np.hanning(HIT) * 0.9
    return x


def _beat_errors_ms(y: np.ndarray, expected: np.ndarray) -> np.ndarray:
    """Each click's centre of energy against where it should be."""
    energy = y[0].astype(float) ** 2
    errors = []
    for c in expected:
        a = max(int(c) - 1500, 0)
        s = energy[a : a + 3000]
        centre = np.searchsorted(np.cumsum(s) / s.sum(), 0.5)
        errors.append((a + centre - c) / SR * 1000)
    return np.array(errors)


def _ramp_plan(bpm0: float, bpm1: float, **deck: object) -> v3.MixPlan:
    tempo = [_kf(bpm0, 0.0, "linear"), _kf(bpm1, float(BEATS))]
    a = _deck("a", grid=[{"start_beat": 0.0, "start_s": 1.0, "bpm": 124.0}], play_from_beat=0.0,
              play_to_beat=float(BEATS), **deck)  # fmt: skip
    return v3.validate(_plan(window={"start_beat": 0.0, "end_beat": float(BEATS)}, tempo=tempo, a=a, b=a))


def _pitch_hz(y: np.ndarray, near: float) -> float:
    a = y[0, SR : SR + 2**17].astype(float) * np.hanning(2**17)
    spectrum = np.abs(np.fft.rfft(a))
    freqs = np.fft.rfftfreq(2**17, 1 / SR)
    band = (freqs > near * 0.8) & (freqs < near * 1.25)
    return float(freqs[band][np.argmax(spectrum[band])])


def _sine(hz: float, seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return np.stack([0.5 * np.sin(2 * np.pi * hz * t)] * 2).astype(np.float32)


# --- the time map ----------------------------------------------------------------------------------------------------


def test_the_map_is_the_tempo_lane_integrated() -> None:
    plan = _ramp_plan(124.0, 128.0)
    beats = st.master_beats(plan.tempo, 0.0, float(BEATS), SR)
    # d(seconds)/d(beat) = 60 / (a + b * beat), which has a closed form
    slope = 4.0 / BEATS
    exact = 60.0 / slope * np.log((124.0 + slope * BEATS) / 124.0)
    assert beats.size == int(exact * SR)
    i = int(exact / 2 * SR)
    assert beats[i] == pytest.approx(124.0 * (np.exp(i / SR * slope / 60.0) - 1) / slope, abs=1e-6)


def test_a_tempo_step_is_a_step_in_the_map() -> None:
    tempo = [v3.Keyframe(beat=0.0, value=120.0, curve="hold"), v3.Keyframe(beat=8.0, value=60.0, curve="hold")]
    beats = st.master_beats(tempo, 0.0, 16.0, SR)
    assert beats.size == int((8 * 0.5 + 8 * 1.0) * SR)
    assert beats[int(4.0 * SR)] == pytest.approx(8.0, abs=1e-3)
    assert beats[int(8.0 * SR)] == pytest.approx(12.0, abs=1e-3)


@pytest.mark.parametrize(
    ("bpm0", "bpm1", "semitones"),
    [(127.0, 127.0, 0), (120.0, 120.0, 0), (124.0, 128.0, 0), (128.0, 120.0, 0), (124.0, 128.0, 2), (127.0, 127.0, -1)],
)
def test_beats_stay_within_a_millisecond_over_32_bars(bpm0: float, bpm1: float, semitones: int) -> None:
    plan = _ramp_plan(bpm0, bpm1, key_shift=semitones)
    y = st.render_deck(plan, "a", _click_track(124.0, 1.0), SR)
    beats = st.master_beats(plan.tempo, 0.0, float(BEATS), SR)
    expected = np.searchsorted(beats, np.arange(1, BEATS - 1, dtype=float)) + HIT / 2 / (y.shape[1] / beats.size)
    errors = _beat_errors_ms(y, expected)
    assert np.abs(errors).max() < 1.0, np.round(errors, 2)


def test_a_piecewise_grid_stays_within_a_millisecond_too() -> None:
    """A live drummer: the file's tempo moves, and the deck follows the grid to a constant master tempo."""
    period_a, period_b, cut = 60 / 120.0, 60 / 123.0, 64
    times = [1.0 + b * period_a if b < cut else 1.0 + cut * period_a + (b - cut) * period_b for b in range(BEATS)]
    x = np.zeros((2, int((times[-1] + 2) * SR)), np.float32)
    for t in times:
        x[:, round(t * SR) : round(t * SR) + HIT] = np.hanning(HIT) * 0.9
    grid = [{"start_beat": 0.0, "start_s": 1.0, "bpm": 120.0},
            {"start_beat": float(cut), "start_s": 1.0 + cut * period_a, "bpm": 123.0}]  # fmt: skip
    a = _deck("a", grid=grid, play_from_beat=0.0, play_to_beat=float(BEATS))
    plan = v3.validate(_plan(window={"start_beat": 0.0, "end_beat": float(BEATS)}, tempo=[_kf(125.0)], a=a, b=a))
    y = st.render_deck(plan, "a", x, SR)
    expected = np.arange(1, BEATS - 1) * 60 / 125.0 * SR + HIT / 2 * 125.0 / 121.0
    assert np.abs(_beat_errors_ms(y, expected)).max() < 1.0


def test_nothing_is_measured_with_a_second_stretch() -> None:
    """Seedbox 1's pilot track took a second run of Rubber Band. This engine doesn't load it at all."""
    code = "import sys, seedbox2.mix.stretch; sys.exit('pedalboard' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# --- pieces ----------------------------------------------------------------------------------------------------------


def _pieces(plan: v3.MixPlan) -> tuple[list[st.Piece], np.ndarray]:
    beats = st.master_beats(plan.tempo, plan.window.start_beat, plan.window.end_beat, SR)
    return st.pieces(st.time_map(plan.a, beats, SR), beats, SR), beats


def test_a_constant_ratio_is_one_piece_with_no_seams() -> None:
    found, beats = _pieces(_ramp_plan(127.0, 127.0))
    assert [(p.start, p.end) for p in found] == [(0, beats.size)]
    assert found[0].ratio == pytest.approx(127.0 / 124.0, abs=1e-9)


def test_a_ramp_is_cut_on_beats_and_no_more_than_it_needs() -> None:
    found, beats = _pieces(_ramp_plan(124.0, 128.0))
    assert 1 < len(found) < BEATS / 2
    for p in found[1:]:
        assert beats[p.start] - np.round(beats[p.start]) == pytest.approx(0.0, abs=1e-3)
    assert [p.end for p in found[:-1]] == [p.start for p in found[1:]]


def test_a_fast_ramp_is_cut_inside_the_beat_rather_than_miss_the_tolerance() -> None:
    plan = _ramp_plan(124.0, 124.0)
    beats = st.master_beats([_kf_model(100.0), _kf_model(200.0, 8.0)], 0.0, 8.0, SR)
    pos = st.time_map(plan.a, beats, SR)
    assert any(abs(beats[p.start] - round(beats[p.start])) > 0.1 for p in st.pieces(pos, beats, SR))


def _kf_model(value: float, beat: float = 0.0) -> v3.Keyframe:
    return v3.Keyframe(beat=beat, value=value, curve="linear")


def test_a_ramp_keeps_a_held_note_level_through_its_seams() -> None:
    plan = _ramp_plan(124.0, 128.0)
    y = st.render_deck(plan, "a", _sine(220.0, 70.0), SR)
    body = y[0, SR : y.shape[1] - SR].astype(float)
    win = SR // 20  # 50 ms, about what the ear integrates over
    rms = np.sqrt(np.convolve(body**2, np.ones(win) / win, "valid"))[:: win // 4]
    assert 20 * np.log10(rms.min() / np.median(rms)) > -3.0


def test_a_slip_loop_repeats_its_slice_and_comes_back_on_time() -> None:
    a = _deck("a", grid=[{"start_beat": 0.0, "start_s": 1.0, "bpm": 124.0}], play_from_beat=0.0, play_to_beat=32.0,
              loops=[{"start_beat": 8.0, "end_beat": 16.0, "source_beat": 8.0, "length_beats": 1.0}])  # fmt: skip
    plan = v3.validate(_plan(window={"start_beat": 0.0, "end_beat": 32.0}, tempo=[_kf(127.0)], a=a, b=a))
    src = _click_track(124.0, 1.0, beats=32)
    y = st.render_deck(plan, "a", src, SR)
    expected = np.arange(1, 31) * 60 / 127.0 * SR + HIT / 2 * 124.0 / 127.0
    assert np.abs(_beat_errors_ms(y, expected)).max() < 1.0  # clicks on every beat, the loop's included
    found, _ = _pieces(plan)
    assert len(found) >= 8


def test_a_short_roll_fits_shorter_seams_and_stays_in_time() -> None:
    a = _deck("a", grid=[{"start_beat": 0.0, "start_s": 1.0, "bpm": 124.0}], play_from_beat=0.0, play_to_beat=16.0,
              loops=[{"start_beat": 4.0, "end_beat": 8.0, "source_beat": 4.0, "length_beats": 0.0625}])  # fmt: skip
    plan = v3.validate(_plan(window={"start_beat": 0.0, "end_beat": 16.0}, tempo=[_kf(127.0)], a=a, b=a))
    y = st.render_deck(plan, "a", _click_track(124.0, 1.0, beats=16), SR)
    expected = np.arange(9, 15) * 60 / 127.0 * SR + HIT / 2 * 124.0 / 127.0
    assert np.abs(_beat_errors_ms(y, expected)).max() < 1.0  # the beats after the roll


# --- pitch -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("semitones", [-2, -1, 1, 2])
@pytest.mark.parametrize("bpm", [124.0, 127.0])
def test_key_shift_lands_on_the_semitone_at_any_tempo(semitones: int, bpm: float) -> None:
    plan = _ramp_plan(bpm, bpm, key_shift=semitones)
    y = st.render_deck(plan, "a", _sine(440.0, 70.0), SR)
    want = 440.0 * 2 ** (semitones / 12)
    assert abs(1200 * np.log2(_pitch_hz(y, want) / want)) < 10.0  # cents: the engine lands a lone sine within 9


def test_key_lock_holds_the_pitch_through_a_stretch() -> None:
    y = st.render_deck(_ramp_plan(127.0, 127.0), "a", _sine(440.0, 70.0), SR)
    assert _pitch_hz(y, 440.0) == pytest.approx(440.0, abs=0.5)


def test_without_key_lock_the_pitch_follows_the_tempo_and_the_shift_comes_on_top() -> None:
    plan = _ramp_plan(127.0, 127.0, key_shift=1)
    plan = plan.model_copy(update={"key_lock": False})
    y = st.render_deck(plan, "a", _sine(440.0, 70.0), SR)
    want = 440.0 * 127.0 / 124.0 * 2 ** (1 / 12)
    assert abs(1200 * np.log2(_pitch_hz(y, want) / want)) < 10.0



def test_each_deck_has_its_own_shift_in_the_plan() -> None:
    a = _deck("a", key_shift=1)
    b = _deck("b", key_shift=-2)
    plan = v3.validate(_plan(a=a, b=b))
    assert (plan.a.key_shift, plan.b.key_shift) == (1, -2)
    with pytest.raises(ValueError, match="key_shift"):
        v3.validate(_plan(a=_deck("a", key_shift=3)))


# --- the deck on the timeline ----------------------------------------------------------------------------------------


def test_a_deck_at_its_own_tempo_is_a_copy_of_the_file() -> None:
    src = np.random.default_rng(0).normal(0, 0.1, (2, 40 * SR)).astype(np.float32)
    a = _deck("a", grid=[{"start_beat": 0.0, "start_s": 1.0, "bpm": 120.0}], play_from_beat=0.0, play_to_beat=16.0)
    plan = v3.validate(_plan(window={"start_beat": 0.0, "end_beat": 16.0}, tempo=[_kf(120.0)], a=a, b=a))
    y = st.render_deck(plan, "a", src, SR)
    assert np.array_equal(y, src[:, SR : SR + y.shape[1]])


def test_a_deck_is_silent_where_it_does_not_play_and_past_the_end_of_its_file() -> None:
    a = _deck("a", grid=[{"start_beat": 0.0, "start_s": 0.0, "bpm": 124.0}], play_from_beat=8.0, play_to_beat=24.0)
    b = _deck("b", play_from_beat=0.0, play_to_beat=32.0)
    plan = v3.validate(_plan(window={"start_beat": 0.0, "end_beat": 32.0}, tempo=[_kf(127.0)], a=a, b=b))
    src = _sine(220.0, 6.0)  # ends around beat 12.4
    y = st.render_deck(plan, "a", src, SR)
    beat = 60 / 127.0 * SR
    assert not y[:, : int(8 * beat) - 1].any()
    assert not y[:, int(24 * beat) + 1 :].any()
    assert np.abs(y[:, int(9 * beat) : int(10 * beat)]).max() > 0.1
    assert np.abs(y[:, int(14 * beat) : int(24 * beat)]).max() < 1e-3
    assert y.shape[1] == st.master_beats(plan.tempo, 0.0, 32.0, SR).size


# --- stems -----------------------------------------------------------------------------------------------------------


def _drums(bpm: float, seconds: float) -> np.ndarray:
    """A noisy hit with a body, on every beat."""
    rng = np.random.default_rng(1)
    hit = np.exp(-np.arange(3000) / 300.0) * (rng.normal(size=3000) * 0.4 + np.sin(2 * np.pi * 180 * np.arange(3000) / SR))
    x = np.zeros((2, int(seconds * SR)), np.float32)
    for t in np.arange(0.5, seconds - 0.5, 60 / bpm):
        x[:, round(t * SR) : round(t * SR) + hit.size] += hit.astype(np.float32)
    return x


def _pre_echo_db(y: np.ndarray, hits: np.ndarray) -> float:
    before = sum(float((y[0, h - 1500 : h - 100].astype(float) ** 2).sum()) for h in hits)
    after = sum(float((y[0, h : h + 3000].astype(float) ** 2).sum()) for h in hits)
    return 10 * np.log10(before / after)


def test_drums_get_settings_that_keep_their_transients() -> None:
    assert st.STEM_PROFILES["drums"].block_s < st.FULL.block_s
    assert set(st.STEM_PROFILES) == {"drums", "bass", "vocals", "other"}
    plan = _ramp_plan(128.0, 128.0)
    src = _drums(124.0, 70.0)
    hits = (np.arange(0.5 - 1.0 + 60 / 124.0 * 8, 50.0, 60 / 124.0) * 124.0 / 128.0 * SR).astype(np.int64)
    as_drums = _pre_echo_db(st.render_deck(plan, "a", src, SR, profile=st.DRUMS), hits)
    as_mix = _pre_echo_db(st.render_deck(plan, "a", src, SR), hits)
    assert as_drums < as_mix - 1.0, (as_drums, as_mix)


def test_stems_stay_together_on_the_timeline() -> None:
    plan = _ramp_plan(124.0, 128.0)
    clicks = _click_track(124.0, 1.0)
    out = st.render_stems(plan, "a", {"drums": clicks, "bass": clicks, "vocals": clicks, "other": 0 * clicks}, SR)
    beats = st.master_beats(plan.tempo, 0.0, float(BEATS), SR)
    expected = np.searchsorted(beats, np.arange(1, BEATS - 1, dtype=float)) + HIT / 2
    for name in ("drums", "bass", "vocals"):
        assert out[name].shape[1] == beats.size
        assert np.abs(_beat_errors_ms(out[name], expected)).max() < 1.0, name
    assert not out["other"].any()  # a silent stem takes no engine time either


# --- the comparison --------------------------------------------------------------------------------------------------


def test_the_comparison_differs_only_in_the_stretcher() -> None:
    from musicreco import mixrender
    from seedbox2.eval import stretchtest

    original = mixrender._to_master
    src = _click_track(124.0, 1.0, beats=16)
    n = 8 * SR
    jq, speed = np.arange(n) * 1.03 + SR, np.full(n, 1.03)
    beats_q = np.arange(n) / SR * 127.72 / 60  # noqa: F841 (the replacement reads it from the caller, as in Seedbox 1)
    with stretchtest.signalsmith_in_seedbox1():
        assert mixrender._to_master is not original
        y, note = mixrender._to_master(src, speed, jq, SR, True, 0)
        same, _ = mixrender._to_master(src, np.ones(n), np.arange(n) + SR, SR, True, 0)
    assert mixrender._to_master is original
    assert note == ""
    assert y.shape == (2, n)
    expected = np.arange(1, 14) * 60 / 124.0 * SR / 1.03 + HIT / 2 / 1.03
    assert np.abs(_beat_errors_ms(y, expected)).max() < 1.0
    assert np.array_equal(same, src[:, SR : SR + n])  # at its own tempo the deck is still Seedbox 1's copy


def test_stretched_pairs_are_those_with_audio_and_a_gap_widest_first() -> None:
    from types import SimpleNamespace as NS

    from seedbox2.eval import stretchtest

    tracks = {"a": NS(bpm=120.0), "b": NS(bpm=120.0), "c": NS(bpm=123.0), "d": NS(bpm=126.0), "e": NS(bpm=180.0)}
    pair = lambda x, y: NS(id=x + y, a=x, b=y)
    corpus = NS(tracks=tracks, pairs=[pair("a", "b"), pair("a", "c"), pair("a", "d"), pair("a", "e"), pair("a", "z")])
    found = dict.fromkeys(tracks, "file")
    assert [p.id for p in stretchtest.stretched_pairs(corpus, found)] == ["ad", "ac"]


def test_mix_metrics_read_the_plans_key_shift() -> None:
    from seedbox2.eval.mixmetrics import Render, key_and_stretch

    plan = _plan(a=_deck("a", key_shift=1), b=_deck("b", key_shift=-2))
    plan = v3.validate(plan).model_dump()
    silent = np.zeros((2, 10), np.float32)
    r = Render(mix=silent, decks={"A": silent, "B": silent}, sr=SR, plan=plan, keys={})
    out = key_and_stretch(r)
    assert (out["a_semitones"], out["b_semitones"]) == (1.0, -2.0)
