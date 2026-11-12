"""A crate from tunebat, as in Seedbox 1: search a song, open it with every track tunebat recommends from it, tick
and download them all into one crate.

`open(seed_id)` is the seed and its recommendations, best first by musicreco's mixing score. `related` is Seedbox 1's
pre-tick rule (a compatible key and the tempo within the tolerance, half and double time count), so the page can tick
those and leave the rest for the user to add. `download(seed_id, ids)` runs musicreco's download batch in the
background into the crate's folder in the music folder (`<Artists - Title>/`, as Seedbox 1 names it), writes its
`tracks.csv` and `crate.json`, then hands the folder to the importer, which takes it in as a crate and a set.

Each track is imported as soon as its file is there (downloaded, copied or already in the folder), so it has its
library row and its analyses queued while the rest of the batch downloads. That import is a plain one
(`LocalMusic.import_now`): mid-batch the folder has no `tracks.csv`/`crate.json` yet, or an earlier run's, so a crate
import would save a half-done crate and set. The folder import at the end reconciles: `complete_batch` only rewrites
tags (energy, track number), which the content hash ignores, so it finds the same tracks, adds tunebat's data, the
crate and the set in order, and queues nothing again (only new tracks get analyses queued).

Tunebat calls go through `TunebatTry`'s client: one at a time, 1.2 s apart, a seed's answer kept for the run. One
download runs at a time; the others wait their turn. Each change to a download is a `tunebat.crate` event with the
download (`job_json`).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from seedbox2.library.tunebat_try import TunebatTry, same_track

log = logging.getLogger(__name__)

FINAL = ("downloaded", "copied", "exists", "skipped", "failed")
HAVE_FILE = ("downloaded", "copied", "exists")  # the track's file is in the crate folder: import it at once
MAX_JOBS = 20  # finished downloads kept for the page


class UnknownCrate(Exception):
    """A seed that wasn't opened first, or tracks that aren't part of its crate."""


def _track_json(t: Any) -> dict:
    return {"id": t.id, "name": t.name, "artists": list(t.artists), "album": t.album, "duration": t.duration_ms / 1000,
            "bpm": t.bpm, "camelot": t.camelot, "key": t.key, "energy": t.energy, "cover_url": t.cover_url}  # fmt: skip


def _settings() -> Any:
    from musicreco import settings

    try:
        return settings.load_settings()
    except Exception:  # noqa: BLE001 - a broken Seedbox 1 settings file: its defaults
        log.warning("couldn't read Seedbox 1's settings, using the defaults", exc_info=True)
        return settings.Settings()


class TunebatCrates:
    def __init__(self, tunebat: TunebatTry, music: Any, publish: Callable[[str, dict], None], *,
                 run_batch: Callable[..., list] | None = None, complete_batch: Callable[..., Any] | None = None,
                 settings: Callable[[], Any] = _settings) -> None:  # fmt: skip
        from musicreco import pipeline

        self.tunebat = tunebat
        self.music = music
        self.publish = publish
        self._run_batch = run_batch or pipeline.run_batch
        self._complete_batch = complete_batch or pipeline.complete_batch
        self._settings = settings
        self._lock = threading.Lock()
        self._crates: dict[str, tuple[Any, list[Any], dict[str, Any]]] = {}  # seed id: (seed, recs best first, scores)
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix="seedbox2-tunebat-crate")

    # --- the crate -------------------------------------------------------------------------------------------------

    def _ranked(self, seed_id: str) -> tuple[Any, list[Any], dict[str, Any]]:
        from musicreco import mixing

        with self._lock:
            if seed_id in self._crates:
                return self._crates[seed_id]
        detail = self.tunebat.detail(seed_id)
        seed, seen, recs = detail.seed, {detail.seed.id, seed_id}, []
        for t in detail.recommended:
            if t.id not in seen:
                seen.add(t.id)
                recs.append(t)
        tol = float(self._settings().bpm_tolerance)
        scores = {t.id: mixing.score(seed, t, tol=tol) for t in recs}
        recs.sort(key=lambda t: -scores[t.id].value)  # stable: ties keep tunebat's order
        with self._lock:
            self._crates[seed_id] = (seed, recs, scores)
        return seed, recs, scores

    def _owned(self) -> list[tuple[str, str, str | None]]:
        """(title, artist, tunebat id) of every track in the library."""
        lib = getattr(self.music, "lib", None)
        if lib is None:
            return []
        out = []
        for r in lib.view_rows():
            tags = r.get("tags") if isinstance(r.get("tags"), dict) else {}
            tb = tags.get("tunebat") if isinstance(tags.get("tunebat"), dict) else {}
            out.append((r.get("title") or "", r.get("artist") or "", tb.get("id")))
        return out

    def open(self, seed_id: str) -> dict:
        """`{seed, folder, recs}`: each rec `{track, score, move, chips, related, reason, in_library}`, best first.
        `TunebatUnavailable` when tunebat can't be reached."""
        from musicreco import library, mixing

        seed, recs, scores = self._ranked(seed_id)
        tol = float(self._settings().bpm_tolerance)
        owned = self._owned()

        def have(t: Any) -> bool:
            return any(i == t.id or same_track(t.name, t.artist_str, title, artist) for title, artist, i in owned)

        rows = []
        for t in recs:
            s = scores[t.id]
            rows.append({"track": _track_json(t), "score": s.value, "move": s.move, "chips": list(s.chips),
                         "related": bool(s.passed), "reason": mixing.passes_rule(seed, t, tol)[1],
                         "in_library": have(t)})  # fmt: skip
        return {"seed": {**_track_json(seed), "in_library": have(seed)}, "folder": library.sanitize_filename(seed.display_name),
                "tolerance": tol, "recs": rows}  # fmt: skip

    # --- downloads -------------------------------------------------------------------------------------------------

    def download(self, seed_id: str, ids: list[str]) -> dict:
        """Queue a download of these tracks of an opened crate (the seed's id for the seed). UnknownCrate if the crate
        wasn't opened or an id isn't in it; ValueError if there is nothing to download."""
        with self._lock:
            crate = self._crates.get(seed_id)
        if crate is None:
            raise UnknownCrate(seed_id)
        seed, recs, scores = crate
        wanted = set(ids)
        if not wanted:
            raise ValueError("tick at least one track")
        if seed_id in wanted:  # tunebat may answer a relinked id for the seed
            wanted.add(seed.id)
        everything = [seed, *recs]
        unknown = wanted - {t.id for t in everything} - {seed_id}
        if unknown:
            raise UnknownCrate(", ".join(sorted(unknown)[:5]))
        from musicreco import library

        selected = [t for t in everything if t.id in wanted]  # the seed first, then the recs best first
        job = {"id": uuid.uuid4().hex[:12], "seed_id": seed_id, "seed": seed.display_name,
               "folder": library.sanitize_filename(seed.display_name), "state": "queued", "error": None,
               "items": [{"id": t.id, "name": t.name, "artists": list(t.artists), "text": "queued", "status": None,
                          "reason": None} for t in selected]}  # fmt: skip
        cancel = threading.Event()
        with self._lock:
            self._jobs[job["id"]] = job
            self._cancels[job["id"]] = cancel
            for old in [j for j in self._jobs.values() if j["state"] in ("done", "cancelled", "failed")][:-MAX_JOBS]:
                self._jobs.pop(old["id"], None)
                self._cancels.pop(old["id"], None)
        queued = self.job(job["id"])
        self._emit(job)
        self._queue.submit(self._run, job, seed, recs, scores, selected, cancel)
        return queued

    def job(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else {**job, "items": [dict(i) for i in job["items"]]}

    def jobs(self) -> list[dict]:
        with self._lock:
            ids = list(self._jobs)
        return [j for i in ids if (j := self.job(i)) is not None]

    def cancel(self, job_id: str) -> dict | None:
        """Stop a download: what isn't started stays out, what is running finishes. None if there's no such job."""
        with self._lock:
            event = self._cancels.get(job_id)
        if event is None:
            return None
        event.set()
        return self.job(job_id)

    def _emit(self, job: dict) -> None:
        self.publish("tunebat.crate", self.job(job["id"]) or job)

    def _set(self, job: dict, **fields: Any) -> None:
        with self._lock:
            job.update(fields)
        self._emit(job)

    def _item(self, job: dict, track_id: str, text: str, status: str | None = None, reason: str | None = None) -> None:
        with self._lock:
            for item in job["items"]:
                if item["id"] == track_id:
                    item["text"] = text
                    if status is not None or text in FINAL:
                        item["status"] = status or text
                        item["reason"] = reason
        self._emit(job)

    def _run(self, job: dict, seed: Any, recs: list[Any], scores: dict[str, Any], selected: list[Any],
             cancel: threading.Event) -> None:  # fmt: skip
        from musicreco import library
        from musicreco import paths as binaries

        try:
            if cancel.is_set():
                self._set(job, state="cancelled")
                return
            if binaries.ffmpeg_path() is None:
                self._set(job, state="failed", error="ffmpeg is missing, and the download needs it. Install it, then "
                                                     "try again.")  # fmt: skip
                return
            self._set(job, state="running")
            settings = self._settings()
            seed_dir = library.seed_dir(seed, root=Path(self.music.music_dir))
            imports = ThreadPoolExecutor(max_workers=1, thread_name_prefix="seedbox2-tunebat-import")

            def on_update(t: Any, text: str) -> None:  # from the download workers: the import waits its turn
                self._item(job, t.id, text)
                if text in HAVE_FILE:
                    imports.submit(self._import_now, seed_dir, t)

            try:
                results = self._run_batch(selected, seed, seed_dir, workers=int(settings.workers),
                                          on_update=on_update, tolerance_s=float(settings.tolerance_s),
                                          cancel=cancel)  # fmt: skip
            finally:
                imports.shutdown(wait=True)  # before complete_batch retags the files
            for r in results:
                self._item(job, r.track.id, r.status, r.status, r.reason)
            self._set(job, state="finishing")
            # after a cancel too: whatever did download is numbered, listed and written to the crate's files
            done = self._complete_batch(seed, recs, scores, {t.id for t in selected}, seed_dir, results,
                                        bpm_tolerance=float(settings.bpm_tolerance))  # fmt: skip
            if errors := getattr(done, "errors", None):
                log.warning("%s: couldn't write %s", seed_dir.name, "; ".join(errors))
            self.music.import_files([seed_dir])
            self._set(job, state="cancelled" if cancel.is_set() else "done", path=str(seed_dir))
        except Exception as e:  # noqa: BLE001 - the thread's last stop: whatever went wrong goes to the screen
            log.exception("tunebat crate %s failed", job["folder"])
            self._set(job, state="failed", error=f"The download failed ({e}). Check the connection and try again.")

    def _import_now(self, seed_dir: Path, t: Any) -> None:
        """Import the file of a track that just finished, so it is in the library and analysing at once."""
        from musicreco import library

        try:
            here = seed_dir.resolve()
            files = [p for p in library.LibraryIndex(seed_dir).find(t.id) if p.parent.resolve() == here]
            if files:
                self.music.import_now(files)
        except Exception:  # noqa: BLE001 - the folder import at the end takes it in anyway
            log.warning("couldn't import %s straight away", t.id, exc_info=True)

    def close(self) -> None:
        with self._lock:
            events = list(self._cancels.values())
        for e in events:
            e.set()
        self._queue.shutdown(wait=False, cancel_futures=True)
