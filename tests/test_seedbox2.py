"""seedbox2 launcher: `python -m seedbox2` starts and keeps its data apart from Seedbox 1's."""

import subprocess
import sys
from pathlib import Path

from seedbox2 import __version__, app

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
