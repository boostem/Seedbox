"""Seedbox 2: the next generation of the Seedbox desktop app (see seedbox2/README.md for the plan).

`python -m seedbox2` runs its own server (seedbox2/server/) and UI (frontend2/), with its own settings, caches and
logs, so it can be used side by side with Seedbox 1 (`seedbox/`). musicreco is reused by import.
"""

import sys

__version__ = "2.0.0.dev0"

# CI tests 3.12 and 3.13; older Pythons fail deep in an import (e.g. `X | Y` type aliases need 3.10). Say so up
# front instead. Keep this file parseable by old Pythons.
MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:
    raise SystemExit(
        "Seedbox 2 needs Python %d.%d or newer, but %s is Python %d.%d.\n"
        "Recreate the virtualenv with a newer Python (see Setup in README.md), for example:\n"
        "  rm -rf .venv && /opt/homebrew/bin/python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
        "then run .venv/bin/python -m seedbox2"
        % (MIN_PYTHON + (sys.executable,) + tuple(sys.version_info[:2]))
    )
