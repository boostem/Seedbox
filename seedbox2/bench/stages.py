"""The benchmarked stages. Each runs in its own process (measure.py) and hands its output to the next stage in files.

Until Seedbox 2's own code lands they run Seedbox 1's (`IMPL` says which): analysis is musicreco.structure until
#43 (beats), #45 (key) and #49 (waveforms), planning and rendering are musicreco.mixplan / mixrender until #51 and
the v2 mix engine. Stems have no Seedbox 1 counterpart: the stage is skipped until #44 adds
`seedbox2.analysis.stems.separate(path, out_dir)`. Point the wrappers below at the new code as each ticket lands.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# imported here, before measure.py starts the clock
from musicreco import mixplan, mixrender, structure
from musicreco.models import Track
from musicreco.paths import ffmpeg_path

IMPL = {
    "analysis": "musicreco.structure.analyze (Seedbox 1, until #43/#45/#49)",
    "stems": "seedbox2.analysis.stems.separate (#44)",
    "plan": "musicreco.mixplan.generate, 32 bars (Seedbox 1, until #51)",
    "render": "musicreco.mixrender.render_audio (Seedbox 1, until the v2 engine)",
}
ORDER = ("analysis", "stems", "plan", "render")


def missing(stage: str) -> str | None:
    """Why `stage` can't run here, or None."""
    if stage == "stems":
        try:
            found = importlib.util.find_spec("seedbox2.analysis.stems") is not None
        except ModuleNotFoundError:
            found = False
        if not found:
            return "not implemented yet (#44)"
    if stage in ("analysis", "render"):
        if ffmpeg_path() is None:
            return "needs ffmpeg"
    return None


def analysis(path: str, out_json: str) -> dict:
    an = structure.analyze(Path(path), track_id=Path(path).stem)
    Path(out_json).write_text(json.dumps(an))
    return {"bpm": an.get("bpm"), "bars": an.get("bar_count")}


def stems(path: str, out_dir: str) -> dict:
    from seedbox2.analysis import stems as impl  # #44

    impl.separate(Path(path), Path(out_dir))
    return {}


def _track(path: Path, an: dict):
    return Track(id=path.stem, name=path.stem, artists=[], album="", release_date="",
                 duration_ms=int(an["duration_s"] * 1000), popularity=0, key="", camelot="", bpm=an["bpm"],
                 energy=0.0, danceability=0.0, cover_url=None)  # fmt: skip


def plan(a_path: str, a_json: str, b_path: str, b_json: str, out_json: str) -> dict:
    a_an, b_an = json.loads(Path(a_json).read_text()), json.loads(Path(b_json).read_text())
    p = mixplan.generate(_track(Path(a_path), a_an), a_an, _track(Path(b_path), b_an), b_an, str(Path(a_path).parent),
                         bars=32)  # fmt: skip
    Path(out_json).write_text(json.dumps(p))
    return {"technique": p.get("technique")}


def render(plan_json: str, a_path: str, b_path: str) -> dict:
    r = mixrender.render_audio(json.loads(Path(plan_json).read_text()), Path(a_path), Path(b_path))
    return {"duration_s": round(float(r["duration_s"]), 2)}
