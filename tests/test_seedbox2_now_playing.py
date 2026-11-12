"""GET /api/sets/{id}/session (#77): what the now-playing view plays a set from."""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from scipy.io import wavfile

from musicreco import mixplan as sb1
from seedbox2.analysis import beats, energy, sections
from seedbox2.bench import frontend
from seedbox2.library import Library
from seedbox2.mix import plan as v3
from seedbox2.server.routers.sets import set_session

from .fixtures import PAIRS, analysis, track
from .s2_ready import make_ready


def _kf(value: float) -> dict:
    return {"beat": 0.0, "value": value, "curve": "hold"}


def _deck(track: str) -> dict:
    return {"track_id": track, "grid": [{"start_beat": 0.0, "start_s": 0.0, "bpm": 120.0}], "play_from_beat": -32.0, "play_to_beat": 64.0,
            "lanes": {name: [_kf(v3.LANE_DEFAULTS[name])] for name in v3.LANES}}  # fmt: skip


def _plan(a: str, b: str) -> dict:
    return {"version": v3.PLAN_VERSION, "technique": "bass swap", "bars": 16, "window": {"start_beat": -32.0, "end_beat": 64.0},
            "tempo": [_kf(120.0)], "a": _deck(a), "b": _deck(b)}  # fmt: skip


def _library(tmp_path) -> tuple[Library, int]:
    lib = Library.open(tmp_path / "library.sqlite3")
    for h, title in (("a" * 40, "One"), ("b" * 40, "Two"), ("c" * 40, "Three")):
        lib.upsert_track(h, 240.0, title=title, artist="Someone")
    make_ready(lib)
    return lib, lib.save_set("Friday", ["a" * 40, "b" * 40, "c" * 40])


def test_session_has_the_tracks_in_order_with_what_is_known_and_each_pairs_plan(tmp_path) -> None:
    lib, set_id = _library(tmp_path)
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    lib.put_analysis(a, beats.ANALYSER, beats.VERSION, {"bpm": 126.0, "first_downbeat_s": 0.25, "beats_per_bar": 4, "segments": []})
    lib.put_analysis(a, energy.LOUDNESS_ANALYSER, energy.VERSION, {"integrated_lufs": -9.5})
    lib.put_analysis(b, sections.ANALYSER, sections.VERSION, {"sections": [], "phrases": [
        {"bar": 0, "start_s": 0.5, "length": 32, "novelty": 0.9}, {"bar": 32, "start_s": 61.0, "length": 8, "novelty": 0.4}]})
    lib.put_plan(a, b, _plan(a, b), user_edited=False)

    s = set_session(set_id, lib)

    assert s["name"] == "Friday"
    assert [t["title"] for t in s["tracks"]] == ["One", "Two", "Three"]
    one, two = s["tracks"][:2]
    assert one["grid"] == {"bpm": 126.0, "first_downbeat_s": 0.25, "beats_per_bar": 4, "segments": []}
    assert one["phrases"] == []
    assert one["integrated_lufs"] == -9.5 and one["duration"] == 240.0
    assert two["grid"] == dict.fromkeys(("bpm", "first_downbeat_s", "beats_per_bar", "segments"))  # an empty grid
    assert two["integrated_lufs"] is None and two["sections"] == []
    assert two["phrases"] == [{"start_s": 0.5, "level": 32}, {"start_s": 61.0, "level": 8}]
    assert two["key"]["camelot"] is None
    assert len(s["plans"]) == 2
    assert s["plans"][0]["version"] == v3.PLAN_VERSION and s["plans"][0]["a"]["track_id"] == a
    assert s["plans"][1] is None


def test_a_stored_plan_of_seedbox_1_arrives_as_version_3(tmp_path) -> None:
    lib, set_id = _library(tmp_path)
    a, b = "a" * 40, "b" * 40
    _, one, two = PAIRS[0]
    lib.put_plan(a, b, sb1.generate(track(one), analysis(one), track(two), analysis(two), "folder"), user_edited=True)
    plan = set_session(set_id, lib)["plans"][0]
    assert plan["version"] == v3.PLAN_VERSION
    assert plan["a"]["track_id"] and plan["b"]["track_id"]
    assert plan["a"]["play_from_beat"] < plan["a"]["play_to_beat"]


def test_a_plan_that_cannot_be_read_doesnt_stop_the_set(tmp_path) -> None:
    lib, set_id = _library(tmp_path)
    lib.put_plan("a" * 40, "b" * 40, {"version": v3.PLAN_VERSION, "nonsense": True}, user_edited=False)
    assert set_session(set_id, lib)["plans"] == [None, None]


def test_a_set_that_isnt_there_is_a_404(tmp_path) -> None:
    lib, _ = _library(tmp_path)
    with pytest.raises(HTTPException) as e:
        set_session(999, lib)
    assert e.value.status_code == 404


# --- in a real browser ------------------------------------------------------------------------------------------------

BPM = 120.0
SR = 44100
DIST = frontend.DIST
browser = pytest.mark.skipif(
    frontend.find_chrome() is None or not (DIST / "index.html").exists() or shutil.which("ffmpeg") is None,
    reason="needs Chrome, ffmpeg and a built frontend2/dist",
)


def _live_deck(track_id: str, start_beat: float, play_from: float, play_to: float) -> dict:
    return {"track_id": track_id, "grid": [{"start_beat": start_beat, "start_s": 0.0, "bpm": BPM}],
            "play_from_beat": play_from, "play_to_beat": play_to,
            "lanes": {name: [_kf(0.4 if name == "volume" else v3.LANE_DEFAULTS[name])] for name in v3.LANES}}  # fmt: skip


