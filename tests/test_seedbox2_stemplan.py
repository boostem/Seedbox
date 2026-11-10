"""Stem-aware transitions (#53): one bass at a time and silence rather than an EQ kill, one vocal at a time without
ducking the mids, B's drums in first and A's out last with the kicks never together, mashup moments when the keys and
the vocal windows line up, and all of it as checks on the plan as it plays. A render of tracks with stems at the end."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.io import wavfile

from musicreco.paths import ffmpeg_path
from seedbox2.analysis.grid import Grid
from seedbox2.eval import mixes
from seedbox2.mix import automation, offline, stemplan
from seedbox2.mix import plan as v3

SR = 44100
BPM = 120.0
SPB = 60.0 / BPM
A0, B0 = 40.0, 10.0  # where master beat 0 is in each file
needs_ffmpeg = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")


def _kf(value: float, beat: float = 0.0, curve: str = "hold") -> dict:
    return {"beat": beat, "value": value, "curve": curve}


def _deck(track_id: str, start_s: float, **lanes: list[dict]) -> dict:
    all_lanes = {name: [_kf(v3.LANE_DEFAULTS[name])] for name in v3.LANES} | lanes
    return {"track_id": track_id, "grid": [{"start_beat": 0.0, "start_s": start_s, "bpm": BPM}],
            "play_from_beat": -64.0, "play_to_beat": 64.0, "lanes": all_lanes}  # fmt: skip


SWAP_A = [_kf(0.0, 31.0, "linear"), _kf(-30.0, 32.0)]  # Seedbox 1's swap: a 1-beat crossfade of the lows
SWAP_B = [_kf(-30.0, 31.0, "linear"), _kf(0.0, 32.0)]
DUCK = [_kf(0.0, 8.0, "ease"), _kf(-18.0, 16.0, "hold"), _kf(-18.0, 32.0, "ease"), _kf(0.0, 40.0)]


def _plan(a: dict | None = None, b: dict | None = None, **over: object) -> v3.MixPlan:
    """A 16-bar blend: B's fader up over its first 2 bars, A's down over its last 2; the lows swap on beat 32."""
    a = a or _deck("a", A0, volume=[_kf(1.0, 56.0, "linear"), _kf(0.0, 64.0)], eq_low=SWAP_A)
    b = b or _deck("b", B0, volume=[_kf(0.0, -2.0, "linear"), _kf(1.0, 6.0)], eq_low=SWAP_B)
    return v3.validate({"technique": "long blend", "bars": 16, "window": {"start_beat": -32.0, "end_beat": 96.0},
                        "tempo": [_kf(BPM)], "a": {**a, "play_to_beat": 64.0}, "b": {**b, "play_to_beat": 128.0},
                        **over})  # fmt: skip


def _flat() -> v3.MixPlan:
    """The blend with no low EQ swap: nothing keeps the kicks apart."""
    return _plan(a=_deck("a", A0, volume=[_kf(1.0, 56.0, "linear"), _kf(0.0, 64.0)]),
                 b=_deck("b", B0, volume=[_kf(0.0, -2.0, "linear"), _kf(1.0, 6.0)]))  # fmt: skip


def _s(start: float, end: float, file0: float) -> tuple[float, float]:
    """Master beats -> a deck's file seconds."""
    return file0 + start * SPB, file0 + end * SPB


def _track(file0: float, stems: bool = True, camelot: str | None = "8A", vocals: list | None = None, **kw) -> stemplan.Track:
    phrases = [file0 + beat * SPB for beat in range(-64, 129, 32)]
    voc = None if vocals is None else [_s(s, e, file0) for s, e in vocals]
    return stemplan.Track(has_stems=stems, camelot=camelot, vocals=voc, phrase_starts=phrases, **kw)


def _value(deck: v3.Deck, lane: str, beat: float) -> float:
    lanes = deck.stem_lanes if lane.startswith("gain_") else deck.lanes
    return float(v3.lane_value(getattr(lanes, lane), beat))


# --- bass ------------------------------------------------------------------------------------------------------------


