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
        with tempfile.TemporaryDirectory(prefix="seedbox2-engine-chrome-") as profile:
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
