"""Tracks that are ready to mix (#99), for tests about something else than readiness.

Sets, mixes and exports only take ready tracks (`seedbox2.library.ready`). `make_ready` fills in whatever a track still
lacks: each required analysis it has none of at its current version, as a copy of its newest one of another version or
else empty (one it has is left as it is), a waveform and a stems artifact (pointing nowhere, so nothing reads them as files).
"""

from __future__ import annotations

from typing import Any


def make_ready(lib: Any, *hashes: str) -> None:
    """These tracks (none: every track in the library) ready."""
    from seedbox2.analysis import beats, energy, key, sections, vocals, waveform

    for h in hashes or [r["hash"] for r in lib.view_rows()]:
        for m in (beats, key, energy, sections, vocals):
            if lib.analysis(h, m.ANALYSER, str(m.VERSION)) is None:
                old = lib.analysis(h, m.ANALYSER)
                lib.put_analysis(h, m.ANALYSER, str(m.VERSION), old["data"] if old else {})
        kinds = {(a["kind"], a.get("model_version")) for a in lib.artifacts(h)}
        if ("waveform", waveform.MODEL_VERSION) not in kinds:
            lib.put_artifact(h, "waveform", waveform.MODEL_VERSION, f"/nonexistent/{h}.waveform")
        if not any(k == "stems" for k, _ in kinds):
            lib.put_artifact(h, "stems", "test", f"/nonexistent/{h}-stems")
