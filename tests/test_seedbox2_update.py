"""Seedbox 2's self-update (seedbox2/update.py), its yt-dlp updater (seedbox2/ytdlp.py) and "Report a problem"
(seedbox2/support.py), #89. Nothing here touches the network: GitHub and PyPI are stand-ins."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seedbox2 import support, update, ytdlp
from seedbox2.library.service import Settings
from seedbox2.update import AppUpdater, UpdateError

DMG = "-macos-arm64.dmg"
EXE = "-windows-x64-setup.exe"


def release(version: str, *, assets: list[str] = (), prerelease: bool = False, draft: bool = False,
            tag_prefix: str = update.TAG_PREFIX, digest: dict | None = None) -> dict:  # fmt: skip
    return {"tag_name": f"{tag_prefix}{version}", "name": f"Seedbox 2 {version}", "draft": draft,
            "prerelease": prerelease, "html_url": f"https://github.com/o/r/releases/tag/{tag_prefix}{version}",
            "body": "notes", "published_at": "2026-11-01T00:00:00Z",
            "assets": [{"name": a, "size": 10, "browser_download_url": f"https://dl/{a}",
                        "digest": (digest or {}).get(a)} for a in assets]}  # fmt: skip


def installer(version: str, suffix: str = DMG) -> str:
    return f"Seedbox-2-{version}{suffix}"


# ---------------------------------------------------------------- picking a release

def test_pick_release_takes_the_newest_seedbox2_release_with_this_computers_installer():
    releases = [
        release("2.0.1", assets=[installer("2.0.1"), update.SUMS_ASSET]),
        release("2.2.0", assets=[installer("2.2.0", EXE)]),  # nothing for this Mac yet
        release("2.1.0", assets=[installer("2.1.0"), update.SUMS_ASSET]),
        release("2.3.0", assets=[installer("2.3.0")], draft=True),
        release("1.9.0", assets=[installer("9.0.0")], tag_prefix="v"),  # Seedbox 1's releases
        release("2.4.0rc1", assets=[installer("2.4.0rc1")], prerelease=True),
    ]
    got = update.pick_release(releases, "2.0.0", DMG)
    assert got["version"] == "2.1.0"
    assert got["asset"]["name"] == installer("2.1.0")
    assert got["sums"] == f"https://dl/{update.SUMS_ASSET}"
    assert update.pick_release(releases, "2.1.0", DMG) is None
    assert update.pick_release(releases, "2.0.0", EXE)["version"] == "2.2.0"


def test_pre_releases_only_count_for_a_pre_release_build():
    releases = [release("2.1.0rc1", assets=[installer("2.1.0rc1")], prerelease=True)]
    assert update.pick_release(releases, "2.0.0", DMG) is None
    assert update.pick_release(releases, "2.0.0.dev0", DMG)["version"] == "2.1.0rc1"


def test_without_an_installer_for_this_platform_any_newer_release_is_linked():
    got = update.pick_release([release("2.1.0", assets=[installer("2.1.0")])], "2.0.0", None)
    assert got["version"] == "2.1.0" and got["asset"] is None


@pytest.mark.parametrize(("platform", "machine", "suffix"), [
    ("darwin", "arm64", "-macos-arm64.dmg"), ("darwin", "x86_64", "-macos-x86_64.dmg"),
    ("win32", "AMD64", "-windows-x64-setup.exe"), ("win32", "ARM64", None), ("linux", "x86_64", None),
])  # fmt: skip
def test_asset_suffix_matches_the_installer_names(platform, machine, suffix):
    assert update.asset_suffix(platform, machine) == suffix


def test_the_checksum_comes_from_sha256sums_else_from_githubs_digest():
    sha = "ab" * 32
    rel = update.pick_release([release("2.1.0", assets=[installer("2.1.0"), update.SUMS_ASSET])], "2.0.0", DMG)
    sums = f"{'cd' * 32}  other.exe\n{sha} *{installer('2.1.0')}\n".encode()
    assert update.expected_sha256(rel, lambda url: sums) == sha
    with pytest.raises(UpdateError, match="no line"):
        update.expected_sha256(rel, lambda url: b"")
    rel = update.pick_release([release("2.1.0", assets=[installer("2.1.0")],
                                       digest={installer("2.1.0"): f"sha256:{sha}"})], "2.0.0", DMG)  # fmt: skip
    assert update.expected_sha256(rel, lambda url: b"") == sha
    rel["asset"]["digest"] = None
    with pytest.raises(UpdateError, match="can't be verified"):
        update.expected_sha256(rel, lambda url: b"")


# ---------------------------------------------------------------- check and download

class FakeGitHub:
    """GitHub's releases API and downloads, from a dict of URL -> bytes."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[str] = []

    def __call__(self, url: str, accept: str) -> io.BytesIO:
        self.asked.append(url)
        if url not in self.files:
            raise UpdateError(f"could not reach {url}")
        return io.BytesIO(self.files[url])