def test_the_bass_changes_hands_on_the_phrase_by_stem_gain_with_true_silence():
    a, b = _track(A0, vocals=[]), _track(B0, vocals=[])
    before = stemplan.check(_plan(), a, b)
    out, report = stemplan.plan(_plan(), a, b)
    assert report["bass_beat"] == 32.0
    assert _value(out.a, "gain_bass", 31.5) == 1.0 and _value(out.a, "gain_bass", 32.0) == 0.0
    assert _value(out.b, "gain_bass", 31.9) == 0.0 and _value(out.b, "gain_bass", 32.25) == 1.0
    assert _value(out.a, "eq_low", 31.5) == 0.0 and _value(out.a, "eq_low", 33.0) == -30.0  # A's lows carry to it
    assert out.b.lanes.eq_low == _plan().b.lanes.eq_low  # B's EQ as planned: it keeps its kick out too
    assert report["checks"]["bass_overlap_db"] == -120.0 < before["bass_overlap_db"]
    assert report["checks"]["ok"]


def test_without_stems_the_hand_off_falls_back_to_the_lows_on_the_same_beat():
    a, b = _track(A0, stems=False, vocals=[]), _track(B0, stems=False, vocals=[])
    out, report = stemplan.plan(_plan(), a, b)
    playing = v3.fold_stem_lanes(out, lambda _t: False)
    assert playing.a.stem_lanes is None and playing.b.stem_lanes is None
    assert _value(playing.a, "eq_low", 31.0) == 0.0 and _value(playing.a, "eq_low", 33.0) == -30.0
    assert _value(playing.b, "eq_low", 31.0) == -30.0 and _value(playing.b, "eq_low", 33.0) == 0.0
    assert report["checks"]["bass_overlap_db"] <= stemplan.LIMITS["bass_overlap_db"] and report["checks"]["ok"]


def test_the_hand_off_is_the_phrase_of_b_nearest_to_where_its_lows_came_in():
    b = _track(B0, vocals=[])
    b.phrase_starts = [B0 + 24 * SPB]  # B's phrase starts on master beat 24 (an 8-bar phrase from an odd intro)
    out, report = stemplan.plan(_plan(), _track(A0, vocals=[]), b)
    assert report["bass_beat"] == 24.0
    assert _value(out.b, "eq_low", 24.0) == 0.0 and _value(out.b, "gain_bass", 24.5) == 1.0  # B's lows open on it
    assert _value(out.a, "gain_bass", 24.5) == 0.0
    assert _value(out.a, "filter", 26.0) >= stemplan.KICK_FILTER  # A's lows are up to 31: its kick goes by the filter
    assert report["checks"]["ok"]
    b.phrase_starts = []  # no phrases: the 4-bar grid from beat 0
    assert stemplan.plan(_plan(), _track(A0, vocals=[]), b)[1]["bass_beat"] == 32.0


# --- vocals ----------------------------------------------------------------------------------------------------------


def test_clashing_vocals_mute_one_stem_for_the_overlap_and_the_mids_stay_up():
    a, b = _track(A0, vocals=[(-32, 40)]), _track(B0, vocals=[(24, 96)])
    generated = _plan(b=_deck("b", B0, volume=[_kf(0.0, -2.0, "linear"), _kf(1.0, 6.0)], eq_low=SWAP_B, eq_mid=DUCK))
    assert stemplan.check(generated, a, b)["vocal_overlap_s"] > 2.0  # the duck lets go from 32 and they clash to 40
    out, report = stemplan.plan(generated, a, b)
    assert _value(out.b, "gain_vocals", 20.0) == 1.0 and _value(out.b, "gain_vocals", 28.0) == 0.0  # A leads to 32
    assert _value(out.b, "gain_vocals", 33.0) == 1.0
    assert _value(out.a, "gain_vocals", 30.0) == 1.0 and _value(out.a, "gain_vocals", 36.0) == 0.0  # then B does
    assert all(k.value > -18.0 for k in out.b.lanes.eq_mid)  # no duck of the whole mid band
    assert report["checks"]["vocal_overlap_s"] <= stemplan.LIMITS["vocal_overlap_s"] and report["checks"]["ok"]


