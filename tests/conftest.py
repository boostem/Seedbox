"""What the whole test run shares."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _a_machine_on_mains(monkeypatch):
    """Tests behave the same on a laptop on battery: Seedbox 2's low-power mode (#85) only turns on where a test asks."""
    from seedbox2 import power

    monkeypatch.setattr(power, "read_power", lambda: power.Reading(False, 100, "nominal"))