def github_with(version: str, body: bytes, *, sha: str | None = None) -> FakeGitHub:
    name = installer(version)
    sums = f"{sha or hashlib.sha256(body).hexdigest()}  {name}\n".encode()
    rels = [release(version, assets=[name, update.SUMS_ASSET])]
    return FakeGitHub({update.RELEASES_URL.format(repo="o/r"): json.dumps(rels).encode(),
                       f"https://dl/{name}": body, f"https://dl/{update.SUMS_ASSET}": sums})  # fmt: skip


def make_updater(tmp_path: Path, gh: FakeGitHub, **kw) -> AppUpdater:
    opts = {"current": "2.0.0", "platform": "darwin", "machine": "arm64", "installable": True, "repo": "o/r",
            "executable": "/Applications/Seedbox 2.app/Contents/MacOS/Seedbox 2", **kw}  # fmt: skip
    return AppUpdater(Settings(tmp_path / "settings.json"), cache_dir=tmp_path / "cache", open_url=gh, **opts)


def test_check_downloads_and_verifies_the_installer(tmp_path):
    body = b"a new dmg" * 1000
    events = []
    up = make_updater(tmp_path, github_with("2.1.0", body), publish=lambda *a, **k: events.append(a[1]["state"]))
    status = up.check()
    assert status["state"] == "ready", status
    assert status["latest"]["version"] == "2.1.0"
    assert (up.folder / installer("2.1.0")).read_bytes() == body
    assert "downloading" in events and events[-1] == "ready"
    ready = json.loads((up.folder / update.READY_FILE).read_text())
    assert ready["sha256"] == hashlib.sha256(body).hexdigest()

    again = make_updater(tmp_path, FakeGitHub({}))  # the next launch: still ready, no network needed
    assert again.status()["state"] == "ready"
    assert again.check()["state"] == "ready"


def test_a_download_that_doesnt_match_its_checksum_is_deleted(tmp_path):
    up = make_updater(tmp_path, github_with("2.1.0", b"tampered", sha="0" * 64))
    status = up.check()
    assert status["state"] == "error" and "checksum" in status["error"]
    assert not any(up.folder.iterdir())


def test_up_to_date_and_offline(tmp_path):
    assert make_updater(tmp_path, github_with("2.0.0", b"x")).check()["state"] == "up_to_date"
    status = make_updater(tmp_path, FakeGitHub({})).check()
    assert status["state"] == "error" and "could not reach" in status["error"]


def test_from_source_a_new_release_is_only_linked(tmp_path):
    gh = github_with("2.1.0", b"dmg")
    status = make_updater(tmp_path, gh, installable=False).check()
    assert status["state"] == "available" and not status["installable"] and status["reason"]
    assert gh.asked == [update.RELEASES_URL.format(repo="o/r")]  # nothing downloaded


