"""seedbox2.mix.stream: a window decodes to the same samples as a whole file, and chunking doesn't change a stage's
output, nor grow what it keeps with the length of the audio."""

from __future__ import annotations

import subprocess
import tracemalloc
from pathlib import Path

import numpy as np
import pytest
from scipy import ndimage
from scipy.io import wavfile

from musicreco import mixrender
from musicreco.paths import ffmpeg_path
from seedbox2.mix import stream

pytestmark = pytest.mark.skipif(ffmpeg_path() is None, reason="needs ffmpeg")

SR = 44100
ENCODERS = {
    "cbr.mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    "vbr.mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "no-header.mp3": ["-c:a", "libmp3lame", "-q:a", "4", "-write_xing", "0"],
    "a.flac": ["-c:a", "flac"],
    "a.wav": ["-c:a", "pcm_s16le"],
    "a48.wav": ["-c:a", "pcm_s24le", "-ar", "48000"],
    "a.aiff": ["-c:a", "pcm_s16be"],
    "a.m4a": ["-c:a", "aac", "-b:a", "192k"],
    "a.ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    "a.opus": ["-c:a", "libopus", "-b:a", "128k"],
}


def noise(seconds: float, seed: int = 1) -> np.ndarray:
    """Coloured stereo noise with an envelope, so that a sample offset can't go unnoticed."""
    n = int(seconds * SR)
    rng = np.random.default_rng(seed)
    x = ndimage.uniform_filter1d(rng.standard_normal((2, n)), 8, axis=-1) * 0.8
    return (x * (0.4 + 0.4 * np.sin(2 * np.pi * np.arange(n) / SR * 3.3))).astype(np.float32)


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("stream") / "source.wav"
    wavfile.write(p, SR, noise(30.0).T)
    return p


@pytest.fixture(scope="module", params=ENCODERS)
def encoded(request: pytest.FixtureRequest, source: Path) -> Path:
    out = source.parent / request.param
    if not out.exists():
        cmd = [ffmpeg_path(), "-nostdin", "-loglevel", "error", "-y", "-i", str(source), *ENCODERS[request.param], str(out)]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode:
            pytest.skip(f"this ffmpeg can't write {request.param}")
    return out


def test_window_is_the_same_samples_as_the_whole_file(encoded: Path):
    whole = mixrender.decode_stereo(encoded)
    for start in (0.0, 0.2, 7.77, 13.37, 24.0):
        w = stream.decode_window(encoded, start, 3.0)
        i = round(start * SR)
        assert w.dtype == np.float32
        assert w.shape == (2, 3 * SR)
        np.testing.assert_array_equal(w, whole[:, i : i + 3 * SR], err_msg=f"{encoded.name} at {start} s")


def test_window_outside_the_file_is_silence(source: Path):
    whole = mixrender.decode_stereo(source)
    before = stream.decode_window(source, -1.0, 2.0)
    np.testing.assert_array_equal(before[:, :SR], 0.0)
    np.testing.assert_array_equal(before[:, SR:], whole[:, :SR])
    after = stream.decode_window(source, 29.0, 2.0)
    np.testing.assert_array_equal(after[:, :SR], whole[:, -SR:])
    np.testing.assert_array_equal(after[:, SR:], 0.0)
    np.testing.assert_array_equal(stream.decode_window(source, 40.0, 1.0), 0.0)
    np.testing.assert_array_equal(stream.decode_window(source, -5.0, 1.0), 0.0)