def test_a_vocal_that_ends_before_the_next_starts_is_left_alone():
    a, b = _track(A0, vocals=[(-32, 16)]), _track(B0, vocals=[(32, 96)])
    out, report = stemplan.plan(_plan(), a, b)
    assert all(k.value == 1.0 for d in (out.a, out.b) for k in d.stem_lanes.gain_vocals)
    assert report["checks"]["vocal_overlap_s"] == 0.0


def test_the_deck_with_stems_is_the_one_muted():
    a, b = _track(A0, vocals=[(-32, 40)]), _track(B0, stems=False, vocals=[(24, 96)])
    out, report = stemplan.plan(_plan(), a, b)
    assert _value(out.a, "gain_vocals", 28.0) == 0.0 and _value(out.b, "gain_vocals", 28.0) == 1.0
    assert report["checks"]["ok"]


def test_two_decks_without_stems_keep_the_mid_duck():
    a, b = _track(A0, stems=False, vocals=[(-32, 40)]), _track(B0, stems=False, vocals=[(24, 96)])
    generated = _plan(b=_deck("b", B0, volume=[_kf(0.0, -2.0, "linear"), _kf(1.0, 6.0)], eq_low=SWAP_B, eq_mid=DUCK))
    out, _ = stemplan.plan(generated, a, b)
    assert out.b.lanes.eq_mid == generated.b.lanes.eq_mid
    assert any("neither deck has stems" in n for n in out.notes)


# --- drums -----------------------------------------------------------------------------------------------------------


def test_b_drums_come_in_filtered_a_drums_go_out_last_and_kicks_never_double():
    flat = _flat()
    a, b = _track(A0, vocals=[]), _track(B0, vocals=[])
    assert stemplan.check(flat, a, b)["kick_double_s"] > 10.0
    out, report = stemplan.plan(flat, a, b)
    for beat in (2.0, 16.0, 31.75):
        assert _value(out.b, "filter", beat) >= stemplan.KICK_FILTER and _value(out.b, "gain_drums", beat) == 1.0
        assert _value(out.a, "filter", beat) == 0.0 and _value(out.a, "gain_drums", beat) == 1.0
    for beat in (32.0, 48.0, 60.0):
        assert _value(out.a, "filter", beat) >= stemplan.KICK_FILTER and _value(out.a, "gain_drums", beat) == 1.0
        assert _value(out.b, "filter", beat) == 0.0
    assert automation.filter_hz(stemplan.KICK_FILTER) == pytest.approx(stemplan.KICK_HP_HZ, rel=0.01)
    assert report["checks"]["kick_double_s"] == 0.0 and report["checks"]["ok"]


def test_a_planned_filter_sweep_above_the_floor_is_kept():
    sweep = [_kf(0.0, 32.0, "linear"), _kf(0.8, 64.0)]
    generated = _plan(a=_deck("a", A0, volume=[_kf(1.0, 56.0, "linear"), _kf(0.0, 64.0)], filter=sweep))
    out, _ = stemplan.plan(generated, _track(A0, vocals=[]), _track(B0, vocals=[]))
    assert _value(out.a, "filter", 60.0) == pytest.approx(_value(generated.a, "filter", 60.0), abs=0.01)
    assert _value(out.a, "filter", 34.0) == pytest.approx(stemplan.KICK_FILTER, abs=0.01)


# --- mashup moments --------------------------------------------------------------------------------------------------


