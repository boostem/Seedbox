"""The live engine (#65) in a real browser, whole: real server, real FLAC chunks, a real-time AudioContext, the worklet.

Needs Chrome and a built frontend2/dist; skipped without them, as `python -m seedbox2.bench frontend` is.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from seedbox2.analysis import stems as st
from seedbox2.analysis import waveform as wf
from seedbox2.bench import frontend
from seedbox2.library import Library
from seedbox2.mix import plan as v3

SR = 44100
BPM = 120.0
DIST = frontend.DIST

pytestmark = pytest.mark.skipif(
    frontend.find_chrome() is None or not (DIST / "index.html").exists() or shutil.which("ffmpeg") is None,
    reason="needs Chrome, ffmpeg and a built frontend2/dist",
)


def _kf(beat: float, value: float, curve: str = "hold") -> dict:
    return {"beat": beat, "value": value, "curve": curve}


def _deck(track_id: str, *, stems: bool) -> dict:
    return {
        "track_id": track_id, "grid": [{"start_beat": 0.0, "start_s": 0.0, "bpm": BPM}],
        "play_from_beat": 0.0, "play_to_beat": 80.0, "key_shift": 0,
        "lanes": {name: [_kf(0.0, 0.4 if name == "volume" else v3.LANE_DEFAULTS[name])] for name in v3.LANES},
        "stem_lanes": {
            "gain_drums": [_kf(0.0, 1.0)], "gain_bass": [_kf(0.0, 1.0), _kf(16.0, 0.0, "linear")],
            "gain_vocals": [_kf(0.0, 0.5)], "gain_other": [_kf(0.0, 1.0)],
        } if stems else None,
        "sends": {"echo_beats": 0.75, "echo_tap": "pre_eq", "reverb_size": 0.75, "reverb_tap": "pre_eq"},
        "loops": [], "fx": [],
    }  # fmt: skip


def _track(path: Path, seed: int) -> np.ndarray:
    t = np.arange(45 * SR) / SR
    x = 0.25 * np.sin(2 * np.pi * (110 + 40 * seed) * t)
    wavfile.write(path, SR, np.stack([x, x]).T.astype(np.float32))
    return x


def test_it_plays_a_plan_in_real_time_with_stems_on_one_deck_and_the_fallback_on_the_other(tmp_path, monkeypatch):
    from websockets.sync.client import connect

    from seedbox2.server import start_server

    monkeypatch.setenv("SEEDBOX_SUPPORT_DIR", str(tmp_path / "support"))
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SEEDBOX_LOG_DIR", str(tmp_path / "logs"))
    (tmp_path / "support").mkdir()
    lib = Library.open(tmp_path / "support" / "library.sqlite3")
    _track(tmp_path / "a.wav", 0)
    _track(tmp_path / "b.wav", 1)
    a = lib.add_file(tmp_path / "a.wav")["hash"]
    b = lib.add_file(tmp_path / "b.wav")["hash"]
    folder = st.folder_for(a)
    folder.mkdir(parents=True)
    for stem in st.STEMS:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "a.wav"), "-af", "volume=0.5",
                        str(folder / f"{stem}.flac")], check=True)  # fmt: skip
    lib.put_artifact(a, st.ARTIFACT_KIND, "htdemucs:test", folder)
    lib.close()

    plan = v3.validate({
        "technique": "test", "bars": 8, "window": {"start_beat": 0.0, "end_beat": 64.0}, "tempo": [_kf(0.0, BPM)],
        "a": _deck(a, stems=True), "b": _deck(b, stems=True),
    }).model_dump()  # fmt: skip

    server = start_server(DIST, port=0)
    try:
        with tempfile.TemporaryDirectory(prefix="seedbox2-engine-chrome-", ignore_cleanup_errors=True) as profile:
            proc, errors, ws = frontend._launch(frontend.find_chrome(), Path(profile), (800, 600))
            try:
                with connect(ws, max_size=None, open_timeout=30) as conn:
                    cdp = frontend.Cdp(conn)
                    cdp.call("Page.enable")
                    cdp.call("Page.navigate", url=f"{server.url.rstrip('/')}/#/bench")
                    frontend._wait(lambda: cdp.eval("!!window.__seedboxBench?.ready"), 30, "the #/bench page")
                    cdp.eval(f"""(async () => {{
                        const e = window.__engine = await window.__seedboxBench.liveEngine();
                        await e.load("a", {{ hash: {json.dumps(a)} }});
                        await e.load("b", {{ hash: {json.dumps(b)} }});
                        e.schedule({json.dumps(plan)});
                        await e.play();
                        const an = window.__meter = e.output.context.createAnalyser();
                        an.fftSize = 2048;
                        e.output.connect(an);
                    }})()""", timeout=60)  # fmt: skip

                    def snap() -> dict:
                        return cdp.eval("""(() => {
                            const e = window.__engine, buf = new Float32Array(2048);
                            window.__meter.getFloatTimeDomainData(buf);
                            return { t: e.seconds(), beat: e.beat(), status: e.state.status, decks: e.state.decks,
                                     stats: e.stats(), peak: buf.reduce((m, v) => Math.max(m, Math.abs(v)), 0) };
                        })()""")  # fmt: skip

                    first, t0 = snap(), time.monotonic()
                    time.sleep(2.0)
                    second, t1 = snap(), time.monotonic()
                    assert first["status"] == "playing"
                    assert first["decks"] == {"a": "stems", "b": "full"}  # the graceful fallback, on b
                    assert second["t"] - first["t"] == pytest.approx(t1 - t0, abs=0.25)  # a clock that runs at real time
                    assert second["beat"] == pytest.approx(second["t"] * BPM / 60, abs=1e-3)
                    assert second["peak"] > 0.05  # sound
                    assert second["stats"]["missingSamples"] == 0
                    assert 0 < second["stats"]["peakDecodedBytes"] <= 200 * 1024 * 1024

                    cdp.eval("window.__engine.seek(40)")
                    time.sleep(1.5)
                    moved = snap()
                    assert moved["beat"] > 40 and moved["beat"] < 46
                    assert moved["peak"] > 0.05

                    cdp.eval("window.__engine.pause()")
                    time.sleep(0.4)
                    paused = snap()
                    time.sleep(0.4)
                    still = snap()
                    assert paused["status"] == "paused" and still["t"] == paused["t"] and still["peak"] == 0

                    cdp.eval("window.__engine.close()")
            finally:
                frontend._stop(proc)
                errors.close()
    finally:
        server.stop()


def _beat_track(path: Path, seed: int) -> None:
    """A kick on every beat with a tone over it: a waveform with something to look at, and sound everywhere."""
    t = np.arange(45 * SR) / SR
    phase = (t * BPM / 60) % 1
    kick = 0.5 * np.exp(-phase * 9) * np.sin(2 * np.pi * 70 * phase * 0.5)
    tone = 0.12 * np.sin(2 * np.pi * (220 + 110 * seed) * t) * (0.5 + 0.5 * np.sin(2 * np.pi * t / (8 + 4 * seed)))
    x = kick + tone
    wavfile.write(path, SR, np.stack([x, x]).T.astype(np.float32))


def _blend(a: str, b: str) -> dict:
    """A into B over 32 beats, both decks on their volume lanes."""
    deck_a, deck_b = _deck(a, stems=False), _deck(b, stems=False)
    deck_a["play_to_beat"], deck_b["play_from_beat"] = 48.0, 16.0
    deck_b["grid"] = [{"start_beat": 16.0, "start_s": 0.0, "bpm": BPM}]
    deck_a["lanes"]["volume"] = [_kf(0.0, 0.8), _kf(16.0, 0.8, "linear"), _kf(48.0, 0.0, "linear")]
    deck_b["lanes"]["volume"] = [_kf(0.0, 0.0), _kf(16.0, 0.0, "linear"), _kf(48.0, 0.8, "linear")]
    return v3.validate({
        "technique": "blend", "bars": 8, "window": {"start_beat": 0.0, "end_beat": 64.0}, "tempo": [_kf(0.0, BPM)],
        "a": deck_a, "b": deck_b,
    }).model_dump()  # fmt: skip


def test_the_mixes_page_scrubs_loops_and_takes_edits_while_it_plays(tmp_path, monkeypatch):
    """Real pointer events on #/mixes, through to what the worklet reports and what comes out of it."""
    from websockets.sync.client import connect

    from seedbox2.server import start_server

    monkeypatch.setenv("SEEDBOX_SUPPORT_DIR", str(tmp_path / "support"))
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SEEDBOX_LOG_DIR", str(tmp_path / "logs"))
    (tmp_path / "support").mkdir()
    lib = Library.open(tmp_path / "support" / "library.sqlite3")
    _beat_track(tmp_path / "a.wav", 0)
    _beat_track(tmp_path / "b.wav", 1)
    a = lib.add_file(tmp_path / "a.wav")["hash"]
    b = lib.add_file(tmp_path / "b.wav")["hash"]
    lib.put_plan(a, b, _blend(a, b), user_edited=False)
    for hash_, name in ((a, "a.wav"), (b, "b.wav")):
        path = wf.artifact_path(hash_)
        path.parent.mkdir(parents=True, exist_ok=True)
        wf.write(wf.compute(tmp_path / name), path)
        lib.put_artifact(hash_, wf.ARTIFACT_KIND, wf.MODEL_VERSION, path)
    lib.close()

    # the page's engine, seen from outside: the worklet's reports, and a meter on its output
    spy = """
        const Node = window.AudioWorkletNode;
        window.__pos = null;
        window.AudioWorkletNode = class extends Node {
            constructor(...args) {
                super(...args);
                this.port.addEventListener("message", (e) => { if (e.data.type === "position") window.__pos = e.data; });
                const meter = window.__meter = args[0].createAnalyser();
                meter.fftSize = 2048;
                this.connect(meter);
            }
        };
    """

    server = start_server(DIST, port=0)
    try:
        with tempfile.TemporaryDirectory(prefix="seedbox2-mixes-chrome-", ignore_cleanup_errors=True) as profile:
            proc, errors, ws = frontend._launch(frontend.find_chrome(), Path(profile), (1280, 720))
            try:
                with connect(ws, max_size=None, open_timeout=30) as conn:
                    cdp = frontend.Cdp(conn)
                    cdp.call("Page.enable")
                    cdp.call("Page.addScriptToEvaluateOnNewDocument", source=spy)
                    cdp.call("Page.navigate", url=f"{server.url.rstrip('/')}/#/mixes")
                    frontend._wait(lambda: cdp.eval("!!document.querySelector('[data-testid=plan-timeline]')"), 30, "the panel")

                    def click(name: str) -> None:
                        cdp.eval(f"""[...document.querySelectorAll('button')].find((b) => b.textContent.includes({json.dumps(name)})).click()""")

                    def pressed(name: str) -> bool:
                        return cdp.eval(f"""[...document.querySelectorAll('button')].find((b) => b.textContent.includes({json.dumps(name)})).getAttribute('aria-pressed') === 'true'""")

                    box = cdp.eval("(() => { const r = document.querySelector('[data-testid=plan-timeline]').getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width }; })()")

                    def mouse(kind: str, beat: float, y: float) -> None:
                        cdp.call("Input.dispatchMouseEvent", type=kind, x=box["x"] + box["w"] * beat / 64, y=box["y"] + y,
                                 button="left", buttons=0 if kind == "mouseReleased" else 1, clickCount=1, pointerType="mouse")  # fmt: skip

                    def out() -> dict:
                        return cdp.eval("""(() => {
                            const buf = new Float32Array(2048), p = window.__pos;
                            window.__meter.getFloatTimeDomainData(buf);
                            return { beat: p && p.q / 44100 * 2, playing: p && p.playing, missing: p && p.missing,
                                     peak: buf.reduce((m, v) => Math.max(m, Math.abs(v)), 0) };
                        })()""")  # fmt: skip

                    def wave_pixels() -> int:
                        return cdp.eval("""(() => {
                            const c = document.querySelector('[data-testid=plan-timeline] canvas');
                            const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                            let coloured = 0; // the grid and the background are grey: the waves aren't
                            for (let i = 0; i < d.length; i += 4) {
                                if (Math.max(d[i], d[i + 1], d[i + 2]) - Math.min(d[i], d[i + 1], d[i + 2]) > 40) coloured++;
                            }
                            return coloured;
                        })()""")  # fmt: skip

                    frontend._wait(lambda: wave_pixels() > 2000, 15, "the waveforms")

                    # scrub from a standstill: heard, and the position follows the pointer
                    wave_y = 22 + 42 + 60  # inside deck B, clear of the volume lane's points on A
                    mouse("mousePressed", 8.0, wave_y)
                    frontend._wait(lambda: abs((out()["beat"] or 0) - 8.0) < 0.1, 10, "the scrub's position")
                    heard = 0.0
                    for i in range(24):
                        mouse("mouseMoved", 8.0 + i * 0.5, wave_y)
                        time.sleep(1 / 60)
                        heard = max(heard, out()["peak"])
                    assert heard > 0.02
                    frontend._wait(lambda: abs(out()["beat"] - 19.5) < 0.1, 2, "the position to reach the pointer")
                    assert out()["playing"] is False
                    mouse("mouseReleased", 19.5, wave_y)
                    time.sleep(0.4)
                    stopped = out()
                    assert stopped["peak"] == 0 and stopped["playing"] is False
                    assert abs(stopped["beat"] - 19.5) < 0.1

                    # play, from there
                    click("Play")
                    frontend._wait(lambda: out()["playing"], 15, "it to play")
                    time.sleep(0.6)
                    assert out()["peak"] > 0.02

                    # loop the transition: it stays in beats 16 to 48
                    click("Loop the transition")
                    assert pressed("Loop the transition")

                    # A keyframe dragged while it plays: B's edit is there, and the transport doesn't stop or jump
                    before = out()
                    y_top = 22 + 8  # volume 1 at the deck's top; A's point at beat 48 is at 0, its bottom
                    y_of = lambda v: y_top + (1 - v) * (84 - 16)  # noqa: E731
                    assert not cdp.eval("""[...document.querySelectorAll('button')].find((b) => b.textContent.includes('Discard')) !== undefined""")
                    mouse("mousePressed", 16.0, y_of(0.8))
                    for v in (0.7, 0.5, 0.3, 0.15):
                        mouse("mouseMoved", 16.0, y_of(v))
                        time.sleep(0.03)
                    mouse("mouseReleased", 16.0, y_of(0.15))
                    time.sleep(0.2)
                    after = out()
                    assert after["playing"] is True
                    assert 16.0 <= after["beat"] < 48.0
                    assert after["beat"] != before["beat"]
                    assert pressed("B: Your edit")
                    assert cdp.eval("""[...document.querySelectorAll('button')].some((b) => b.textContent.includes('Discard'))""")

                    # A/B: both ways round, playing throughout
                    click("A: Auto")
                    time.sleep(0.2)
                    assert pressed("A: Auto") and out()["playing"] is True
                    click("B: Your edit")
                    time.sleep(0.2)
                    assert pressed("B: Your edit") and out()["playing"] is True

                    # select a loop on the ruler: the playhead goes round in it
                    mouse("mousePressed", 24.0, 10)
                    mouse("mouseMoved", 28.0, 10)
                    mouse("mouseReleased", 28.0, 10)
                    time.sleep(0.2)
                    click("Pause")
                    time.sleep(0.3)
                    click("Play")
                    time.sleep(0.3)
                    assert out()["playing"] is True
                    for _ in range(12):
                        time.sleep(0.25)
                        # a master beat of 0.5 s: past beat 28 it would be gone inside this loop's 3 s if it didn't loop
                        assert out()["beat"] <= 28.0 + 0.1
                    assert out()["peak"] > 0.005

            finally:
                frontend._stop(proc)
                errors.close()
    finally:
        server.stop()
