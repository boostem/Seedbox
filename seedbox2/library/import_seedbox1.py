"""Import Seedbox 1's crates, sets and user-edited mix plans into the library. Read-only on Seedbox 1's files.

Seedbox 1 keeps everything next to the music: a crate is a folder of MP3s under the crates folder (Settings.
output_root) with `.seedbox/crate.json` (tunebat's data), `tracks.csv` and the files' own tags; its set is the
crate in track-number order; edited transitions are `.seedbox/mixplans/<from>__<to>.json`, keyed by Spotify ID.
Here every file becomes a track keyed by its content hash (source `seedbox1`, source_id its Spotify ID), every
crate a crate, every crate's running order a set, and every saved mix plan a user-edited plan between two hashes.

Importing again updates what came from Seedbox 1 and never overwrites a plan edited in Seedbox 2. Files are only
read (musicreco.library's readers, mutagen and ffmpeg): nothing in the crates folder is written, moved or touched.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from musicreco import library as sb1
from seedbox2.library import hashing
from seedbox2.library.repo import Library

log = logging.getLogger(__name__)

SOURCE = "seedbox1"
HASH_WORKERS = min(4, os.cpu_count() or 1)  # ffmpeg decodes run outside the GIL


@dataclass
class ImportReport:
    crates: int = 0
    tracks: int = 0
    sets: int = 0
    plans: int = 0
    plans_kept: int = 0  # edited in Seedbox 2 since: left alone
    errors: list[str] = field(default_factory=list)
    new_hashes: list[str] = field(default_factory=list)  # tracks the library didn't have: they need analysis

    def to_json(self) -> dict:
        return {"crates": self.crates, "tracks": self.tracks, "sets": self.sets, "plans": self.plans,
                "plans_kept": self.plans_kept, "new": len(self.new_hashes), "errors": self.errors}  # fmt: skip


def _tags(f: sb1.CrateFile, track: object | None) -> dict:
    tags = {"camelot": f.camelot, "bpm": f.bpm or None, "genre": f.genre, "label": f.label, "remixer": f.remixer,
            "year": f.year, "energy_rating": f.energy_rating, "stars": f.stars or None, "bitrate": f.bitrate or None,
            "sample_rate": f.sample_rate or None}  # fmt: skip
    if track is not None:  # tunebat's data from crate.json/tracks.csv
        tags["tunebat"] = track.to_json()  # type: ignore[attr-defined]
    return {k: v for k, v in tags.items() if v not in (None, "")}


def import_seedbox1(lib: Library, root: Path | None = None,
                    progress: Callable[[int, int, str], None] | None = None) -> ImportReport:  # fmt: skip
    """Import every crate folder under `root` (default: the crates folder from Seedbox 1's settings).

    `progress(done, total, crate_folder)` is called after each crate. Unreadable files and plans are listed in
    the report's errors and skipped.
    """
    root = Path(root) if root is not None else sb1.downloads_root()
    report = ImportReport()
    crates = sb1.list_crates(root)
    with ThreadPoolExecutor(max_workers=HASH_WORKERS, thread_name_prefix="seedbox2-hash") as pool:
        for n, summary in enumerate(crates, 1):
            seed_dir = Path(summary["path"])
            try:
                import_crate(lib, seed_dir, pool, report)
            except Exception as e:
                log.exception("importing crate %s", seed_dir.name)
                report.errors.append(f"{seed_dir.name}: {e}")
            if progress is not None:
                progress(n, len(crates), seed_dir.name)
    log.info("imported from Seedbox 1 (%s): %s", root, report.to_json())
    return report


def import_crate(lib: Library, seed_dir: Path, pool: ThreadPoolExecutor, report: ImportReport) -> None:
    """Import (or update) one crate folder, holding Seedbox 1's lock on it while a download may be writing it."""
    with sb1.crate_lock(seed_dir):
        _import_crate(lib, seed_dir, pool, report)


def is_crate_folder(folder: Path) -> bool:
    """A folder Seedbox 1 downloaded a crate into (its tracks.csv or .seedbox/crate.json)."""
    return (folder / "tracks.csv").is_file() or sb1.crate_json_path(folder).is_file()


def _import_crate(lib: Library, seed_dir: Path, pool: ThreadPoolExecutor, report: ImportReport) -> None:
    files = sb1.crate_files(seed_dir)  # set order
    known = sb1.load_crate_tracks(seed_dir)
    seed = sb1.load_crate_seed(seed_dir)

    def hash_one(f: sb1.CrateFile) -> hashing.ContentHash | None:
        try:
            return lib.hash_file(f.path)
        except (OSError, hashing.HashError) as e:
            report.errors.append(f"{seed_dir.name}/{f.path.name}: {e}")
            return None

    hashes: list[str] = []
    by_spotify: dict[str, str] = {}
    for f, content in zip(files, pool.map(hash_one, files)):
        if content is None:
            continue
        track = known.get(f.track_id) if f.track_id else None
        meta = {"title": f.title or (track.name if track else f.path.stem),
                "artist": f.artist or (", ".join(track.artists) if track else ""),
                "album": f.album or (track.album if track else ""), "tags": _tags(f, track), "source": SOURCE,
                "source_id": f.track_id}  # fmt: skip
        if lib.track(content.hash) is None:
            report.new_hashes.append(content.hash)
        lib.add_file(f.path, content=content, **meta)
        hashes.append(content.hash)
        if f.track_id:
            by_spotify.setdefault(f.track_id, content.hash)
        report.tracks += 1
    seed_hash = by_spotify.get(seed.id) if seed else None
    crate_id = lib.save_crate(seed_dir.name, hashes, folder=str(seed_dir), source=SOURCE, seed_hash=seed_hash)
    lib.save_set(seed_dir.name, hashes, crate_id=crate_id, source=SOURCE)
    report.crates += 1
    report.sets += 1
    _import_plans(lib, seed_dir, by_spotify, report)


def _import_plans(lib: Library, seed_dir: Path, by_spotify: dict[str, str], report: ImportReport) -> None:
    folder = seed_dir / sb1.SEEDBOX_DIR / "mixplans"
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return
    except OSError as e:
        report.errors.append(f"{seed_dir.name}/mixplans: {e}")
        return
    for name in names:
        if name.startswith(".") or not name.endswith(".json") or "__" not in name:
            continue
        from_id, _, to_id = name[: -len(".json")].partition("__")
        where = f"{seed_dir.name}/mixplans/{name}"
        if from_id not in by_spotify or to_id not in by_spotify:
            report.errors.append(f"{where}: a track of this pair has no file in the crate")
            continue
        try:
            plan = json.loads((folder / name).read_text("utf-8"))
        except (OSError, ValueError) as e:
            report.errors.append(f"{where}: {e}")
            continue
        if not isinstance(plan, dict):
            report.errors.append(f"{where}: not a mix plan")
            continue
        if lib.put_plan(by_spotify[from_id], by_spotify[to_id], plan, user_edited=True, source=SOURCE,
                        overwrite=False):  # fmt: skip
            report.plans += 1
        else:
            report.plans_kept += 1
