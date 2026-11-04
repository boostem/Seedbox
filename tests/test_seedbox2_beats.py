"""Seedbox 2 beats (#43): the Beat This! front end and peak picker, the piecewise grid fit, the tempo octave, the bar
phase, the job and its API, and piecewise grids in the mix planner and renderer."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from musicreco import mixplan, mixrender
from musicreco.paths import ffmpeg_path
from seedbox2.analysis import beats, beatthis
from seedbox2.analysis.grid import Grid, Segment, coverage, fit, global_period, index_beats

from .fixtures import Spec, analysis, click_offset, click_track, track

FPS = beatthis.FPS


# --- front end and peaks -----------------------------------------------------------------------------------------


def test_mel_filterbank_shape_and_coverage() -> None:
    fb = beatthis.mel_filterbank()
    assert fb.shape == (513, 128)
    assert (fb.sum(axis=0) > 0).all()  # every band has some bins
    freqs = np.linspace(0, 11025, 513)
    assert fb[freqs < 29].sum() == 0 and fb[freqs > 11001].sum() == 0


def test_log_mel_frames_and_level() -> None:
    sr = beatthis.SAMPLE_RATE
    x = np.zeros(sr * 3, np.float32)
    assert beatthis.log_mel(x).shape == (1 + x.size // 441, 128)
    assert np.allclose(beatthis.log_mel(x), 0)
    tone = np.sin(2 * np.pi * 440 * np.arange(sr) / sr).astype(np.float32)
    spec = beatthis.log_mel(tone)
    assert spec[10:-10].argmax(axis=1).std() == 0  # one band holds the tone in every frame


def test_log_mel_matches_torchaudio() -> None:
    torch = pytest.importorskip("torch")
    pytest.importorskip("torchaudio")
    try:
        from beat_this.preprocessing import LogMelSpect
    except Exception:  # noqa: BLE001 - beat_this (dev-only) missing or its torchaudio broken
        pytest.skip("beat_this not installed")
    x = (np.random.default_rng(0).standard_normal(22050 * 5) * 0.1).astype(np.float32)
    want = LogMelSpect()(torch.from_numpy(x)).numpy()
    assert np.abs(want - beatthis.log_mel(x)).max() < 1e-3


def test_chunks_cover_the_piece_like_upstream() -> None:
    assert list(beatthis.chunk_starts(1000)) == [-6]
    s = beatthis.chunk_starts(4000)
    assert s[0] == -6 and s[-1] == 4000 - 1494 and np.all(np.diff(s) <= 1488)


def test_logits_stitch_chunks_without_gaps() -> None:
    n = 3333
    seen = []

    def run(spect: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        seen.append(spect.shape)
        v = spect[0, :, 0].astype(np.float32)  # echo the frame numbers back
        return v, -v

    spect = np.repeat(np.arange(n, dtype=np.float32)[:, None], 128, axis=1)
    beat, down = beatthis.logits(spect, run)
    assert all(s[1] <= 1500 and s[2] == 128 for s in seen)
    assert np.array_equal(beat, np.arange(n)) and np.array_equal(down, -np.arange(n))


def _logits(times: np.ndarray, n: int) -> np.ndarray:
    """Logits with a 3-frame bump (2, 6, 2) at each time, -6 elsewhere."""
    x = np.full(n, -6.0, np.float32)
    for t in times:
        i = int(round(t * FPS))
        for o, v in ((-1, 2.0), (0, 6.0), (1, 2.0)):
            if 0 <= i + o < n:
                x[i + o] = max(x[i + o], v)
    return x


def test_peaks_are_beat_this_minimal_postprocessing() -> None:
    n = 1000
    bt = np.arange(0.5, 19, 0.5)
    db = bt[::4] + 0.02  # a frame late: moved onto the beat
    b, d = beatthis.peaks(_logits(bt, n), _logits(db, n))
    assert np.allclose(b, bt, atol=1e-9)
    assert np.allclose(d, bt[::4], atol=1e-9)
    two = np.full(n, -6.0, np.float32)
    two[100:102] = 3.0  # a flat top of two frames is one peak, in the middle
    assert np.allclose(beatthis.peaks(two, two)[0], [100.5 / FPS])


# --- grid ------------------------------------------------------------------------------------------------------


def test_grid_maps_beats_and_seconds_both_ways() -> None:
    g = Grid([Segment(0, 1.0, 120), Segment(8, 5.0, 60)])
    assert g.time_at(4) == pytest.approx(3.0)
    assert g.time_at(10) == pytest.approx(7.0)
    assert g.time_at(-2) == pytest.approx(0.0)
    assert np.allclose(g.beat_at(np.asarray(g.time_at(np.arange(-3, 20.0)))), np.arange(-3, 20.0))
    assert g.bpm_at(9) == 60 and g.main_bpm(1.0, 30.0) == 60
    assert np.allclose(g.beats(1.0, 7.0), [1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 6, 7])
    assert Grid.from_json(g.to_json()).to_json() == g.to_json()
    with pytest.raises(ValueError):
        Grid([Segment(0, 1.0, 120), Segment(4, 0.5, 120)])


def test_grid_scaling_keeps_the_times() -> None:
    g = Grid([Segment(0, 1.0, 120), Segment(8, 5.0, 100)])
    double, half = g.scaled(2.0), g.scaled(0.5, offset=1.0)
    for b in (0.0, 3.0, 9.0, 13.0):
        assert double.time_at(2 * b) == pytest.approx(g.time_at(b))
        assert half.time_at((b - 1) / 2) == pytest.approx(g.time_at(b))
    assert g.shifted(4).time_at(0) == pytest.approx(g.time_at(4))


def _jitter(t: np.ndarray, ms: float = 6.0, seed: int = 0) -> np.ndarray:
    return t + np.random.default_rng(seed).normal(0, ms / 1000, t.size)


def test_quantised_track_gets_one_exact_segment() -> None:
    t = 0.37 + np.arange(400) * 60 / 128.0
    f = fit(_jitter(t))
    assert f is not None and f.grid.is_constant
    assert f.grid.segments[0].bpm == pytest.approx(128.0, abs=0.01)
    assert np.abs(f.grid.beats(t[0], t[-1]) - t).max() < 0.005
    assert f.coverage > 0.99


def test_missing_extra_and_off_beat_detections_dont_shift_the_count() -> None:
    period = 60 / 126.0
    t = 0.2 + np.arange(300) * period
    det = np.delete(t, [10, 11, 50, 51, 52, 200])  # missed beats
    det = np.sort(np.concatenate([det, t[100:107] + 0.18, t[[20, 30]] + 0.04]))  # extras near and between beats
    det = np.concatenate([det[(det < t[150]) | (det > t[166])], t[150:166] + period / 2])  # an off-beat breakdown
    f = fit(np.sort(det))
    assert f.grid.is_constant
    assert np.abs(f.grid.beats(t[0], t[-1]) - t).max() < 0.004


def test_tempo_change_gives_two_segments() -> None:
    a = 0.5 + np.arange(128) * 60 / 120.0
    b = a[-1] + np.arange(1, 129) * 60 / 128.0
    f = fit(_jitter(np.concatenate([a, b]), 4))
    assert len(f.grid.segments) == 2
    s0, s1 = f.grid.segments
    assert s0.bpm == pytest.approx(120, abs=0.1) and s1.bpm == pytest.approx(128, abs=0.1)
    assert s1.start_beat == pytest.approx(127, abs=2)
    truth = np.concatenate([a, b])
    assert np.abs(np.asarray(f.grid.time_at(np.round(f.grid.beat_at(truth)))) - truth).max() < 0.02


def test_live_drift_is_followed_within_tolerance() -> None:
    # a drummer speeding up from 100 to 106 BPM over 400 beats
    k = np.arange(400)
    bpm = 100 + 6 * k / 400
    t = 1.0 + np.concatenate([[0], np.cumsum(60 / bpm[:-1])])
    f = fit(_jitter(t, 5))
    assert len(f.grid.segments) >= 2
    on = np.asarray(f.grid.time_at(np.round(f.grid.beat_at(t))))
    assert np.mean(np.abs(on - t) < 0.025) > 0.95
    assert coverage(f.grid, t) > 0.95


def test_global_period_and_index_beats() -> None:
    t = 0.1 + np.arange(200) * 0.469
    p, ph = global_period(t, 0.47)
    assert p == pytest.approx(0.469, rel=2e-4) and ph == pytest.approx(0.1, abs=0.01)
    _, k, good = index_beats(np.sort(np.concatenate([t, t[5:8] + 0.01])), 0.469)
    assert np.array_equal(k, np.arange(200)) and good.all()


def test_too_few_beats_is_no_grid() -> None:
    assert fit(np.arange(5) * 0.5) is None


# --- octave, bar phase, hints -------------------------------------------------------------------------------------


def _activations(bpm: float, bars: int, *, downbeat_every: int = 4, first: float = 0.5, phase: int = 0,
                 detect_every: int = 1, n_extra: float = 2.0) -> beatthis.Activations:  # fmt: skip
    period = 60 / bpm
    t = first + np.arange(bars * 4) * period
    det = t[::detect_every]
    db = t[phase::downbeat_every]
    n = int((t[-1] + n_extra) * FPS)
    bl, dl = _logits(det, n), _logits(db, n)
    b, d = beatthis.peaks(bl, dl)
    return beatthis.Activations(bl, dl, b, d)


def test_dnb_tracked_at_half_tempo_comes_out_at_174() -> None:
    # beat_this sees 87 BPM with a downbeat every 2 of its beats: bars of 2 are a level too slow
    act = _activations(87, 64, downbeat_every=2)
    r = beats.analyse_activations(act)
    assert r["bpm"] == pytest.approx(174, abs=0.2) and r["octave"] == 2.0


def test_dnb_at_174_stays() -> None:
    assert beats.analyse_activations(_activations(174, 64))["bpm"] == pytest.approx(174, abs=0.2)


def test_halftime_70_comes_out_at_140() -> None:
    r = beats.analyse_activations(_activations(70, 32))
    assert r["bpm"] == pytest.approx(140, abs=0.2)
    assert beats.analyse_activations(_activations(140, 64))["bpm"] == pytest.approx(140, abs=0.2)


def test_no_folding_into_a_range() -> None:
    for bpm in (85.0, 96.0, 124.0, 160.0, 180.0):
        assert beats.analyse_activations(_activations(bpm, 48))["bpm"] == pytest.approx(bpm, abs=0.2)


def test_double_time_tracking_is_halved_on_the_downbeat_beats() -> None:
    # beat_this on every 8th note of a 100 BPM track, downbeats every 8 of them
    act = _activations(200, 48, downbeat_every=8, phase=3)
    r = beats.analyse_activations(act)
    assert r["bpm"] == pytest.approx(100, abs=0.2)
    g = Grid(r["segments"])
    downs = 0.5 + np.arange(3, 48 * 4, 8) * 60 / 200
    assert np.abs(np.asarray(g.time_at(np.round(g.beat_at(downs) / 4) * 4)) - downs).max() < 0.01


def test_bar_phase_is_voted_across_the_track() -> None:
    act = _activations(124, 32, phase=2)
    # a few wrong downbeat detections don't move it
    dl = act.downbeat.copy()
    for t in act.beats[[5, 9, 13]]:
        dl[int(round(t * FPS))] = 6.0
    act = beatthis.Activations(act.beat, dl, act.beats, beatthis.peaks(act.beat, dl)[1])
    r = beats.analyse_activations(act)
    g = Grid(r["segments"])
    first_down = act.beats[2]
    assert r["first_downbeat_s"] == pytest.approx(first_down, abs=0.01)
    assert g.beat_at(act.beats[6]) == pytest.approx(4, abs=0.01)
    assert r["time_signature"] == {"assumed": 4, "detected": 4, "confidence": r["time_signature"]["confidence"]}


def test_beat_zero_allows_a_pickup() -> None:
    act = _activations(120, 16, phase=1)  # one pickup beat before the first downbeat
    r = beats.analyse_activations(act)
    assert r["first_downbeat_s"] == pytest.approx(act.beats[1], abs=0.005)
    assert Grid(r["segments"]).beat_at(act.beats[0]) == pytest.approx(-1, abs=0.01)


def test_three_four_is_flagged() -> None:
    r = beats.analyse_activations(_activations(150, 48, downbeat_every=3))
    assert r["time_signature"]["detected"] == 3 and r["beats_per_bar"] == 4


def test_tag_bpm_only_breaks_a_tie() -> None:
    bar_unknown = None
    scores = beats.octave_scores(100.0, bar_unknown)
    assert max(scores, key=scores.get) == 1.0
    assert beats.choose_octave(100.0, None, [200.0]) == (1.0, None)  # not close: the hint is ignored
    # 82 vs 164 is close without downbeat evidence: a 164 tag decides it, an 82 tag keeps 82
    s = beats.octave_scores(82.0, None)
    assert abs(s[1.0] - s[2.0]) < beats.HINT_MARGIN
    assert beats.choose_octave(82.0, None, [164.0]) == (2.0, 164.0)
    assert beats.choose_octave(82.0, None, [82.0])[0] == 1.0


def test_bpm_hints_from_tags() -> None:
    tags = {"bpm": 128.0, "file": {"bpm": "128,00", "key": "8A"}, "rekordbox": {"bpm": 64.0},
            "tunebat": {"bpm": 174}, "comment": "x", "deep": {"a": {"b": {"bpm": 99}}}}  # fmt: skip
    assert beats.bpm_hints(tags) == [128.0, 64.0, 174.0]
    assert beats.bpm_hints(None) == [] and beats.bpm_hints({"bpm": "fast"}) == []


def test_beat_times_cover_the_detected_span() -> None:
    r = beats.analyse_activations(_activations(120, 8, first=2.0))
    bt = beats.beat_times(r)
    assert bt[0] == pytest.approx(2.0, abs=0.01) and bt.size == 32
    assert np.allclose(beats.beat_times(r, 4), bt[::4])


def test_no_beats_raises() -> None:
    n = 500
    quiet = np.full(n, -6.0, np.float32)
    with pytest.raises(beats.NoBeats):
        beats.analyse_activations(beatthis.Activations(quiet, quiet, np.zeros(0), np.zeros(0)))


def test_low_power_uses_small0_first() -> None:
    assert beats.model_order(False) == ("beat_this", "beat_this_small")
    assert beats.model_order(True) == ("beat_this_small", "beat_this")


# --- the job and the API -----------------------------------------------------------------------------------------


class _FakeSession:
    """Stands in for an onnxruntime session: beats every 0.5 s (120 BPM), a downbeat every 4."""

    def get_inputs(self) -> list:
        return [type("I", (), {"name": "spectrogram"})()]

    def run(self, _outputs: object, feed: dict) -> list[np.ndarray]:
        n = feed["spectrogram"].shape[1]
        b = np.full((1, n), -6.0, np.float32)
        d = b.copy()
        b[0, ::25] = 6.0
        d[0, ::100] = 6.0
        return [b, d]


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")
def test_analyse_a_file_with_a_session(tmp_path: Path) -> None:
    p = tmp_path / "t.wav"
    wavfile.write(p, 22050, (np.random.default_rng(1).standard_normal(22050 * 40) * 0.05).astype(np.float32))
    r = beats.analyse(p, _FakeSession())
    # the fake logits are per chunk, so they restart at each chunk's border: just check the grid is 120 and complete
    assert r["model"] == "beat_this" and r["bpm"] == pytest.approx(120, abs=1.0)
    assert r["duration_s"] == pytest.approx(40, abs=0.01)


@pytest.fixture
def server(tmp_path, monkeypatch):
    from seedbox2.library import Library
    from seedbox2.server import start_server

    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))  # no models there: the Seedbox 1 fallback
    lib = Library.open(tmp_path / "library.sqlite3")
    spec = Spec("served", bpm=126.0, bars=24, intro_bars=4, outro_bars=4, first_downbeat_s=0.4)
    wav = tmp_path / "song.wav"
    wavfile.write(wav, 22050, click_track(spec, 22050, 1500.0).T)
    track_ = lib.add_file(wav, tags={"file": {"bpm": 126}})
    lib.close()
    handle = start_server(library_path=tmp_path / "library.sqlite3")
    handle.hash = track_["hash"]
    yield handle
    handle.stop()


def _get(url: str) -> tuple[int, dict]:
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - localhost
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")
def test_the_endpoint_queues_the_job_then_serves_the_grid(server) -> None:
    url = f"{server.url}/api/analysis/{server.hash}/beats"
    status, body = _get(url)
    assert status == 202 and body["job"]["kind"] == "beats"
    deadline = time.monotonic() + 60
    while (got := _get(url))[0] == 202 and time.monotonic() < deadline:
        time.sleep(0.2)
    status, data = got
    assert status == 200
    assert data["model"] == "seedbox1" and data["bpm"] == pytest.approx(126, abs=0.2)
    g = Grid(data["segments"])
    on = (g.time_at(0) - 0.4) / (60 / 126)  # on one of the clicks (Seedbox 1 guesses the bar phase)
    assert abs(on - round(on)) < 0.05 and data["beats_per_bar"] == 4
    assert _get(f"{server.url}/api/analysis/{'0' * 40}/beats")[0] == 404


def test_beats_job_is_a_light_per_track_analysis() -> None:
    from seedbox2.jobs import REGISTRY

    names = {t.name: t for t in REGISTRY.per_track()}
    assert names["beats"].pool == "light"


# --- piecewise grids in mix plans ----------------------------------------------------------------------------------


def _deck(grid: list[dict] | None = None) -> dict:
    d = {"track_id": "x", "beat0_s": 1.0, "native_bpm": 120.0, "play_from_beat": 0.0, "play_to_beat": 64.0}
    if grid is not None:
        d["grid"] = grid
    return d


def _plan(a: dict) -> dict:
    return {"from_id": "x", "to_id": "y", "technique": "long blend", "bars": 8, "a": a, "b": {**_deck(), "track_id": "y"},
            "tempo": [{"beat": 0, "value": 120}], "window": {"start_beat": 0, "end_beat": 32}}  # fmt: skip


def test_plan_decks_accept_piecewise_grids() -> None:
    raw = [{"start_beat": 16, "start_s": 0, "bpm": 125}, {"start_beat": 0, "start_s": 1.0, "bpm": 120}]
    plan = mixplan.validate(_plan(_deck(raw)))
    g = plan["a"]["grid"]
    assert [s["start_beat"] for s in g] == [0, 16]
    assert g[1]["start_s"] == pytest.approx(9.0)  # made continuous
    assert plan["a"]["beat0_s"] == pytest.approx(1.0)
    assert mixplan.deck_time(plan["a"], 20.0) == pytest.approx(9.0 + 4 * 60 / 125)
    assert mixplan.deck_bpm(plan["a"], 20.0) == 125 and mixplan.deck_bpm(plan["a"], 3.0) == 120
    assert "grid" not in mixplan.validate(_plan(_deck()))["a"]
    with pytest.raises(ValueError):
        mixplan.validate(_plan(_deck([{"start_beat": 0, "start_s": 0, "bpm": 9}])))


def test_constant_deck_time_is_unchanged() -> None:
    d = _deck()
    assert mixplan.deck_time(d, 8.0) == pytest.approx(5.0)
    assert mixplan.deck_bpm(d, 8.0) == 120.0


def test_analysis_grid_follows_the_deck_and_its_factor() -> None:
    an = {"bpm": 120.0, "grid": [{"start_beat": 0, "start_s": 0.5, "bpm": 120.0},
                                 {"start_beat": 64, "start_s": 32.5, "bpm": 126.0}]}  # fmt: skip
    g = mixplan.analysis_grid(an, 8, 2.0, 120.0)  # deck beat0 = bar 8 = track beat 32
    assert g[0]["start_beat"] == pytest.approx(-32) and mixplan.grid_time(g, 0.0) == pytest.approx(16.5)
    assert g[1]["start_beat"] == pytest.approx(32)
    half = mixplan.analysis_grid(an, 8, 4.0, 240.0)  # B at double time on the master grid: 2 master beats a beat
    assert half[0]["start_beat"] == pytest.approx(-128) and half[1]["start_beat"] == pytest.approx(0)
    assert half[1]["bpm"] == pytest.approx(252)
    assert mixplan.analysis_grid({"bpm": 120.0}, 0, 2.0, 120.0) is None


SR = 44100
DRIFT_A = Spec("drift_a", bpm=124.0, bars=40, intro_bars=8, outro_bars=16)
DRIFT_B = Spec("drift_b", bpm=124.0, bars=40, intro_bars=16, outro_bars=8, first_downbeat_s=0.25)
KNOT = 64  # track beat where A speeds up
FAST = 127.0


def _drift_grid() -> list[dict]:
    t_knot = DRIFT_A.first_downbeat_s + KNOT * 60 / DRIFT_A.bpm
    return [{"start_beat": 0, "start_s": DRIFT_A.first_downbeat_s, "bpm": DRIFT_A.bpm},
            {"start_beat": KNOT, "start_s": t_knot, "bpm": FAST}]  # fmt: skip


def _drift_audio() -> np.ndarray:
    """click_track(DRIFT_A) with its beats on the two-segment grid (124 BPM, then 127 from beat 64)."""
    g = _drift_grid()
    n = int(round((mixplan.grid_time(g, DRIFT_A.bars * 4) + 1.0) * SR))
    x = np.zeros(n, np.float32)
    m = int(0.003 * SR)
    env = np.exp(-np.arange(m) / (0.0006 * SR)).astype(np.float32)
    for k in range(DRIFT_A.bars * 4):
        i = int(round(float(mixplan.grid_time(g, k)) * SR))
        f, amp = (2000.0, 0.8) if k % 4 == 0 else (1000.0, 0.5)
        seg = x[i : i + m]
        seg += amp * env[: seg.size] * np.sin(2 * np.pi * f * np.arange(seg.size) / SR)
    return np.stack([x, x])


@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")
def test_renderer_keeps_a_drifting_deck_on_the_master_clock(tmp_path: Path) -> None:
    a_path, b_path = tmp_path / "a.wav", tmp_path / "b.wav"
    wavfile.write(a_path, SR, _drift_audio().T)
    wavfile.write(b_path, SR, click_track(DRIFT_B, SR, 3000.0).T)
    an = {**analysis(DRIFT_A), "grid": _drift_grid()}
    results = {}
    for name, a_an in (("piecewise", an), ("constant", analysis(DRIFT_A))):
        plan = mixplan.generate(track(DRIFT_A), a_an, track(DRIFT_B), analysis(DRIFT_B), "f", technique="long blend")
        assert ("grid" in plan["a"]) == (name == "piecewise")
        r = mixrender.render_audio(plan, a_path, b_path, SR)
        t_beats = np.asarray(r["beat_times_s"])
        first = np.ceil(r["start_beat"])
        a = plan["a"]
        played = np.arange(first, min(a["play_to_beat"] - 1, plan["window"]["end_beat"]))
        vol = mixplan.lane_value(a["lanes"]["volume"], played)
        played = played[vol > 0.99]
        err = np.array([click_offset(r["stems"]["A"], SR, t) for t in t_beats[(played - first).astype(int)]])
        results[name] = float(np.max(err))
    assert results["piecewise"] < 0.003, f"a beat is {1000 * results['piecewise']:.1f} ms off the master clock"
    assert results["constant"] > 0.01  # the test means something: a constant grid drifts off


def test_segments_split_at_grid_knots() -> None:
    deck = mixplan.validate(_plan(_deck([{"start_beat": 0, "start_s": 1.0, "bpm": 120},
                                         {"start_beat": 8, "start_s": 5.0, "bpm": 60}])))["a"]  # fmt: skip
    deck["loops"] = []
    pieces = mixrender._segments(deck, -2.0, 12.0)
    assert [round(p[0], 6) for p in pieces] == [-2.0, 8.0]
    assert pieces[1][3] == pytest.approx(1.0) and pieces[0][3] == pytest.approx(0.5)
    src, kb, ks = mixrender._source_mapped(np.zeros((2, 44100 * 20), np.float32), deck, -2.0, 12.0, 1000)
    assert src.shape[1] == pytest.approx(10 * 500 + 4 * 1000, abs=1)
    assert np.interp(8.0, kb, ks) == pytest.approx(5000)


# --- the real models (nightly: `pytest -m models`) ---------------------------------------------------------------


@pytest.mark.models
@pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")
@pytest.mark.parametrize("name", beats.MODELS)
def test_beat_this_on_a_click_track(name: str, tmp_path: Path) -> None:
    from seedbox2.models import load_session, store
    from seedbox2.models.registry import MODELS

    root = store.models_dir()
    store.download(MODELS[name], root)
    spec = Spec("real", bpm=128.0, bars=32, intro_bars=4, outro_bars=4, first_downbeat_s=0.6)
    wav = tmp_path / "clicks.wav"
    wavfile.write(wav, 22050, click_track(spec, 22050, 1500.0).T)
    r = beats.analyse(wav, load_session(name, root=root), model=name)
    assert r["constant"] and r["bpm"] == pytest.approx(128.0, abs=0.05)
    g = Grid(r["segments"])
    clicks = spec.first_downbeat_s + np.arange(spec.bars * 4) * 60 / spec.bpm
    assert np.abs(np.asarray(g.time_at(np.round(g.beat_at(clicks)))) - clicks).max() < 0.02
