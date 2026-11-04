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
from fastapi.testclient import TestClient

from musicreco import paths as mpaths
from seedbox2 import __version__, app, paths, shell
from seedbox2.server import DEFAULT_PORT, create_app, start_server
from seedbox2.server.routers import ROUTERS

ROOT = Path(__file__).resolve().parent.parent


def test_version_flag():
    out = subprocess.run([sys.executable, "-m", "seedbox2", "--version"], cwd=ROOT, capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == f"Seedbox 2 {__version__}"


def test_data_dirs_are_separate_from_seedbox1():
    dirs = app.data_dirs()
    assert set(dirs) == {"SEEDBOX_SUPPORT_DIR", "SEEDBOX_CACHE_DIR", "SEEDBOX_LOG_DIR"}
    assert all({"Seedbox 2", "seedbox2"} & set(p.parts) for p in dirs.values())


def test_apply_data_dirs_keeps_overrides():
    env = {"SEEDBOX_CACHE_DIR": "/somewhere/else"}
    app.apply_data_dirs(env)
    assert env["SEEDBOX_CACHE_DIR"] == "/somewhere/else"
    assert env["SEEDBOX_SUPPORT_DIR"] == str(app.data_dirs()["SEEDBOX_SUPPORT_DIR"])
    assert env["SEEDBOX_LOG_DIR"] == str(app.data_dirs()["SEEDBOX_LOG_DIR"])


# ---------------------------------------------------------------- folders, binaries and the window per OS (#37)


@pytest.mark.skipif(sys.platform == "win32", reason="platformdirs' macOS and XDG rules, computed on a POSIX home")
def test_data_dirs_follow_macos_and_xdg_conventions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    lib = tmp_path / "Library"
    assert paths.data_dirs("darwin") == {
        "SEEDBOX_SUPPORT_DIR": lib / "Application Support" / "Seedbox 2",
        "SEEDBOX_CACHE_DIR": lib / "Caches" / "Seedbox 2",
        "SEEDBOX_LOG_DIR": lib / "Logs" / "Seedbox 2",
    }
    assert paths.data_dirs("linux") == {
        "SEEDBOX_SUPPORT_DIR": tmp_path / ".local" / "share" / "seedbox2",
        "SEEDBOX_CACHE_DIR": tmp_path / ".cache" / "seedbox2",
        "SEEDBOX_LOG_DIR": tmp_path / ".local" / "state" / "seedbox2" / "log",
    }
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    assert paths.data_dirs("linux")["SEEDBOX_CACHE_DIR"] == tmp_path / "xdg-cache" / "seedbox2"
    assert paths.default_music_dir("darwin") == tmp_path / "Music" / "MusicReco"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows known folders")
def test_data_dirs_on_windows_live_in_local_appdata():
    local = Path(os.environ["LOCALAPPDATA"])
    dirs = paths.data_dirs()
    assert dirs["SEEDBOX_SUPPORT_DIR"] == local / "Seedbox 2"
    assert dirs["SEEDBOX_CACHE_DIR"] == local / "Seedbox 2" / "Cache"
    assert dirs["SEEDBOX_LOG_DIR"] == local / "Seedbox 2" / "Logs"
    assert paths.default_music_dir().name == "MusicReco"


def test_folders_take_the_environment_overrides(tmp_path):
    env = {"SEEDBOX_SUPPORT_DIR": str(tmp_path / "s"), "SEEDBOX_LOG_DIR": " ", "MUSICRECO_OUTPUT_ROOT": str(tmp_path / "m")}
    got = paths.folders(env)
    assert got["settings"] == tmp_path / "s" and got["music"] == tmp_path / "m"
    assert got["logs"] == paths.data_dirs()["SEEDBOX_LOG_DIR"]  # blank counts as unset
    assert got["cache"] == paths.data_dirs()["SEEDBOX_CACHE_DIR"]


def test_reveal_command_per_os(tmp_path):
    f = tmp_path / "set.json"
    f.write_text("{}")
    assert paths.reveal_command(tmp_path, "darwin") == ["open", str(tmp_path)]
    assert paths.reveal_command(f, "darwin") == ["open", "-R", str(f)]
    assert paths.reveal_command(tmp_path, "win32") == ["explorer", str(tmp_path)]
    assert paths.reveal_command(f, "win32") == ["explorer", f"/select,{f}"]
    assert paths.reveal_command(tmp_path, "linux") == ["xdg-open", str(tmp_path)]
    linux_file = paths.reveal_command(f, "linux")  # FileManager1 over gdbus selects the file, else its folder opens
    assert linux_file == ["xdg-open", str(tmp_path)] or (linux_file[0] == "gdbus" and f.as_uri() in linux_file[-2])
    assert [paths.reveal_label(p) for p in ("darwin", "win32", "linux")] == [
        "Show in Finder", "Show in Explorer", "Show in file manager"]


def test_reveal_endpoint_only_shows_seedbox2_folders(server, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(paths, "reveal", lambda p: shown.append(Path(p)))
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(tmp_path / "music"))

    def post(path: str) -> int:
        req = urllib.request.Request(f"{server.url}/api/system/reveal", json.dumps({"path": path}).encode(),
                                     {"content-type": "application/json"})  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    assert post(str(tmp_path / "cache")) == 200 and (tmp_path / "cache").is_dir()  # our folder: created
    assert post(str(tmp_path / "elsewhere")) == 403
    assert post(str(tmp_path / "cache" / ".." / "..")) == 403
    assert shown == [tmp_path / "cache"]
    monkeypatch.setattr(paths, "reveal", lambda p: (_ for _ in ()).throw(FileNotFoundError(str(p))))
    assert post(str(tmp_path / "music" / "crate")) == 404


def test_system_lists_the_folders(server, tmp_path, monkeypatch):
    monkeypatch.setenv("SEEDBOX_SUPPORT_DIR", str(tmp_path / "support"))
    info = json.loads(_get(f"{server.url}/api/system")[1])
    assert set(info["folders"]) == {"settings", "cache", "logs", "music"}
    assert info["folders"]["settings"] == str(tmp_path / "support")
    assert info["reveal_label"] == paths.reveal_label()


def test_gui_backend_per_os():
    assert shell.gui_backend("darwin") == "cocoa"
    assert shell.gui_backend("win32") == "edgechromium"
    assert shell.gui_backend("linux", {}) == "gtk"
    assert shell.gui_backend("linux", {"KDE_FULL_SESSION": "true"}) == "qt"
    assert shell._colorref("#0b0d10") == 0x100D0B


def test_ffmpeg_lookup_needs_no_homebrew(tmp_path, monkeypatch):
    assert mpaths.exe_name("ffmpeg", "win32") == "ffmpeg.exe"
    assert mpaths.exe_name("ffmpeg.exe", "win32") == "ffmpeg.exe"
    assert mpaths.exe_name("ffmpeg", "darwin") == "ffmpeg"
    mac = mpaths.extra_dirs("darwin", {})
    assert Path("/opt/homebrew/bin") in mac and Path("/usr/local/bin") in mac  # Apple silicon and Intel Homebrew
    win = mpaths.extra_dirs("win32", {"LOCALAPPDATA": str(tmp_path), "ProgramFiles": str(tmp_path / "pf")})
    assert tmp_path / "Microsoft" / "WinGet" / "Links" in win and tmp_path / "pf" / "ffmpeg" / "bin" in win
    assert not any("homebrew" in str(d) for d in win + mpaths.extra_dirs("linux", {}))
    # a binary in $SEEDBOX_BIN_DIR wins, with this OS's file name
    exe = tmp_path / mpaths.exe_name("ffmpeg")
    exe.write_text("")
    exe.chmod(0o755)
    monkeypatch.setenv(mpaths.BIN_DIR_ENV, str(tmp_path))
    assert mpaths.ffmpeg_path() == str(exe)


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


def test_without_a_build_the_root_says_how_to_build_it():
    with TestClient(create_app(None, start_jobs=False)) as client:
        r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "deno task build" in r.text
    with TestClient(create_app(None, dev=True, start_jobs=False)) as client:
        assert client.get("/").status_code == 404  # --dev: the window loads Vite, not this server


def _fake_deno(tmp_path, monkeypatch, exit_code=0):
    """shell.build_frontend's deno: records its calls, and `task build` writes dist/index.html."""
    calls = []

    def run(cmd, cwd, **_kw):
        calls.append(cmd[1:])
        if cmd[1:] == ["task", "build"] and exit_code == 0:
            (Path(cwd) / "dist").mkdir(parents=True)
            (Path(cwd) / "dist" / "index.html").write_text(INDEX)
        return subprocess.CompletedProcess(cmd, exit_code, "out", "the error")

    (tmp_path / "frontend2").mkdir()
    monkeypatch.delenv(shell.STATIC_DIR_ENV, raising=False)
    monkeypatch.setattr(shell, "project_root", lambda: tmp_path)
    monkeypatch.setattr(mpaths, "deno_path", lambda: "/bin/deno")
    monkeypatch.setattr(shell.subprocess, "run", run)
    return calls


def test_a_fresh_clone_builds_the_frontend(tmp_path, monkeypatch):
    calls = _fake_deno(tmp_path, monkeypatch)
    assert shell.build_frontend() == tmp_path / "frontend2" / "dist"
    assert calls == [["install"], ["task", "build"]]


def test_a_failed_build_starts_without_a_frontend(tmp_path, monkeypatch, caplog):
    calls = _fake_deno(tmp_path, monkeypatch, exit_code=1)
    assert shell.build_frontend() is None
    assert calls == [["install"]] and "the error" in caplog.text


def test_no_build_without_deno_or_with_a_chosen_folder(tmp_path, monkeypatch):
    calls = _fake_deno(tmp_path, monkeypatch)
    monkeypatch.setattr(mpaths, "deno_path", lambda: None)
    assert shell.build_frontend() is None
    monkeypatch.setattr(mpaths, "deno_path", lambda: "/bin/deno")
    monkeypatch.setenv(shell.STATIC_DIR_ENV, str(tmp_path / "elsewhere"))
    assert shell.build_frontend() is None
    assert calls == []


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