def test_the_check_at_startup_is_opt_out(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "STARTUP_DELAY_S", 0.0)
    up = make_updater(tmp_path, github_with("2.1.0", b"dmg"))
    assert up.enabled
    up.set_enabled(False)
    assert json.loads((tmp_path / "settings.json").read_text())["updates"] == {"check": False}
    up.start()
    assert up._thread is None
    up.set_enabled(True)
    deadline = time.monotonic() + 5
    while up.status()["state"] != "ready" and time.monotonic() < deadline:
        time.sleep(0.02)
    up.close()
    assert up.status()["state"] == "ready"


def test_an_installed_update_is_tidied_up_and_a_failed_one_reported(tmp_path):
    up = make_updater(tmp_path, github_with("2.1.0", b"dmg"))
    up.check()
    (up.folder / update.RESULT_FILE).write_text("failed: could not move the app aside\n")
    failed = make_updater(tmp_path, FakeGitHub({}))
    assert failed.status()["state"] == "ready"
    assert "could not move the app aside" in failed.status()["error"]
    installed = make_updater(tmp_path, FakeGitHub({}), current="2.1.0")
    assert installed.status()["state"] == "idle"
    assert not up.folder.exists()


# ---------------------------------------------------------------- install

def test_install_runs_the_helper_at_exit_only_when_ready(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(update, "_spawn_detached", lambda cmd, log: spawned.append(cmd))
    up = make_updater(tmp_path, github_with("2.1.0", b"dmg"))
    assert up.install_if_ready() is False
    with pytest.raises(UpdateError):
        up.request_install()
    up.check()
    up.request_install(relaunch=True)
    assert up.install_if_ready() is True
    cmd = spawned[-1]
    assert cmd[:2] == ["/bin/bash", str(up.folder / "install.sh")]
    assert cmd[2:] == [str(os.getpid()), str(up.folder / installer("2.1.0")), "/Applications/Seedbox 2.app", "1",
                       str(up.folder / update.RESULT_FILE)]  # fmt: skip

    quit_only = make_updater(tmp_path, FakeGitHub({}))  # quitting with an update ready installs it, no restart
    assert quit_only.install_if_ready() is True and spawned[-1][5] == "0"

    (up.folder / installer("2.1.0")).write_bytes(b"changed")  # never install what doesn't match any more
    assert make_updater(tmp_path, FakeGitHub({})).install_if_ready() is False


def test_the_windows_helper_runs_the_setup_silently(tmp_path):
    exe = r"C:\Users\me\AppData\Local\Programs\Seedbox 2\Seedbox 2.exe"
    cmd = update.install_command(tmp_path / "setup.exe", tmp_path, platform="win32", executable=exe, pid=42,
                                 relaunch=True)  # fmt: skip
    assert cmd[0] == "powershell.exe" and cmd[cmd.index("-File") + 1] == str(tmp_path / "install.ps1")
    assert cmd[cmd.index("-ProcessId") + 1] == "42" and cmd[cmd.index("-App") + 1] == exe
    script = (tmp_path / "install.ps1").read_text()
    assert "/VERYSILENT" in script and "Wait-Process" in script


def test_mac_app_bundle():
    assert update.mac_app_bundle("/Applications/Seedbox 2.app/Contents/MacOS/Seedbox 2") == Path("/Applications/Seedbox 2.app")
    assert update.mac_app_bundle("/usr/bin/python3") is None
    with pytest.raises(UpdateError):
        update.install_command(Path("x.dmg"), Path("."), platform="darwin", executable="/usr/bin/python3", pid=1,
                               relaunch=False)  # fmt: skip


def _fake_tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="the macOS helper is a bash script")
@pytest.mark.parametrize("works", [True, False])
def test_the_mac_helper_swaps_the_app_and_restarts_it(tmp_path, works):
    """install.sh, run for real against stand-ins for hdiutil, ditto and open: waits for the app's process to end,
    puts the new app in place (or keeps the old one when it can't) and opens it again."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    new_app = tmp_path / "dmg-contents" / "Seedbox 2.app"
    (new_app / "Contents").mkdir(parents=True)
    (new_app / "Contents" / "version").write_text("2.1.0")
    # hdiutil attach ... -mountpoint MNT DMG: "mount" by copying the DMG's stand-in folder
    _fake_tool(bin_dir, "hdiutil", f'''case "$1" in
attach) shift; while [ "$1" != -mountpoint ]; do shift; done; cp -R "{new_app.parent}/." "$2" ;;
detach) ;;
esac''')
    _fake_tool(bin_dir, "ditto", 'cp -R "$1" "$2"' if works else "exit 1")
    _fake_tool(bin_dir, "open", f'echo "$1" >> "{tmp_path}/opened"')
    app = tmp_path / "Applications" / "Seedbox 2.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "version").write_text("2.0.0")
    dmg = tmp_path / "update.dmg"
    dmg.write_bytes(b"dmg")
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])  # noqa: S603 - the "app"
    cmd = update.install_command(dmg, tmp_path, platform="darwin", executable=str(app / "Contents/MacOS/Seedbox 2"),
                                 pid=running.pid, relaunch=True)  # fmt: skip
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    helper = subprocess.Popen(cmd, env=env)  # noqa: S603
    running.wait()
    assert helper.wait(timeout=30) == (0 if works else 1)
    result = (tmp_path / update.RESULT_FILE).read_text().strip()
    assert (app / "Contents" / "version").read_text() == ("2.1.0" if works else "2.0.0")
    assert (tmp_path / "opened").read_text().strip() == str(app)  # opened again either way
    assert sorted(p.name for p in app.parent.iterdir()) == ["Seedbox 2.app"]  # no staged or old copies left
    if works:
        assert result == "ok" and not dmg.exists()
    else:
        assert result.startswith("failed: could not copy")


# ---------------------------------------------------------------- the API

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEDBOX_SUPPORT_DIR", str(tmp_path / "support"))
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SEEDBOX_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(tmp_path / "music"))
    monkeypatch.setattr(ytdlp, "check_in_background", lambda force=False: None)  # no PyPI
    from seedbox2.server.app import create_app

    app = create_app(None)
    app.state.updater = make_updater(tmp_path, github_with("2.1.0", b"dmg"))
    with TestClient(app) as client:
        yield client


def test_updates_api(api, monkeypatch):
    spawned = []
    monkeypatch.setattr(update, "_spawn_detached", lambda cmd, log: spawned.append(cmd))
    body = api.get("/api/system/updates").json()
    assert body["app"]["state"] == "idle" and body["app"]["current"] == "2.0.0"
    assert body["ytdlp"]["running"] not in ("", None)
    assert api.post("/api/system/updates/install", json={}).status_code == 409
    api.post("/api/system/updates/check")
    deadline = time.monotonic() + 5
    while api.get("/api/system/updates").json()["app"]["state"] != "ready" and time.monotonic() < deadline:
        time.sleep(0.02)
    app = api.app
    assert not app.state.quit.is_set()
    r = api.post("/api/system/updates/install", json={"relaunch": True})
    assert r.status_code == 200 and r.json()["app"]["install_on_exit"]
    assert app.state.quit.is_set()  # the shell closes the window, then:
    from seedbox2 import shell

    class Handle:
        pass

    handle = Handle()
    handle.app = app
    shell.install_update_at_exit(handle)
    assert spawned and spawned[-1][5] == "1"
    off = api.post("/api/system/updates/settings", json={"check": False}).json()
    assert off["app"]["enabled"] is False


# ---------------------------------------------------------------- yt-dlp

@pytest.fixture
def pydeps(tmp_path, monkeypatch):
    root = tmp_path / "pydeps"
    monkeypatch.setenv(ytdlp.PYDEPS_ENV, str(root))
    monkeypatch.setitem(ytdlp._state, "activate_called", False)
    monkeypatch.setitem(ytdlp._state, "activated", None)
    monkeypatch.setitem(ytdlp._state, "bundled", "2026.01.01")
    return root


def fake_copy(root: Path, version: str) -> Path:
    folder = root / "versions" / version
    (folder / "yt_dlp").mkdir(parents=True)
    (folder / ytdlp.MARKER).write_text(json.dumps({"ok": True, "yt_dlp": version}))
    return folder


def test_pointer_files_instead_of_symlinks(pydeps):
    folder = fake_copy(pydeps, "2026.09.01")
    ytdlp._point(pydeps, "current", folder)
    assert (pydeps / "current").read_text().strip() == "2026.09.01" and not (pydeps / "current").is_symlink()
    assert ytdlp._target(pydeps, "current") == folder
    assert ytdlp.installed_version() == "2026.09.01"
    (pydeps / "current").write_text("../../etc\n")
    assert ytdlp._target(pydeps, "current") is None


def test_activate_puts_a_newer_verified_copy_first(pydeps, monkeypatch):
    monkeypatch.setattr(sys, "modules", {k: v for k, v in sys.modules.items() if not k.startswith("yt_dlp")})
    monkeypatch.setattr(sys, "path", list(sys.path))
    folder = fake_copy(pydeps, "2026.09.01")
    ytdlp._point(pydeps, "current", folder)
    assert ytdlp.activate() == folder.resolve()
    assert sys.path[0] == str(folder.resolve())


def test_activate_keeps_the_bundled_copy_when_it_is_as_new(pydeps, monkeypatch):
    monkeypatch.setattr(sys, "modules", {k: v for k, v in sys.modules.items() if not k.startswith("yt_dlp")})
    ytdlp._point(pydeps, "current", fake_copy(pydeps, "2025.12.01"))
    assert ytdlp.activate() is None


def test_update_and_rollback(pydeps, monkeypatch):
    """_update with PyPI and the self-check replaced: the new folder gets the marker and `current`, the old one
    becomes `previous`; rolling back swaps them back, and again drops the download."""
    monkeypatch.setattr(ytdlp, "current_version", lambda: "2026.01.01")
    monkeypatch.setattr(ytdlp, "latest_version", lambda timeout=0: "2026.10.01")

    def wheel(name, version):
        return {"name": name, "version": version, "filename": f"{name}-{version}-py3-none-any.whl",
                "requires_dist": ["yt-dlp-ejs==0.3.0"] if name == "yt-dlp" else []}

    def download(w, dest):
        path = dest / w["filename"]
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(f"{w['name'].replace('-', '_')}/__init__.py", "")
        return path

    monkeypatch.setattr(ytdlp, "_wheel", wheel)
    monkeypatch.setattr(ytdlp, "_download", download)
    monkeypatch.setattr(ytdlp, "_verify", lambda folder, expected: {"ok": True, "version": expected, "ejs": "0.3.0"})
    old = fake_copy(pydeps, "2026.09.01")
    ytdlp._point(pydeps, "current", old)
    result = ytdlp.update_ytdlp()
    assert result["ok"] and result["version"] == "2026.10.01" and "Restart" in result["message"]
    assert ytdlp._target(pydeps, "current").name == "2026.10.01"
    assert ytdlp._target(pydeps, "previous") == old
    assert (pydeps / "versions" / "2026.10.01" / "yt_dlp_ejs" / "__init__.py").is_file()
    status = ytdlp.status()
    assert status["restart"] and status["can_roll_back"]
    assert ytdlp.update_ytdlp()["message"].startswith("yt-dlp 2026.10.01 is already installed")

    assert ytdlp.rollback()["version"] == "2026.09.01"
    assert ytdlp._target(pydeps, "current") == old and not (pydeps / "previous").exists()
    assert not (pydeps / "versions" / "2026.10.01").exists()
    assert "bundled" in ytdlp.rollback()["message"]
    assert ytdlp.rollback()["ok"] is False


def test_wheels_with_unsafe_paths_are_refused(tmp_path):
    whl = tmp_path / "bad.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("../evil.py", "")
    with pytest.raises(ytdlp.UpdateError, match="unsafe"):
        ytdlp._extract(whl, tmp_path / "out")


def test_the_self_check_child_reports_through_a_file(tmp_path):
    """The real check, in a child process, of a copy of the installed yt-dlp: it reports through a file, as the
    windowed app on Windows has no stdout."""
    import yt_dlp
    import yt_dlp_ejs

    folder = tmp_path / "versions" / "copy"
    for mod in (yt_dlp, yt_dlp_ejs):
        src = Path(mod.__file__).parent
        shutil.copytree(src, folder / src.name, ignore=shutil.ignore_patterns("__pycache__"))
    from yt_dlp.version import __version__ as version

    report = ytdlp._verify(folder, version)
    assert report["ok"] and report["version"] == version and report["extractors"] > 1000
    with pytest.raises(ytdlp.UpdateError, match="expected"):
        ytdlp._verify(folder, "1999.01.01")


# ---------------------------------------------------------------- report a problem

def test_report_a_problem(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    logs = home / "logs"
    logs.mkdir(parents=True)
    for var, sub in (("SEEDBOX_SUPPORT_DIR", "support"), ("SEEDBOX_CACHE_DIR", "cache"), ("MUSICRECO_OUTPUT_ROOT", "music")):
        monkeypatch.setenv(var, str(home / sub))
    monkeypatch.setenv("SEEDBOX_LOG_DIR", str(logs))
    (logs / "seedbox2.log").write_text(f"INFO imported {home}/music/track.flac\n")
    (logs / "seedbox2.log.1").write_text("older\n")
    (logs / "track.flac").write_bytes(b"audio")  # not a log: stays out
    (home / "cache" / "updates").mkdir(parents=True)
    (home / "cache" / "updates" / "install.log").write_text("installer said hi\n")
    monkeypatch.setattr(support, "run_self_test", lambda work: {"ran": True, "exit_code": 0, "report": {"ok": True}})

    out = support.build_bundle()
    path = Path(out["path"])
    assert path.parent == logs / support.REPORTS_SUBDIR and out["self_test_ok"] is True
    assert "issues/new?body=" in out["issue_url"]
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert names == {"README.txt", "system.json", "versions.json", "self-test.json", "logs/seedbox2.log",
                         "logs/seedbox2.log.1", "logs/install.log"}  # fmt: skip
        assert zf.read("logs/seedbox2.log").decode() == "INFO imported ~/music/track.flac\n"
        system = json.loads(zf.read("system.json"))
        assert system["version"] and system["folders"]["logs"] == "~/logs"
        versions = json.loads(zf.read("versions.json"))
        assert versions["analysers"]["key"]["VERSION"] and versions["models"]["beat_this"]["version"]
        assert versions["library"] == {"exists": False}
        assert str(home) not in "".join(zf.read(n).decode() for n in names)
    assert support.build_bundle()["files"]  # a second report leaves the first out
    assert len(list((logs / support.REPORTS_SUBDIR).glob("*.zip"))) >= 1
    assert not list((logs / support.REPORTS_SUBDIR).glob(".work-*"))


def test_library_versions_counts_analyses_by_version(tmp_path):
    from seedbox2.library.db import Database

    db = Database(tmp_path / "library.sqlite3")
    with db.write() as conn:
        conn.execute("INSERT INTO tracks (hash, duration) VALUES ('h1', 1.0)")
        conn.execute("INSERT INTO analyses (hash, analyser, version, data) VALUES ('h1', 'key', '1', '{}')")
    db.close()
    got = support.library_versions(tmp_path / "library.sqlite3")
    assert got["exists"] and got["schema"] > 0 and got["tracks"] == 1
    assert got["analyses"] == [["key", "1", 1]]


def test_the_self_test_runs_in_another_process(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "self_test_command",
                        lambda report: [sys.executable, "-c", f"open({str(report)!r}, 'w').write('{{\"ok\": true}}')"])
    got = support.run_self_test(tmp_path)
    assert got["ran"] and got["exit_code"] == 0 and got["report"] == {"ok": True}