def test_window_of_a_bad_file_says_so(tmp_path: Path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio at all" * 100)
    with pytest.raises(ValueError, match="could not decode bad.mp3"):
        stream.decode_window(bad, 1.0, 1.0)
    with pytest.raises(ValueError, match="could not decode"):
        stream.decode_window(tmp_path / "missing.wav", 0.0, 1.0)


def test_iter_decode_is_the_whole_file(source: Path):
    chunks = list(stream.iter_decode(source, chunk=5000))
    assert all(c.shape[1] == 5000 for c in chunks[:-1])
    np.testing.assert_array_equal(np.concatenate(chunks, axis=1), mixrender.decode_stereo(source))
    next(stream.iter_decode(source)).shape  # abandoned half way: nothing hangs or raises


def test_track_loudness_matches_measuring_it_whole(source: Path):
    whole = mixrender.decode_stereo(source)
    assert stream.measure_track(source) == pytest.approx(mixrender.integrated_lufs(whole[:, ::2], SR // 2), abs=1e-6)


@pytest.mark.parametrize("seconds", [0.2, 0.4, 0.45, 3.0])
@pytest.mark.parametrize("chunk", [1000, 4410, stream.CHUNK])
def test_loudness_and_peak_in_chunks_match_seedbox1(seconds: float, chunk: int):
    x = noise(seconds)
    meter = stream.Loudness(SR)
    for a in range(0, x.shape[1], chunk):
        meter.feed(x[:, a : a + chunk])
    assert meter.integrated() == pytest.approx(mixrender.integrated_lufs(x, SR), abs=1e-6)
    assert meter.ungated() == pytest.approx(mixrender._range_lufs(x, SR), abs=1e-6)
    assert stream.true_peak_db(x) == pytest.approx(mixrender.true_peak_db(x), abs=1e-3)


def _ctl(points: list[float], n: int) -> np.ndarray:
    return np.interp(np.arange(0, n + 16, 16), np.linspace(0, n, len(points)), points)


def _lane(points: list[float], n: int, smooth: float) -> stream.Lane:
    return stream.Lane(_ctl(points, n), 16, smooth, SR)


def _chunked(x: np.ndarray, chunk: int, fn) -> np.ndarray:
    return np.concatenate([fn(x[:, a : a + chunk], a) for a in range(0, x.shape[1], chunk)], axis=1)


SIZES = [stream.CHUNK, 4096, 1000, 777]


@pytest.mark.parametrize("sweep", [[0, 0, -1, -1, 0], [0, 0, 0.6, 0.002, 0.0, 0.0, 1, -1], [0.5, 0.5]])
@pytest.mark.parametrize("chunk", SIZES)
def test_filter_in_chunks_is_seedbox1s(sweep: list[float], chunk: int):
    x = noise(6.0)
    n = x.shape[1]
    f, res = _lane(sweep, n, 0.03), _lane([0.0, 1.0], n, 0.03)
    want = mixrender._dj_filter(x, f.at(0, n), SR, res.at(0, n))
    dj = stream.DjFilter(f, res, n, SR)
    np.testing.assert_allclose(_chunked(x, chunk, dj), want, atol=1e-6)


def test_filter_without_scipys_private_call_is_the_same(monkeypatch: pytest.MonkeyPatch):
    x = noise(2.0)
    n = x.shape[1]
    f = _lane([0.0, -1.0, 0.8], n, 0.03)
    fast = stream.DjFilter(f, None, n, SR)(x, 0)

    def public(sos, x, zi):
        y, z = stream.signal.sosfilt(sos, x, axis=-1, zi=np.moveaxis(zi, 1, 0))
        x[:] = y
        zi[:] = np.moveaxis(z, 0, 1)

    monkeypatch.setattr(stream, "_sos_in_place", public)
    np.testing.assert_array_equal(stream.DjFilter(f, None, n, SR)(x, 0), fast)


@pytest.mark.parametrize("chunk", SIZES)
def test_isolator_in_chunks_is_seedbox1s(chunk: int):
    x = noise(4.0)
    n = x.shape[1]
    gains = [stream.iso_gain(_lane(p, n, 0.01).at(0, n)) for p in ([0, -30, 0], [0, 0, -12], [-6, 3, -30])]
    iso = stream.Isolator(SR)
    y = _chunked(x, chunk, lambda c, a: iso(c, *(g[a : a + c.shape[1]] for g in gains)))
    np.testing.assert_allclose(y, mixrender._isolator(x, *gains, SR), atol=2e-6)


@pytest.mark.parametrize("chunk", SIZES)
def test_echo_in_chunks_is_seedbox1s(chunk: int):
    x = noise(6.0)
    x[:, 2 * SR :] = 0.0  # the tail rings on
    n = x.shape[1]
    delay = np.linspace(0.375 * SR, 0.36 * SR, n)
    fb = np.linspace(0.7, 0.5, n, dtype=np.float32)
    want = mixrender._echo(x, delay, fb, SR)
    echo = stream.Echo(float(delay.max()), SR)
    y = _chunked(x, chunk, lambda c, a: echo(c, delay[a : a + c.shape[1]], fb[a : a + c.shape[1]]))
    np.testing.assert_allclose(y, want, atol=1e-5)


@pytest.mark.parametrize("chunk", SIZES)
def test_reverb_in_chunks_is_seedbox1s(chunk: int):
    x = noise(4.0)  # sound from the first sample: its settings mustn't ramp in
    want = mixrender._reverb(x, SR, 0.5)
    rv = stream.Reverb(0.5, SR)
    np.testing.assert_allclose(_chunked(x, chunk, lambda c, a: rv(c)), want, atol=1e-4)


def test_reverb_keeps_its_tail_when_a_later_chunk_is_bigger():
    x = noise(2.0)
    want = mixrender._reverb(x, SR, 0.5)
    rv = stream.Reverb(0.5, SR)
    y = np.concatenate([rv(x[:, :500]), rv(x[:, 500:20000]), rv(x[:, 20000:])], axis=1)
    np.testing.assert_allclose(y, want, atol=1e-4)


def test_leveller_in_chunks_is_seedbox1s():
    x = noise(8.0) * 3
    ref = 10 ** ((mixrender.DECK_LUFS + 0.691) / 10)
    want, red = mixrender._level(x, ref, SR)
    lev = stream.Leveller(ref, mixrender.LEVEL_THRESHOLD_DB, mixrender.LEVEL_RATIO, mixrender.MAKEUP_DB, SR)
    for a in range(0, x.shape[1], 1000):
        lev.feed(x[:, a : a + 1000])
    assert lev.finish() == pytest.approx(red, abs=1e-6)
    np.testing.assert_allclose(lev.gain(0, x.shape[1]), want, atol=1e-6)


@pytest.mark.parametrize("level", [0.2, 4.0], ids=["under the ceiling", "over it"])
def test_limiter_in_chunks_is_seedbox1s(level: float):
    x = noise(6.0) * level
    want, red = mixrender._limit(x, mixrender.CEILING_DBTP)
    y = x.copy()
    assert stream.limit(y, mixrender.CEILING_DBTP, SR) == pytest.approx(red, abs=1e-3)
    np.testing.assert_allclose(y, want, atol=2e-5)


def _peak(fn) -> float:
    """What `fn` allocates at its peak, in MB."""
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1] / 2**20
    finally:
        tracemalloc.stop()


def test_stages_keep_a_chunk_not_the_audio():
    """60 s of stereo is 21 MB in float32 and 42 MB in float64: no stage may come near a copy of it."""
    x = noise(60.0)
    n = x.shape[1]
    f = _lane([0.0, -1.0, 0.0, 1.0], n, 0.03)
    gain = np.float32(1.0)
    fb = np.full(stream.CHUNK, 0.6, np.float32)
    delay = np.full(stream.CHUNK, 0.4 * SR)
    meter_ref = 10 ** ((mixrender.DECK_LUFS + 0.691) / 10)

    def run(stage) -> None:
        for a in range(0, n - stream.CHUNK, stream.CHUNK):
            stage(x[:, a : a + stream.CHUNK], a)

    iso, dj = stream.Isolator(SR), stream.DjFilter(f, None, n, SR)
    echo, rv = stream.Echo(0.4 * SR, SR), stream.Reverb(0.5, SR)
    lev, meter = stream.Leveller(meter_ref, 1.0, 3.0, 1.0, SR), stream.Loudness(SR)
    stages = {
        "isolator": lambda c, a: iso(c, gain, gain, gain),
        "filter": lambda c, a: dj(c, a),
        "echo": lambda c, a: echo(c, delay, fb),
        "reverb": lambda c, a: rv(c),
        "leveller": lambda c, a: lev.feed(c),
        "loudness": lambda c, a: meter.feed(c),
    }
    for name, stage in stages.items():
        assert _peak(lambda: run(stage)) < 4.0, name
    assert _peak(lambda: stream.true_peak_db(x)) < 4.0
    y = x * 4
    assert _peak(lambda: stream.limit(y, mixrender.CEILING_DBTP, SR)) < 15.0  # float32 gain per sample, 10.5 MB


def test_float32_in_float32_out():
    x = noise(1.0)
    n = x.shape[1]
    assert stream.Isolator(SR)(x, 1.0, 1.0, 1.0).dtype == np.float32
    assert stream.DjFilter(_lane([0.0, -1.0], n, 0.03), None, n, SR)(x, 0).dtype == np.float32
    assert stream.Echo(1000.0, SR)(x, np.full(n, 1000.0), np.full(n, 0.5, np.float32)).dtype == np.float32
    assert stream.Reverb(0.5, SR)(x).dtype == np.float32
    assert stream.iso_gain(np.zeros(4, np.float32)).dtype == np.float32
    assert _lane([0.0, 1.0], n, 0.01).at(0, 100).dtype == np.float32
