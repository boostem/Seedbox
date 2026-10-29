"""seedbox2: `python -m seedbox2` starts its own server and UI and keeps its data apart from Seedbox 1's."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from seedbox2 import __version__, app, shell
from seedbox2.server import DEFAULT_PORT, start_server
from seedbox2.server.routers import ROUTERS

ROOT = Path(__file__).resolve().parent.parent


def test_version_flag():
    out = subprocess.run([sys.executable, "-m", "seedbox2", "--version"], cwd=ROOT, capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == f"Seedbox 2 {__version__}"


def test_data_dirs_are_separate_from_seedbox1(tmp_path):
    dirs = app.data_dirs(tmp_path)
    assert set(dirs) == {"SEEDBOX_SUPPORT_DIR", "SEEDBOX_CACHE_DIR", "SEEDBOX_LOG_DIR"}
    assert all(p.name == "Seedbox 2" for p in dirs.values())


def test_apply_data_dirs_keeps_overrides():
    env = {"SEEDBOX_CACHE_DIR": "/somewhere/else"}
    app.apply_data_dirs(env)
    assert env["SEEDBOX_CACHE_DIR"] == "/somewhere/else"
    assert env["SEEDBOX_SUPPORT_DIR"].endswith("Seedbox 2")
    assert env["SEEDBOX_LOG_DIR"].endswith("Seedbox 2")


# ---------------------------------------------------------------- server and shell (#36)

INDEX = "<!doctype html><title>seedbox2 test</title>"


def _get(url: str, timeout: float = 5.0) -> tuple[int, str, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - our own localhost server
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(INDEX)
    (d / "assets" / "app-123.js").write_text("console.log(1)")
    return d


@pytest.fixture
def server(dist):
    handle = start_server(dist)
    yield handle
    handle.stop()


def test_server_starts_and_health_answers(server):
    assert server.running and server.port > 0
    assert _get(f"{server.url}/api/health")[:2] == (200, '{"ok":true}')


def test_system_describes_seedbox2(server, dist):
    status, body, _ = _get(f"{server.url}/api/system")
    info = json.loads(body)
    assert status == 200
    assert info["app"] == "Seedbox 2" and info["version"] == __version__
    assert info["frontend"] == str(dist)
    assert {"python", "platform", "machine", "ffmpeg", "deno"} <= set(info)


def test_a_router_per_area():
    prefixes = {r.prefix for r in ROUTERS}
    assert {"/library", "/analysis", "/sets", "/mixes", "/jobs"} <= prefixes
    paths = {route.path for r in ROUTERS for route in r.routes}
    assert {"/health", "/system", "/events"} <= paths


def test_serves_the_frontend_and_json_404s_under_api(server, dist):
    assert _get(f"{server.url}/")[:2] == (200, INDEX)
    assert _get(f"{server.url}/some/client/route")[:2] == (200, INDEX)  # SPA fallback
    status, _, headers = _get(f"{server.url}/assets/app-123.js")
    assert status == 200 and "immutable" in headers["cache-control"]
    status, body, _ = _get(f"{server.url}/api/nope")
    assert status == 404 and json.loads(body) == {"detail": "Not Found"}
    (dist.parent / "secret.txt").write_text("secret")
    assert _get(f"{server.url}/..%2Fsecret.txt")[:2] == (200, INDEX)  # never a file outside dist


def test_event_stream_delivers_published_events(server):
    with urllib.request.urlopen(f"{server.url}/api/events", timeout=5) as resp:  # noqa: S310
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.readline().startswith(b"retry:")  # subscribed once the first chunk arrives
        server.events.publish("jobs.updated", {"n": 1})
        lines = [resp.readline() for _ in range(4)]
    assert b'data: {"type":"jobs.updated","data":{"n":1}}\n' in lines


def test_dev_port_is_8766_next_to_seedbox1():
    assert DEFAULT_PORT == 8766
    assert "http://127.0.0.1:8766" in (ROOT / "frontend2" / "vite.config.ts").read_text()
    assert shell.DEV_URL == "http://localhost:5174"  # Seedbox 1's Vite has 5173
    assert "http://127.0.0.1:8765" in (ROOT / "frontend" / "vite.config.ts").read_text()


def test_static_dir_defaults_to_frontend2_dist(tmp_path, monkeypatch):
    monkeypatch.delenv(shell.STATIC_DIR_ENV, raising=False)
    monkeypatch.setattr(shell, "project_root", lambda: tmp_path)
    assert shell.static_dir() is None
    (tmp_path / "frontend2" / "dist").mkdir(parents=True)
    (tmp_path / "frontend2" / "dist" / "index.html").write_text(INDEX)
    assert shell.static_dir() == tmp_path / "frontend2" / "dist"


def test_does_not_import_seedbox1():
    code = ("import sys, seedbox2.app, seedbox2.shell, seedbox2.server; "
            "bad = [m for m in sys.modules if m == 'seedbox' or m.startswith('seedbox.')]; assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_only_serves_frontend2_dist(tmp_path, dist):
    port = _free_port()
    env = {**os.environ, "HOME": str(tmp_path), shell.STATIC_DIR_ENV: str(dist),
           **{var: str(tmp_path / var) for var in app.data_dirs()}}
    proc = subprocess.Popen([sys.executable, "-m", "seedbox2", "--server-only", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # fmt: skip
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                if _get(f"http://127.0.0.1:{port}/api/health", timeout=1)[0] == 200:
                    break
            except OSError:
                pass
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "the server didn't answer /api/health"
            time.sleep(0.1)
        assert _get(f"http://127.0.0.1:{port}/")[:2] == (200, INDEX)
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0, out
    assert f"API on http://127.0.0.1:{port}" in out
    assert (tmp_path / "SEEDBOX_LOG_DIR" / "seedbox2.log").is_file()