def test_b_acapella_over_a_instrumental_when_keys_and_windows_line_up():
    a, b = _track(A0, camelot="8A", vocals=[]), _track(B0, camelot="9A", vocals=[(0, 30)])
    out, report = stemplan.plan(_plan(), a, b)
    m = report["mashup"]
    assert m == {"shift": 0, "direction": "forward", "start": 0.0, "end": 32.0, "bars": 8, "bass_beat": 32.0}
    for beat in (4.0, 20.0):
        assert _value(out.b, "gain_vocals", beat) == 1.0
        assert all(_value(out.b, lane, beat) == 0.0 for lane in ("gain_drums", "gain_bass", "gain_other"))
        assert _value(out.a, "gain_vocals", beat) == 0.0 and _value(out.a, "gain_drums", beat) == 1.0
    assert all(_value(out.b, lane, 33.0) == 1.0 for lane in v3.STEM_LANES)
    assert any(n.startswith("Mashup: B's acapella") for n in out.notes) and report["checks"]["ok"]


def test_no_mashup_across_a_key_clash_or_without_the_singers_stems():
    a = _track(A0, camelot="8A", vocals=[])
    assert stemplan.plan(_plan(), a, _track(B0, camelot="3B", vocals=[(0, 30)]))[1]["mashup"] is None
    assert stemplan.plan(_plan(), a, _track(B0, stems=False, camelot="8A", vocals=[(0, 30)]))[1]["mashup"] is None
    assert stemplan.plan(_plan(), _track(A0, camelot="8A", vocals=[(-8, 40)]),
                         _track(B0, camelot="8A", vocals=[(0, 30)]))[1]["mashup"] is None  # A isn't instrumental


def test_b_moves_by_whole_phrases_to_line_its_vocal_up():
    # B sings from 16 s into the file to 32 s: master beats 32-64 as generated, 0-32 once B is 32 beats on
    b = _track(B0, camelot="8B", vocals=[(32, 63)])
    out, report = stemplan.plan(_plan(), _track(A0, camelot="8A", vocals=[]), b)
    assert report["mashup"]["shift"] == 32 and report["mashup"]["direction"] == "forward"
    assert float(v3.deck_time(out.b, 0.0)) == pytest.approx(B0 + 32 * SPB)


def test_a_acapella_over_b_instrumental_after_the_hand_off():
    a, b = _track(A0, camelot="8A", vocals=[(30, 64)]), _track(B0, camelot="8A", vocals=[])
    out, report = stemplan.plan(_plan(), a, b)
    assert report["mashup"]["direction"] == "reverse" and report["mashup"]["start"] == 32.0
    assert _value(out.a, "gain_vocals", 40.0) == 1.0
    assert all(_value(out.a, lane, 40.0) == 0.0 for lane in ("gain_drums", "gain_bass", "gain_other"))
    assert _value(out.b, "gain_vocals", 40.0) == 0.0 and _value(out.b, "gain_drums", 40.0) == 1.0
    assert report["checks"]["ok"]


def test_compatible_keys_follow_the_key_shift():
    a, b = _track(A0, camelot="8A"), _track(B0, camelot="10A")
    assert not stemplan.keys_compatible(a, b, _plan())
    shifted = _plan(b={**_deck("b", B0, volume=[_kf(0.0, -2.0, "linear"), _kf(1.0, 6.0)]), "key_shift": -2})
    assert stemplan.keys_compatible(a, b, shifted)  # two semitones down is two steps back round the wheel: 8A
    assert stemplan.keys_compatible(_track(A0, camelot="8A"), _track(B0, camelot="8B"), _plan())


# --- planner and checks ----------------------------------------------------------------------------------------------


def test_the_rules_are_put_back_after_the_automation_moves_lanes():
    a, b = _track(A0, vocals=[(-32, 40)]), _track(B0, vocals=[(24, 96)])
    out, report = stemplan.plan(_flat(), a, b)
    broken = out.model_copy(update={
        "a": out.a.model_copy(update={"lanes": out.a.lanes.model_copy(update={"filter": [v3.Keyframe(beat=0.0, value=0.0)]})}),
        "b": out.b.model_copy(update={"lanes": out.b.lanes.model_copy(update={"filter": [v3.Keyframe(beat=0.0, value=0.0)]}),
                                      "stem_lanes": out.b.stem_lanes.model_copy(
            update={"gain_bass": [v3.Keyframe(beat=0.0, value=0.8)], "gain_vocals": [v3.Keyframe(beat=0.0, value=1.0)]})}),
    })  # fmt: skip
    bad = stemplan.check(broken, a, b)
    assert not bad["ok"] and bad["kick_double_s"] > 0 and bad["bass_overlap_db"] > -20.0
    fixed = stemplan.enforce(broken, report)
    assert stemplan.check(fixed, a, b) == report["checks"]
    assert stemplan.enforce(fixed, report) == fixed  # idempotent
    v3.validate(fixed.model_dump())


