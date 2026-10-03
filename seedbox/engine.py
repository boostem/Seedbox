"""The server's single doorway into musicreco: thin wrappers, plus lazy imports of the optional modules
(previews / structure / transitions) so the API still boots if one of them is missing or broken.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from musicreco import library, mixing, paths, pipeline
from musicreco import settings as msettings
from musicreco.models import DownloadResult, Score, Track

log = logging.getLogger(__name__)


def optional(name: str) -> ModuleType | None:
    """musicreco.<name> if it imports, else None."""
    try:
        return importlib.import_module(f"musicreco.{name}")
    except ImportError as e:
        if e.name not in (f"musicreco.{name}", None):
            log.warning("musicreco.%s failed to import: %s", name, e)
        return None


def accepts(fn: Callable, param: str) -> bool:
    """True if `fn` takes a parameter called `param`."""
    try:
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# --- settings + binaries ------------------------------------------------------------------------------------------

Settings = msettings.Settings
cache_dir = msettings.cache_dir
load_settings = msettings.load_settings
save_settings = msettings.save_settings
settings_to_json = msettings.to_json
ffmpeg_path = paths.ffmpeg_path
deno_path = paths.deno_path


def settings_from_json(d: dict, strict: bool = False) -> Settings:
    """Settings from a dict; strict raises ValueError on unknown keys / bad values (API input)."""
    return msettings.from_json(d, strict=strict)


# --- ranking ------------------------------------------------------------------------------------------------------


def passes_rule(seed: Track, other: Track, tol: float) -> tuple[bool, str]:
    return mixing.passes_rule(seed, other, tol=tol)


def unique_recs(seed: Track, recs: list[Track]) -> list[Track]:
    """Recommendations without the seed itself or repeats, in tunebat's order."""
    seen = {seed.id}
    out = []
    for t in recs:
        if t.id not in seen:
            seen.add(t.id)
            out.append(t)
    return out


def rank(seed: Track, recs: list[Track], tol: float) -> tuple[list[Track], dict[str, Score]]:
    """Recs best-first by mixing.score (ties keep tunebat's order), plus each one's Score."""
    scores = {t.id: mixing.score(seed, t, tol=tol) for t in recs}
    return sorted(recs, key=lambda t: -scores[t.id].value), scores


# --- crates -------------------------------------------------------------------------------------------------------


def crate_folder_name(seed: Track) -> str:
    return library.sanitize_filename(seed.display_name)


def crate_seed_id(seed_dir: Path) -> str | None:
    seed = library.load_crate_seed(seed_dir)
    return seed.id if seed else None


# --- download batches ---------------------------------------------------------------------------------------------


def run_batch(
    tracks: list[Track],
    seed: Track,
    seed_dir: Path,
    *,
    workers: int,
    tolerance_s: float,
    on_update: Callable[[Track, str], None],
    cancel: threading.Event,
) -> list[DownloadResult]:
    """pipeline.run_batch, cancellable from another thread via `cancel`."""
    return pipeline.run_batch(tracks, seed, seed_dir, workers=workers, on_update=on_update, tolerance_s=tolerance_s,
                              cancel=cancel)  # fmt: skip


def complete_batch(
    seed: Track,
    recs: list[Track],
    scores: dict[str, Score],
    ticked_ids: set[str],
    seed_dir: Path,
    results: list[DownloadResult],
    tol: float = 0.10,
) -> Any:
    """Everything the CLI does after run_batch: finalize, tracks.csv, skipped.txt, .m3u8, .xml, crate.json."""
    return pipeline.complete_batch(seed, recs, scores, ticked_ids, seed_dir, results, bpm_tolerance=tol)
