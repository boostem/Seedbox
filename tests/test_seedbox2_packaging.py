"""The Seedbox 2 installers (packaging/): the self-test's onnxruntime check and what the smoke test holds a build to.
The builds themselves run by hand on each target (packaging/README-seedbox2.md)."""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from seedbox2 import shell
from seedbox2.models import selftest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = load("check_seedbox2")


def good_report(bundle: Path, accelerator: str = "CoreMLExecutionProvider") -> dict:
    return {
        "ok": True,
        "frozen": True,
        "ffmpeg": str(bundle / "bin" / "ffmpeg"),
        "ffmpeg_version": "ffmpeg version 8.1.3",
        "deno": str(bundle / "bin" / "deno"),
        "deno_version": "deno 2.9.8",
        "modules": {"webview": "ok", "fastapi": "ok"},
        "window_backend": "ok",
        "onnxruntime": {"ok": True, "providers": [accelerator, "CPUExecutionProvider"]},
        "frontend": str(bundle / "web"),
        "server": {"ok": True},
    }


def test_the_tiny_model_runs_here_and_in_a_spawned_worker():
    report = selftest.check()
    assert report["ok"], report
    assert report["in_process"] == report["worker"] == selftest.EXPECTED


def test_a_broken_onnxruntime_is_reported_not_raised(monkeypatch):
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # `import onnxruntime` raises
    report = selftest.check()
    assert report["ok"] is False
    assert "onnxruntime" in report["error"]


def test_self_test_writes_the_report_file_with_onnxruntime(tmp_path):
    report_file = tmp_path / "report.json"
    env = {**os.environ, "SEEDBOX_SUPPORT_DIR": str(tmp_path / "s"), "SEEDBOX_CACHE_DIR": str(tmp_path / "c"),
           "SEEDBOX_LOG_DIR": str(tmp_path / "l"), "MUSICRECO_OUTPUT_ROOT": str(tmp_path / "m")}  # fmt: skip
    # the exit code depends on whether this checkout has built frontend2/dist
    subprocess.run([sys.executable, "-m", "seedbox2", "--self-test", "--report", str(report_file)], cwd=ROOT, env=env,
                   capture_output=True, timeout=180, check=False)  # fmt: skip
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["onnxruntime"]["ok"], report["onnxruntime"]
    assert "window_backend" not in report  # only the frozen app checks it


def test_binary_version_is_none_for_a_binary_that_does_not_run(tmp_path):
    assert shell._binary_version(None) is None
    assert shell._binary_version(str(tmp_path / "nothing")) is None


def test_binary_version_reads_the_first_line():
    assert "Python" in shell._binary_version(sys.executable)


def test_a_good_report_has_no_problems(tmp_path):
    assert check.problems(good_report(tmp_path), tmp_path, platform="darwin") == []


@pytest.mark.parametrize("change, expected", [
    ({"ok": False}, "self-test"),
    ({"frozen": False}, "frozen"),
    ({"ffmpeg_version": None}, "ffmpeg doesn't run"),
    ({"deno": "/opt/homebrew/bin/deno"}, "deno isn't the bundled one"),
    ({"deno": None}, "deno isn't the bundled one"),
    ({"modules": {"webview": "ImportError: no"}}, "import webview"),
    ({"window_backend": "ImportError: no clr"}, "window backend"),
    ({"onnxruntime": {"ok": False, "error": "no dll"}}, "no dll"),
    ({"onnxruntime": {"ok": True, "providers": ["CPUExecutionProvider"]}}, "CoreMLExecutionProvider"),
    ({"frontend": None}, "no built UI"),
    ({"server": {"ok": False}}, "server"),
])  # fmt: skip
def test_problems_catches(tmp_path, change, expected):
    found = check.problems({**good_report(tmp_path), **change}, tmp_path, platform="darwin")
    assert any(expected in problem for problem in found), found


def test_problems_wants_directml_on_windows(tmp_path):
    report = good_report(tmp_path, accelerator="DmlExecutionProvider")
    assert check.problems(report, tmp_path, platform="win32") == []
    assert check.problems(good_report(tmp_path), tmp_path, platform="win32")


def test_forbidden_in_finds_torch_and_the_gpl_ffmpeg_package(tmp_path):
    assert check.forbidden_in(tmp_path) == []
    (tmp_path / "_internal" / "torch" / "lib").mkdir(parents=True)
    (tmp_path / "_internal" / "imageio_ffmpeg").mkdir()
    assert sorted(Path(p).name for p in check.forbidden_in(tmp_path)) == ["imageio_ffmpeg", "torch"]


def test_bundle_root_is_the_app_on_macos_and_the_folder_elsewhere():
    assert check.bundle_root(Path("/a/Seedbox 2.app/Contents/MacOS/Seedbox 2")).name == "Seedbox 2.app"
    assert check.bundle_root(Path("/a/Seedbox 2/Seedbox 2.exe")).name == "Seedbox 2"


@pytest.mark.skipif(sys.platform == "win32", reason="a fake ffmpeg as a shell script")
def test_ffmpeg_problems_refuses_a_gpl_build(tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\necho 'configuration: --enable-gpl --enable-libx264'\n")
    fake.chmod(0o755)
    assert any("--enable-gpl" in problem for problem in check.ffmpeg_problems(str(fake), tmp_path))


def test_intel_macs_get_the_last_onnxruntime_with_their_wheels():
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert any(re.match(r'onnxruntime<1\.24; sys_platform == "darwin" and platform_machine == "x86_64"', ln)
               for ln in lines)  # fmt: skip


def test_the_spec_keeps_torch_and_the_gpl_ffmpeg_out():
    spec = (PACKAGING / "seedbox2.spec").read_text(encoding="utf-8")
    excludes = spec.split("excludes = [")[1].split("]")[0]
    for module in ("torch", "imageio_ffmpeg", "onnx"):
        assert f'"{module}"' in excludes