def test_edited_plans_and_cuts_are_left_alone():
    a, b = _track(A0), _track(B0)
    edited = _plan(edited=True)
    assert stemplan.plan(edited, a, b) == (edited, {"skipped": "edited"})
    cut = _plan(a=_deck("a", A0, volume=[_kf(1.0, 0.0), _kf(0.0, 0.0)]), b=_deck("b", B0, volume=[_kf(0.0, 0.0), _kf(1.0, 0.0)]))
    out, report = stemplan.plan(cut, a, b)
    assert out == cut and report["skipped"] == "cut"


def test_unknown_vocals_are_neither_ruled_nor_checked():
    out, report = stemplan.plan(_plan(), _track(A0), _track(B0))
    assert report["checks"]["vocal_overlap_s"] is None and report["checks"]["ok"]
    assert any("not known" in n for n in out.notes)


def test_a_track_from_its_analyses():
    grid = [{"start_beat": 0.0, "start_s": 1.0, "bpm": BPM}]
    an = {"grid": grid, "vocal_phrases": [{"start_s": 10.0, "end_s": 20.0, "start_beat": 18, "end_beat": 38}],
          "vocal_acapella": [{"start_s": 12.0, "end_s": 14.0}], "vocal_instrumental": []}  # fmt: skip
    sections = {"first_bar_beat": 0, "phrases": [{"bar": 0, "start_s": 1.0, "length": 8}],
                "activity": {"bass": [0.0, 1.0], "drums": [1.0, 1.0]}}  # fmt: skip
    t = stemplan.track_from_analysis(an, has_stems=True, camelot="8A", sections=sections)
    assert t.vocals == [(10.0, 20.0)] and t.acapella == [(12.0, 14.0)] and t.phrase_starts == [1.0]
    assert t.activity("bass", np.array([1.5, 3.5, 99.0])).tolist() == [0.0, 1.0, 0.0]  # bar 0, bar 1, past the end
    heuristic = stemplan.track_from_analysis({"vocal_per_bar": [0, 0.8, 0.9, 0], "bar_s": 2.0, "first_downbeat_s": 1.0},
                                             has_stems=False)  # fmt: skip
    assert heuristic.vocals == [(3.0, 7.0)] and heuristic.activity is None
    assert stemplan.track_from_analysis({}, has_stems=False).vocals is None


# --- with stems, through the renderer --------------------------------------------------------------------------------