def _tone(path, seed: int) -> None:
    t = np.arange(45 * SR) / SR
    x = 0.25 * np.sin(2 * np.pi * (110 + 40 * seed) * t)
    wavfile.write(path, SR, np.stack([x, x]).T.astype(np.float32))


@browser
def test_a_set_plays_through_a_hand_off_without_a_gap(tmp_path, monkeypatch):
    from websockets.sync.client import connect

    from seedbox2.server import start_server

    monkeypatch.setenv("SEEDBOX_SUPPORT_DIR", str(tmp_path / "support"))
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SEEDBOX_LOG_DIR", str(tmp_path / "logs"))
    (tmp_path / "support").mkdir()
    lib = Library.open(tmp_path / "support" / "library.sqlite3")
    hashes = []
    for i, name in enumerate(("one", "two", "three")):
        _tone(tmp_path / f"{name}.wav", i)
        added = lib.add_file(tmp_path / f"{name}.wav")
        lib.upsert_track(added["hash"], added["duration"], title=name)
        hashes.append(added["hash"])
    make_ready(lib, *hashes)
    x, y, z = hashes
    # x -> y blends over beats 8-16, and the plan's window ends on 32, so y goes on to the next plan on about beat 24
    lib.put_plan(x, y, {"version": v3.PLAN_VERSION, "technique": "bass swap", "bars": 4, "window": {"start_beat": 0.0, "end_beat": 32.0},
                        "tempo": [_kf(BPM)], "a": _live_deck(x, 0.0, 0.0, 16.0), "b": _live_deck(y, 8.0, 8.0, 32.0)},
                 user_edited=False)  # fmt: skip
    lib.put_plan(y, z, {"version": v3.PLAN_VERSION, "technique": "drop swap", "bars": 4, "window": {"start_beat": 0.0, "end_beat": 60.0},
                        "tempo": [_kf(BPM)], "a": _live_deck(y, 0.0, 0.0, 40.0), "b": _live_deck(z, 30.0, 30.0, 60.0)},
                 user_edited=False)  # fmt: skip
    set_id = lib.save_set("Friday", hashes)
    lib.close()

    server = start_server(DIST, port=0, power_mode="off")
    try:
        with tempfile.TemporaryDirectory(prefix="seedbox2-now-playing-") as profile:
            proc, errors, ws = frontend._launch(frontend.find_chrome(), Path(profile), (1280, 720))
            try:
                with connect(ws, max_size=None, open_timeout=30) as conn:
                    cdp = frontend.Cdp(conn)
                    cdp.call("Page.enable")
                    cdp.call("Page.navigate", url=f"{server.url.rstrip('/')}/#/now-playing?set={set_id}")
                    frontend._wait(lambda: cdp.eval("!!document.querySelector('section[aria-label=\"Deck B\"] canvas')"),
                                   30, "the decks")  # fmt: skip
                    deck_b = "document.querySelector('section[aria-label=\"Deck B\"]').textContent"
                    frontend._wait(lambda: cdp.eval(f"{deck_b}.includes('two') && !document.querySelector('button[aria-label=Play]').disabled"),
                                   30, "the second track on deck B")  # fmt: skip
                    # the page's AudioContext, and from then on a log per frame: y's file time, and the output's peak
                    cdp.eval("""(() => {
                        document.querySelector('button[aria-label=Play]').click();
                        const d = window.__seedboxNowPlaying.driver;
                        const log = window.__log = [];
                        let an = null;
                        const buf = new Float32Array(1024);
                        const tick = () => {
                            const e = d.engine;
                            if (!an && e.output && d.status() === 'playing') {
                                an = e.output.context.createAnalyser();
                                an.fftSize = 1024;
                                e.output.connect(an);
                            }
                            if (an) {
                                an.getFloatTimeDomainData(buf);
                                const plan = d.plan(), deck = d.index() === 0 ? plan.b : plan.a, g = deck.grid[0];
                                log.push({ i: d.index(), beat: d.beat(), now: performance.now(),
                                           y: g.start_s + (d.beat() - g.start_beat) * 60 / g.bpm,
                                           peak: buf.reduce((m, v) => Math.max(m, Math.abs(v)), 0) });
                            }
                            requestAnimationFrame(tick);
                        };
                        tick();
                    })()""")  # fmt: skip
                    frontend._wait(lambda: cdp.eval("window.__seedboxNowPlaying.driver.index() === 1"), 30, "the hand-off")
                    time.sleep(1.0)
                    log = cdp.eval("window.__log")
                    cut = next(k for k, s in enumerate(log) if s["i"] == 1)
                    around = log[max(0, cut - 30):cut + 30]
                    # y goes on at real-time speed across the plans: no jump in its file time beyond a frame or two
                    moved = around[-1]["y"] - around[0]["y"]
                    elapsed = (around[-1]["now"] - around[0]["now"]) / 1000
                    assert moved == pytest.approx(elapsed, abs=0.12)
                    steps = [b["y"] - a["y"] for a, b in zip(around, around[1:], strict=False)]
                    assert max(abs(s) for s in steps) < 0.2
                    # and it's heard all the way through
                    assert min(s["peak"] for s in around) > 0.02
                    assert cdp.eval("window.__seedboxNowPlaying.driver.engine.stats().missingSamples") == 0
                    # the track stayed where it was; the plan is the next one
                    assert "two" in cdp.eval(deck_b)
                    assert "Track 2 of 3" in cdp.eval("document.body.textContent")
                    strip = cdp.eval("document.querySelector('section[aria-label=\"Up next\"]').textContent")
                    assert "three" in strip and "Drop swap" in strip
                    assert cdp.eval("window.__seedboxNowPlaying.draws()") > 30
            finally:
                frontend._stop(proc)
                errors.close()
    finally:
        server.stop()
