"""Seedbox 2's waveforms (#49): the Bessel band split, the binary format, the size budget, the job and the endpoint."""

from __future__ import annotations

import gzip
import json
import struct
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from musicreco.paths import ffmpeg_path
from seedbox2.analysis import waveform as wf
from seedbox2.library import Library

pytestmark = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")

SR = 44_100


def write_wav(path: Path, x: np.ndarray, sr: int = SR) -> Path:
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def tone(hz: float, seconds: float, amp: float = 0.5) -> np.ndarray:
    return amp * np.sin(2 * np.pi * hz * np.arange(int(seconds * SR)) / SR)


def test_the_bessel_split_puts_each_tone_in_its_band(tmp_path):
    # 2 s each of 100 Hz, 1.5 kHz and 9 kHz, then 1 s of silence
    path = write_wav(tmp_path / "tones.wav", np.concatenate([tone(100, 2), tone(1500, 2), tone(9000, 2),
                                                             np.zeros(SR)]))  # fmt: skip
    w = wf.compute(path)
    assert w.duration == pytest.approx(7, abs=0.01)
    assert w.points == 7 * wf.POINTS_PER_S
    assert w.detail.shape == (3, 2, w.points) and w.detail.dtype == np.uint8
    assert w.overview.shape == (3, 2, wf.OVERVIEW_COLUMNS) and w.stems.shape == (0, wf.OVERVIEW_COLUMNS)
    peak = wf.to_amplitude(w.detail[:, 0])
    for segment, band in enumerate(range(3)):  # the middle of each tone, away from the filters' transients
        mid = peak[:, int((segment * 2 + 0.5) * 150):int((segment * 2 + 1.5) * 150)].mean(axis=1)
        assert mid[band] == pytest.approx(0.5, rel=0.12), (segment, mid)
        assert all(mid[b] < 0.5 * 0.25 for b in range(3) if b != band), (segment, mid)
    assert (w.detail[:, :, -100:] == 0).all()  # silence
    rms = wf.to_amplitude(w.detail[0, 1, 50:250])  # a sine's RMS is its peak / sqrt(2)
    assert np.median(rms) == pytest.approx(0.5 / np.sqrt(2), rel=0.1)


def test_byte_scale_is_log_with_a_floor():
    amps = np.array([0, 10 ** (-wf.FLOOR_DB / 20), 10 ** (-wf.FLOOR_DB / 40), 1.0, 2.0])
    b = wf.to_byte(amps)
    assert b.tolist()[0:2] == [0, 0] and b[3] == 255 and b[4] == 255 and abs(int(b[2]) - 128) <= 3
    assert len(np.unique(wf.to_byte(np.geomspace(1e-4, 1, 10_000)))) == wf.LEVELS
    assert wf.to_amplitude(wf.to_byte(np.array([0.3])))[0] == pytest.approx(0.3, rel=0.05)


def test_overview_keeps_peaks_and_power_and_handles_short_tracks():
    env = np.zeros((1, 2, 2_400), np.float32)
    env[0, 0, 5] = 1.0  # one transient
    env[0, 1, :] = 0.5
    o = wf.overview(env)
    assert o.shape == (1, 2, 1_200) and o[0, 0, 2] == 1.0 and o[0, 0].sum() == 1.0
    assert np.allclose(o[0, 1], 0.5)
    short = wf.overview(np.arange(2 * 300, dtype=np.float32).reshape(1, 2, 300))  # fewer points than columns
    assert short.shape == (1, 2, 1_200) and short[0, 0, 0] == 0 and short[0, 0, -1] == 299


def test_format_round_trips_with_a_small_header(tmp_path):
    w = wf.compute(write_wav(tmp_path / "a.wav", tone(440, 1.5)))
    data = w.to_bytes()
    h = wf.read_header(data)
    assert data[:4] == b"SBWF" and h["header_size"] == 32
    assert {k: h[k] for k in ("version", "points_per_s", "bands", "values_per_band", "points", "overview_columns",
                              "stems", "floor_db", "sample_rate", "levels")} == {
        "version": wf.VERSION, "points_per_s": 150, "bands": 3, "values_per_band": 2, "points": 225,
        "overview_columns": 1200, "stems": 0, "floor_db": wf.FLOOR_DB, "sample_rate": wf.SAMPLE_RATE,
        "levels": wf.LEVELS}  # fmt: skip
    assert len(data) == 32 + 6 * 225 + 6 * 1200
    back = wf.Waveform.from_bytes(data)
    assert np.array_equal(back.detail, w.detail) and np.array_equal(back.overview, w.overview)
    assert back.duration == pytest.approx(1.5)
    with pytest.raises(wf.WaveformError):
        wf.Waveform.from_bytes(data[:-1])
    with pytest.raises(wf.WaveformError):
        wf.read_header(b"RIFF" + data[4:])