def _stem_track(seconds: float, sung: list[tuple[float, float]], f_bass: float, seed: int) -> dict[str, np.ndarray]:
    """A four-on-the-floor track as its four stems (mono): kick and hats, a bassline, a sung-like line in `sung`
    (seconds), a quiet pad."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    drums, bass = np.zeros(n), np.zeros(n)
    kl, hl, bl = int(0.25 * SR), int(0.04 * SR), int(0.2 * SR)
    tk = np.arange(kl) / SR
    kick = np.sin(2 * np.pi * np.cumsum(50 + 120 * np.exp(-tk * 30)) / SR) * np.exp(-tk * 9) * 0.8
    hat = np.diff(np.diff(rng.standard_normal(hl) * np.exp(-np.arange(hl) / SR * 90), prepend=0), prepend=0) * 0.15
    note = np.sin(2 * np.pi * f_bass * np.arange(bl) / SR) * np.exp(-np.arange(bl) / SR * 4) * 0.5
    for beat in range(int(seconds / SPB)):
        i, j = int(beat * SPB * SR), int((beat + 0.5) * SPB * SR)
        drums[i: i + kl] += kick[: n - i]
        drums[j: j + hl] += hat[: n - j]
        bass[j: j + bl] += note[: n - j]
    f_voc = 330 * (1 + 0.012 * np.sin(2 * np.pi * 5.5 * t))
    ph = 2 * np.pi * np.cumsum(f_voc) / SR
    gate = np.zeros(n)
    for s, e in sung:
        gate[int(s * SR): int(e * SR)] = 1.0
    vocals = sum(np.sin(h * ph) / h for h in range(1, 7)) * 0.15 * gate
    other = np.sin(2 * np.pi * 220 * t) * 0.03
    return {"drums": drums, "bass": bass, "vocals": vocals, "other": other}


@needs_ffmpeg
def test_a_render_with_stems_keeps_to_the_rules(tmp_path):
    # A sings to master beat 40, B from beat 24: they clash over 8 bars of the blend without the stem planner
    stems = {"a": _stem_track(80.0, [(A0 - 16.0, A0 + 40 * SPB)], 55.0, 1),
             "b": _stem_track(80.0, [(B0 + 24 * SPB, B0 + 60.0)], 61.7, 2)}
    paths, files = {}, {}
    for deck, parts in stems.items():
        files[deck] = {}
        for name, x in parts.items():
            files[deck][name] = tmp_path / f"{deck}_{name}.wav"
            wavfile.write(files[deck][name], SR, np.stack([x, x]).T.astype(np.float32))
        mix = sum(parts.values())
        paths[deck] = tmp_path / f"{deck}.wav"
        wavfile.write(paths[deck], SR, np.stack([mix, mix]).T.astype(np.float32))
    plan = _plan()
    audio_a, audio_b = offline.deck_audio(plan, paths["a"], paths["b"], files["a"], files["b"])
    a, b = offline.tracks_for(plan, audio_a, audio_b, ("8A", "3B"))
    assert a.vocals and b.vocals  # the vocal stems' phrases were found
    for track in (a, b):
        track.phrase_starts = _track(A0 if track is a else B0).phrase_starts
    assert not stemplan.check(_plan(b=_deck("b", B0, volume=[_kf(0.0, -2.0, "linear"), _kf(1.0, 6.0)])), a, b)["ok"]
    out, report = stemplan.plan(plan, a, b)
    assert report["checks"]["ok"] and report["checks"]["vocal_overlap_s"] <= 0.5
    r = offline.render(out, paths["a"], paths["b"], audio_a, audio_b)
    clock = r["clock"]

    def level(deck: str, beat0: float, beat1: float) -> float:
        i0, i1 = (int(float(clock.time(x)) * SR) for x in (beat0, beat1))
        return 10 * np.log10(np.mean(r["stems"][deck][:, i0:i1] ** 2) + 1e-12)

    assert level("A", 36.0, 40.0) < level("A", 20.0, 24.0) - 6.0  # A's vocal and bass out once B leads


@needs_ffmpeg
def test_the_engine_runs_the_stem_planner(tmp_path, monkeypatch):
    monkeypatch.setattr(mixes, "SYNTHETIC", mixes.SYNTHETIC[2:3])  # vocal outro into vocal intro
    (pair,) = mixes.synthetic_pairs(tmp_path)
    r = mixes.render_seedbox2(pair)
    assert r.plan["stems"]["checks"]["ok"]


def test_a_track_from_its_stems_finds_the_vocal_and_the_levels():
    parts = _stem_track(40.0, [(10.0, 30.0)], 55.0, 3)
    stems = {k: np.stack([x, x]) for k, x in parts.items()}
    t = stemplan.track_from_audio(stems, SR, Grid([{"start_beat": 0.0, "start_s": 0.0, "bpm": BPM}]), camelot="8A")
    assert t.has_stems and t.vocals
    assert t.vocals[0][0] == pytest.approx(10.0, abs=1.0) and t.vocals[-1][1] == pytest.approx(30.0, abs=1.0)
    early = np.arange(4.0, 8.0, stemplan.ENVELOPE_S)
    assert t.activity("bass", early).max() > 0.5 and t.activity("vocals", early).max() < 0.05
