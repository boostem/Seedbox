"""`/api/tracks/{hash}/audio`: the chunks the live engine (#65) streams line up with a whole decode, sample for sample."""

from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy.io import wavfile

from seedbox2.analysis import stems as st
from seedbox2.server.app import create_app
from seedbox2.server.routers.tracks import CHUNK_FRAMES, CHUNK_SAMPLE_RATE

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

SECONDS = 45


def _decode(data: bytes) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "2", "pipe:1"], input=data,
                       capture_output=True, check=True)  # fmt: skip
    return np.frombuffer(r.stdout, "<f4").reshape(-1, 2)


def _whole(path) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", "44100", "pipe:1"],
                       capture_output=True, check=True)  # fmt: skip
    return np.frombuffer(r.stdout, "<f4").reshape(-1, 2)


def _song(sr: int = CHUNK_SAMPLE_RATE) -> np.ndarray:
    t = np.arange(SECONDS * sr) / sr
    return np.stack([0.3 * np.sin(2 * np.pi * (220 + 8 * t) * t), 0.3 * np.sin(2 * np.pi * (330 + 5 * t) * t)]).T


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SEEDBOX_SUPPORT_DIR", str(tmp_path / "support"))
    app = create_app(library_path=tmp_path / "support" / "library.sqlite3")
    with TestClient(app) as client:
        client.get("/api/analysis/stems")  # opens the library
        yield client, app.state.library


def _add(lib, path) -> str:
    return lib.add_file(path)["hash"]


@pytest.mark.parametrize("ext, args", [
    ("flac", []), ("mp3", ["-b:a", "256k"]), ("m4a", ["-b:a", "256k"]), ("opus", ["-b:a", "192k"]),
])  # fmt: skip
def test_chunks_are_a_whole_decode_cut_up(api, tmp_path, ext, args):
    client, lib = api
    wav = tmp_path / "song.wav"
    wavfile.write(wav, CHUNK_SAMPLE_RATE, _song().astype(np.float32))
    path = tmp_path / f"song.{ext}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), *args, str(path)], check=True)
    h = _add(lib, path)

    info = client.get(f"/api/tracks/{h}/audio").json()
    assert info["chunk_frames"] == CHUNK_FRAMES and info["sample_rate"] == 44100 and info["sources"] == ["mix"]
    assert abs(info["frames"] - SECONDS * CHUNK_SAMPLE_RATE) < CHUNK_SAMPLE_RATE // 2
    whole = _whole(path)
    chunks = []
    for i in range(3):
        r = client.get(f"/api/tracks/{h}/audio/mix/{i}")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/flac"
        chunks.append(_decode(r.content))
    assert [len(c) for c in chunks[:2]] == [CHUNK_FRAMES, CHUNK_FRAMES]
    got = np.concatenate(chunks)
    assert np.max(np.abs(got[:len(whole)] - whole[:len(got)])) < 1e-3  # 24-bit FLAC of the same samples
    assert client.get(f"/api/tracks/{h}/audio/mix/3").status_code == 404
    assert client.get(f"/api/tracks/{h}/audio/mix/-1").status_code == 404


def test_another_sample_rate_arrives_at_44100(api, tmp_path):
    client, lib = api
    path = tmp_path / "song.flac"
    wav = tmp_path / "song.wav"
    wavfile.write(wav, 48000, _song(48000).astype(np.float32))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), str(path)], check=True)
    h = _add(lib, path)
    whole = _whole(path)
    got = np.concatenate([_decode(client.get(f"/api/tracks/{h}/audio/mix/{i}").content) for i in range(2)])
    assert len(got) == 2 * CHUNK_FRAMES
    assert np.max(np.abs(got - whole[: len(got)])) < 1e-3


def test_stems_are_sources_once_separated_and_seek_exactly(api, tmp_path):
    client, lib = api
    path = tmp_path / "song.wav"
    wavfile.write(path, CHUNK_SAMPLE_RATE, _song().astype(np.float32))
    h = _add(lib, path)
    assert client.get(f"/api/tracks/{h}/audio").json()["sources"] == ["mix"]
    assert client.get(f"/api/tracks/{h}/audio/vocals/0").status_code == 404
    assert client.get("/api/tracks/nope/audio").status_code == 404

    folder = st.folder_for(h)
    folder.mkdir(parents=True)
    for i, stem in enumerate(st.STEMS):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-af", f"volume={0.2 + i * 0.1}",
                        str(folder / f"{stem}.flac")], check=True)  # fmt: skip
    lib.put_artifact(h, st.ARTIFACT_KIND, "htdemucs:test", folder)
    assert client.get(f"/api/tracks/{h}/audio").json()["sources"] == ["mix", *sorted(st.STEMS)]
    whole = _whole(folder / "vocals.flac")
    got = _decode(client.get(f"/api/tracks/{h}/audio/vocals/1").content)
    assert len(got) == CHUNK_FRAMES
    assert np.max(np.abs(got - whole[CHUNK_FRAMES : 2 * CHUNK_FRAMES])) < 1e-3