def test_stem_overviews(tmp_path):
    track = write_wav(tmp_path / "t.wav", tone(200, 3))
    folder = tmp_path / "stems"
    folder.mkdir()
    for i, name in enumerate(wf.STEMS):  # drums silent, the others louder and louder
        write_wav(folder / f"{name}.wav", tone(200, 3, amp=0.25 * i))
    stems = wf.stem_files(folder)
    assert stems is not None and list(stems) == sorted(wf.STEMS)
    w = wf.compute(track, stems)
    assert w.stems.shape == (4, wf.OVERVIEW_COLUMNS)
    level = wf.to_amplitude(w.stems[:, 100:1100]).mean(axis=1)
    assert (w.stems[0] == 0).all() and level[1:] == pytest.approx([0.25, 0.5, 0.75], rel=0.1)
    back = wf.Waveform.from_bytes(w.to_bytes())
    assert np.array_equal(back.stems, w.stems)
    (folder / "vocals.wav").unlink()
    assert wf.stem_files(folder) is None
    with pytest.raises(wf.WaveformError):
        wf.compute(track, {"drums": track})


def test_a_dense_six_minute_track_fits_in_200_kb(tmp_path):
    """Noise whose level jumps every few points: harder to compress than real music (real music measured: ≤ 185 KB)."""
    rng = np.random.default_rng(49)
    seconds, sr = 360, 22_050
    gain = np.repeat(rng.uniform(0.05, 1, seconds * 150 // 4), 4 * 147)  # a new level every 4 points
    x = rng.standard_normal(seconds * sr) * 0.3 * gain
    x[:: sr // 2] = 0.99  # transients
    path = write_wav(tmp_path / "six.wav", x, sr)
    stems = {name: path for name in wf.STEMS}
    started = time.perf_counter()
    w = wf.compute(path, stems)
    elapsed = time.perf_counter() - started
    size = wf.write(w, tmp_path / "six.sbwf.gz")
    assert w.points == 360 * 150
    assert size <= 200 * 1024, f"{size / 1024:.0f} KB"
    assert elapsed < 15, f"{elapsed:.1f} s"  # the 'beats + key + waveform' budget is 15 s for a whole track
    assert wf.read(tmp_path / "six.sbwf.gz").points == w.points


def test_progress_and_cancel(tmp_path):
    path = write_wav(tmp_path / "p.wav", tone(300, 4))
    seen: list[float] = []
    wf.compute(path, progress=seen.append, duration_hint=4)
    assert seen[-1] == 1.0 and seen == sorted(seen) and 0 < seen[0] < 0.5

    class Stop(Exception):
        pass

    def stop(fraction: float) -> None:
        raise Stop

    with pytest.raises(Stop):
        wf.compute(path, progress=stop, duration_hint=4)


def test_a_file_that_is_not_audio(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio" * 100)
    with pytest.raises(wf.WaveformError):
        wf.compute(bad)


# --- the job and the endpoint ---------------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    from seedbox2.server import start_server

    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))  # the spawned workers inherit it
    lib = Library.open(tmp_path / "library.sqlite3")
    track = lib.add_file(write_wav(tmp_path / "song.wav", np.concatenate([tone(100, 2), tone(5000, 2)])))
    lib.close()
    handle = start_server(library_path=tmp_path / "library.sqlite3")
    handle.hash = track["hash"]
    yield handle
    handle.stop()


def get(url: str, **headers: str) -> tuple[int, Any, bytes]:  # headers: case-insensitive
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - localhost
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_the_endpoint_queues_the_job_then_serves_the_binary_with_caching(server, tmp_path):
    url = f"{server.url}/api/tracks/{server.hash}/waveform"
    status, _, body = get(url)
    assert status == 202
    job = json.loads(body)["job"]
    assert job["kind"] == "waveform" and job["payload"] == {"hash": server.hash}
    deadline = time.monotonic() + 30
    while (status := get(url)[0]) == 202 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert status == 200
    status, headers, raw = get(url)
    assert headers["Content-Type"] == "application/octet-stream" and "Content-Encoding" not in headers
    w = wf.Waveform.from_bytes(raw)
    assert w.points == 4 * 150 and w.detail[0, 0, 150] > w.detail[2, 0, 150] and w.detail[2, 0, 450] > 200
    etag = headers["ETag"]
    assert etag == f'"{server.hash}.v{wf.VERSION}.s0"'

    status, headers, gz = get(url, **{"Accept-Encoding": "gzip, deflate"})
    assert headers["Content-Encoding"] == "gzip" and gzip.decompress(gz) == raw
    assert len(gz) < len(raw)
    assert get(url, **{"If-None-Match": etag})[0] == 304
    assert get(url, **{"If-None-Match": '"something-else"'})[0] == 200

    files = list((tmp_path / "cache" / "waveforms").rglob("*.sbwf.gz"))
    assert [f.name for f in files] == [f"{server.hash}.sbwf.gz"]
    jobs = json.loads(get(f"{server.url}/api/jobs")[2])
    assert "waveform" in jobs["types"] and [j["state"] for j in jobs["items"]] == ["done"]
    assert jobs["items"][0]["result"]["bytes"] == files[0].stat().st_size


def test_the_endpoint_404s_for_unknown_tracks(server):
    assert get(f"{server.url}/api/tracks/{'0' * 40}/waveform")[0] == 404


def test_stem_detail_is_its_own_file(tmp_path):
    track = write_wav(tmp_path / "t.wav", tone(200, 3))
    folder = tmp_path / "stems"
    folder.mkdir()
    for i, name in enumerate(wf.STEMS):  # drums silent, the others louder and louder
        write_wav(folder / f"{name}.wav", tone(200, 3, amp=0.25 * i))
    w = wf.compute(track, wf.stem_files(folder))
    assert w.stem_detail.shape == (4, w.points)
    assert (w.stem_detail[0] == 0).all()
    assert wf.to_amplitude(w.stem_detail[1:, 100:300]).mean(axis=1) == pytest.approx([0.25, 0.5, 0.75], rel=0.1)
    raw = w.stems_to_bytes()
    magic, version, size, pps, count, floor, points, duration = struct.unpack_from(wf.STEM_HEADER_FORMAT, raw)
    assert (magic, version, size, pps, count, floor, points) == (b"SBSW", 1, 20, 150, 4, wf.FLOOR_DB, w.points)
    assert duration == pytest.approx(3, abs=0.01)
    assert np.array_equal(np.frombuffer(raw, np.uint8, offset=size).reshape(4, points), w.stem_detail)
    assert wf.Waveform.from_bytes(w.to_bytes()).points == w.points  # the main file doesn't carry it
    assert wf.compute(track).stem_detail.shape[0] == 0


def test_the_stem_endpoint_says_no_stems_then_serves_them(server, tmp_path):
    url = f"{server.url}/api/tracks/{server.hash}/waveform/stems"
    assert get(url)[0] == 404
    assert get(f"{server.url}/api/tracks/{'0' * 40}/waveform/stems")[0] == 404

    folder = tmp_path / "separated"
    folder.mkdir()
    for name in wf.STEMS:
        write_wav(folder / f"{name}.wav", tone(200, 4, amp=0.5))
    lib = Library.open(tmp_path / "library.sqlite3")
    lib.put_artifact(server.hash, "stems", "test", folder)
    lib.close()

    status, _, body = get(url)
    assert status == 202 and json.loads(body)["job"]["kind"] == "waveform"
    deadline = time.monotonic() + 30
    while status != 200 and time.monotonic() < deadline:
        time.sleep(0.05)
        status, headers, raw = get(url)
    assert status == 200 and headers["Content-Type"] == "application/octet-stream"
    assert raw[:4] == b"SBSW" and len(raw) == 20 + 4 * 4 * 150
    assert get(url, **{"If-None-Match": headers["ETag"]})[0] == 304
    assert gzip.decompress(get(url, **{"Accept-Encoding": "gzip"})[2]) == raw
    assert wf.Waveform.from_bytes(gzip.decompress(get(f"{server.url}/api/tracks/{server.hash}/waveform", **{"Accept-Encoding": "gzip"})[2])).stems.shape[0] == 4
