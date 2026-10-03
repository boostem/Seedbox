"""Per-track pipeline (library check -> YouTube match -> download -> metadata -> loudness -> tags), run a few
at a time, then crate-wide energy ratings and set order (finalize_crate) and the crate's files (complete_batch).

Genre/Beatport lookups and cover art only need tunebat's data, so a _Prefetcher thread fetches them for the
whole batch while the workers download; a worker picks its track's result up after downloading.

Each track is built in a private `seed_dir/.tmp-<id>/` folder and only renamed to its final name once fully
tagged, so an interrupted run never leaves an untagged or half-written MP3 where find_existing would trust it.

Everything here is thread-safe: the app runs batches from worker threads (cancel via run_batch's `cancel`);
the CLI runs them from the main thread (Ctrl-C). Run one batch per crate folder at a time.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

from . import analysis, genres, library, mixing, tagging, youtube
from .models import BeatportInfo, CompletedBatch, DownloadResult, Loudness, Score, Track, YTCandidate

log = logging.getLogger(__name__)

CANCELLED = "cancelled (Ctrl-C)"
TMP_PREFIX = ".tmp-"
FILE_STATUSES = ("downloaded", "copied", "exists")
_ALT = "trying alternative"

# Status texts reported while a track is in progress -> how a failure in that step reads.
_STEP_NAMES = {
    "checking library": "library check",
    "copying": "copy",
    "searching": "YouTube search",
    "downloading": "download",
    _ALT: "download",
    "genre lookup": "genre lookup",
    "loudness scan": "loudness scan",
    "tagging": "tagging",
}

_claims_lock = threading.Lock()
_claims: dict[Path, str] = {}  # target path -> Spotify ID of the worker currently writing it
_active_work: set[str] = set()  # real paths of .tmp-* dirs in use in this process (never swept); under _claims_lock


class _Cancelled(Exception):
    pass


def process_track(
    track: Track,
    seed: Track,
    seed_dir: Path,
    *,
    on_status: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    index: library.LibraryIndex | None = None,
    prefetch: _Prefetcher | None = None,
    tolerance_s: float = 15.0,
) -> DownloadResult:
    """Get one track into `seed_dir`. Never raises: errors come back as status "failed" with a reason.

    `tolerance_s` is the YouTube duration tolerance (youtube.find_match).
    `on_status` receives each step ("searching", "downloading", ...) and finally the result status.
    `cancel`, when set, stops the track before its next network step (an in-flight download finishes).
    `index` is the batch's shared library index (a fresh scan of seed_dir.parent if omitted).
    `prefetch` supplies genre/Beatport info looked up ahead of time (looked up inline if omitted).
    """
    result = DownloadResult(track=track, status="failed")
    step = "checking library"

    def emit(text: str) -> None:
        if on_status:
            try:
                on_status(text)
            except Exception:  # noqa: BLE001 - a display glitch must not fail a download
                pass

    def report(text: str) -> None:
        nonlocal step
        step = text
        if cancel is not None and cancel.is_set() and (text in ("searching", "downloading") or text.startswith(_ALT)):
            raise _Cancelled
        emit(text)

    try:
        _process(result, seed, seed_dir, report, index or library.LibraryIndex(seed_dir.parent), prefetch, tolerance_s)
    except _Cancelled:
        result.status, result.reason = "failed", CANCELLED
    except Exception as e:  # noqa: BLE001 - one bad track must never take down the batch
        what = _STEP_NAMES[_ALT] if step.startswith(_ALT) else _STEP_NAMES.get(step, step)
        msg = _describe(e)
        result.status = "failed"
        result.reason = msg if msg.lower().startswith(what.lower()) else f"{what} failed: {msg}"
    if prefetch is not None and result.status != "downloaded":
        prefetch.discard(track)  # don't spend rate-limited lookups on a track that won't be tagged
    emit("cancelled" if result.reason == CANCELLED else result.status)
    return result


def _process(
    result: DownloadResult,
    seed: Track,
    seed_dir: Path,
    report: Callable[[str], None],
    index: library.LibraryIndex,
    prefetch: _Prefetcher | None = None,
    tolerance_s: float = 15.0,
) -> None:
    """Fill in `result` step by step; raises on failure."""
    track = result.track

    report("checking library")
    found = index.find(track.id)
    here = [p for p in found if p.parent.resolve() == seed_dir.resolve()]
    if here:
        result.status, result.path = "exists", here[0]
        _fill_from_tags(result)
        return
    if found:
        report("copying")
        src = found[0]
        with _claimed_path(seed_dir, track) as dest, _work_dir(seed_dir, track) as work:
            staged = work / "audio.mp3"
            shutil.copy2(src, staged)
            tagging.set_grouping(staged, seed)
            _publish(staged, dest)
        index.add(dest, track.id)
        result.status, result.path, result.reason = "copied", dest, f"from {src.parent.name}"
        _fill_from_tags(result)
        return

    report("searching")
    match = youtube.find_match(track, tolerance_s=tolerance_s)
    result.closest = match.closest
    if match.match is None:
        result.status = "skipped"
        result.reason = match.reason or f"no YouTube candidate within ±{tolerance_s:g} s"
        return
    result.youtube = match.match

    report("downloading")
    with _claimed_path(seed_dir, track) as dest, _work_dir(seed_dir, track) as work:
        audio = _download_any(result, [match.match, *match.alternatives], work, report)
        report("genre lookup")
        meta = prefetch.get(track) if prefetch is not None else None
        result.genre, result.genre_source, result.beatport = meta or _metadata(track)
        report("loudness scan")
        result.loudness = _loudness(audio)
        report("tagging")
        tagging.write_tags(audio, track, seed, result.genre, beatport=result.beatport, loudness=result.loudness)
        _publish(audio, dest)
    index.add(dest, track.id)
    result.status, result.path = "downloaded", dest


class _AllCandidatesFailed(Exception):
    pass


def _download_any(
    result: DownloadResult, candidates: list[YTCandidate], work: Path, report: Callable[[str], None]
) -> Path:
    """Download the first candidate that works into `work`; on YouTubeError move on to the next one.

    Records the candidate used in `result.youtube` (and a note in `result.reason` if it wasn't the first).
    """
    errors: list[str] = []
    for n, cand in enumerate(candidates, 1):
        if n > 1:
            report(f"{_ALT} {n}/{len(candidates)}")
        try:
            audio = Path(youtube.download(cand, work / "audio.mp3"))
        except youtube.YouTubeError as e:
            if len(candidates) == 1:
                raise
            errors.append(_describe(e))
            (work / "audio.mp3").unlink(missing_ok=True)
            continue
        result.youtube = cand
        if errors:
            result.reason = f"used alternative {n}/{len(candidates)} (first choice failed: {errors[0]})"
        return audio
    raise _AllCandidatesFailed(f"{errors[-1]} (all {len(candidates)} candidates failed)")


def _metadata(track: Track) -> tuple[str | None, str | None, BeatportInfo | None]:
    try:
        return genres.find_metadata(track)
    except Exception as e:  # noqa: BLE001 - tags without a genre beat losing the download
        log.warning("metadata lookup failed for %r: %s", track.display_name, _describe(e))
        return None, None, None


class _Prefetcher:
    """One background thread that looks up genre/Beatport info and cover art for a batch, in batch order.

    Beatport and MusicBrainz are rate-limited process-wide, so one thread is as fast as several; running it
    beside the downloads takes that waiting off the download workers. `get` bumps a track that hasn't been
    started to the front of the queue and waits for it; it returns None (caller looks up inline) for a track
    that was never queued or if the thread stopped.
    """

    def __init__(self, tracks: Iterable[Track]) -> None:
        self._cond = threading.Condition()
        self._queue = list({t.id: t for t in tracks}.values())
        self._busy: str | None = None
        self._results: dict[str, tuple[str | None, str | None, BeatportInfo | None]] = {}
        self._thread = threading.Thread(target=self._run, name="musicreco-prefetch", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                if not self._queue:
                    self._busy = None
                    self._cond.notify_all()
                    return
                track = self._queue.pop(0)
                self._busy = track.id
            meta = _metadata(track)
            tagging.prefetch_cover(track.cover_url)
            with self._cond:
                self._results[track.id] = meta
                self._busy = None
                self._cond.notify_all()

    def get(self, track: Track) -> tuple[str | None, str | None, BeatportInfo | None] | None:
        with self._cond:
            if track.id not in self._results and track.id != self._busy:
                queued = next((i for i, t in enumerate(self._queue) if t.id == track.id), None)
                if queued is None:
                    return None
                self._queue.insert(0, self._queue.pop(queued))
            while track.id not in self._results and self._thread.is_alive():
                self._cond.wait(0.25)
            return self._results.get(track.id)

    def discard(self, track: Track) -> None:
        """Drop a track that no longer needs a lookup (skipped, failed, already on disk) if not yet started."""
        with self._cond:
            self._queue = [t for t in self._queue if t.id != track.id]

    def close(self) -> None:
        """Stop after the lookup in progress."""
        with self._cond:
            self._queue.clear()


def _loudness(path: Path) -> Loudness | None:
    try:
        return analysis.measure_loudness(path)
    except Exception as e:  # noqa: BLE001 - ReplayGain is optional
        log.warning("loudness scan failed for %s: %s", path.name, _describe(e))
        return None


def _fill_from_tags(result: DownloadResult) -> None:
    """Genre, energy, set position, Beatport extras and ReplayGain of an existing file, so its row isn't blank."""
    try:
        tags = tagging.read_tags_summary(result.path)
    except Exception as e:  # noqa: BLE001 - only cosmetic
        log.warning("couldn't read tags of %s: %s", result.path.name if result.path else "?", _describe(e))
        return
    if tags.get("genre"):
        result.genre, result.genre_source = tags["genre"], "file"
    if stars := _int(tags.get("energy_stars")):
        result.energy_rating = max(1, min(10, stars * 2))  # finalize_crate recomputes the exact 1-10 value
    if order := _int(tags.get("track_number")):
        result.order = order
    extras = {k: tags.get(k) or None for k in ("label", "isrc", "mix_name")}
    if any(extras.values()):
        result.beatport = BeatportInfo(**extras)
    rg = tags.get("replaygain_db")
    if isinstance(rg, (int, float)) and math.isfinite(rg):  # peak isn't kept in the summary
        result.loudness = Loudness(lufs=round(-18.0 - rg, 2), true_peak_dbtp=math.nan, replaygain_db=rg, peak=math.nan)


def _int(v: object) -> int | None:
    """5, 5.0, "5" or "5/12" -> 5; anything else -> None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if math.isfinite(v) and v > 0 else None
    if isinstance(v, str) and (m := re.match(r"\s*(\d+)", v)):
        return int(m.group(1)) or None
    return None


@contextmanager
def _work_dir(seed_dir: Path, track: Track) -> Iterator[Path]:
    """A private `.tmp-<id>/` folder in seed_dir (same filesystem, so _publish is an atomic rename); always removed."""
    work = seed_dir / f"{TMP_PREFIX}{library.sanitize_filename(track.id)}"
    key = os.path.realpath(work)
    with _claims_lock:
        _active_work.add(key)
    try:
        shutil.rmtree(work, ignore_errors=True)  # leftover from a crashed run
        work.mkdir(parents=True)
        yield work
    finally:
        shutil.rmtree(work, ignore_errors=True)
        with _claims_lock:
            _active_work.discard(key)


def _publish(src: Path, dest: Path) -> None:
    """Atomically move the finished file into place, never replacing a file someone else created meanwhile."""
    if dest.exists():
        raise FileExistsError(f"refusing to overwrite {dest.name}")
    os.replace(src, dest)


def sweep_temp_dirs(seed_dir: Path) -> None:
    """Delete `.tmp-*` work folders left in `seed_dir` by a crashed or killed run (not ones in use right now)."""
    try:
        stale = [p for p in seed_dir.glob(f"{TMP_PREFIX}*") if p.is_dir()]
    except OSError:
        return
    with _claims_lock:
        stale = [p for p in stale if os.path.realpath(p) not in _active_work]
    for p in stale:
        shutil.rmtree(p, ignore_errors=True)


@contextmanager
def _claimed_path(seed_dir: Path, track: Track) -> Iterator[Path]:
    """Pick the target .mp3 path for `track` and reserve it while the file is being built.

    library.track_path only sees files already on disk, so two tracks with the same "Artists - Title"
    (e.g. single and album versions) downloading at once would otherwise both get the plain name.
    Nothing is written at the path until _publish renames the finished file onto it.
    """
    tid = track.id
    with _claims_lock:
        path = library.track_path(seed_dir, track)
        if _claims.get(path, tid) != tid:
            stem = path.stem
            for n in (6, 10, len(tid)):
                path = path.with_name(f"{stem} [{tid[:n]}].mp3")
                if _claims.get(path, tid) == tid and not path.exists():
                    break
        if path.exists():  # find_existing would have caught a same-ID file; never clobber anything else
            raise FileExistsError(f"refusing to overwrite {path.name}")
        _claims[path] = tid
    try:
        yield path
    finally:
        with _claims_lock:
            _claims.pop(path, None)


def run_batch(
    tracks: list[Track],
    seed: Track,
    seed_dir: Path,
    workers: int = 2,
    on_update: Callable[[Track, str], None] | None = None,
    *,
    tolerance_s: float = 15.0,
    cancel: threading.Event | None = None,
) -> list[DownloadResult]:
    """Process `tracks` `workers` at a time; returns results in the same order as `tracks`.

    Setting `cancel` (from any thread) or Ctrl-C (main thread) cancels everything not yet started and stops
    in-flight tracks before their next network step, then waits for running downloads to finish (a second
    Ctrl-C stops waiting). Cancelled tracks come back as "failed" with reason CANCELLED. `on_update` gets
    (track, text) for every step, from worker threads.
    """

    def notify(track: Track, text: str) -> None:
        if on_update:
            try:
                on_update(track, text)
            except Exception:  # noqa: BLE001 - a display glitch must not fail a download
                pass

    unique = list({t.id: t for t in tracks}.values())  # the same track twice would race on one file
    for t in unique:
        notify(t, "queued")

    sweep_temp_dirs(seed_dir)
    index = library.LibraryIndex(seed_dir.parent)  # scanned once, here
    prefetch = _Prefetcher(t for t in unique if not index.find(t.id))  # files on disk need no lookup
    cancel = cancel if cancel is not None else threading.Event()
    pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="musicreco")
    run = partial(process_track, cancel=cancel, index=index, prefetch=prefetch, tolerance_s=tolerance_s)
    futures: dict[str, Future[DownloadResult]] = {
        t.id: pool.submit(run, t, seed, seed_dir, on_status=partial(notify, t)) for t in unique
    }

    def stop() -> None:
        cancel.set()
        prefetch.close()
        pool.shutdown(wait=False, cancel_futures=True)
        for t in unique:
            if not futures[t.id].done():
                notify(t, "cancelling")
        try:
            _wait_all(futures.values())
        except KeyboardInterrupt:
            pass  # stop waiting; anything still running finishes in the background

    try:
        _wait_all(futures.values(), cancel)
        if cancel.is_set():
            stop()
    except KeyboardInterrupt:
        stop()
    finally:
        prefetch.close()
        pool.shutdown(wait=False)

    by_id: dict[str, DownloadResult] = {}
    for t in unique:
        fut = futures[t.id]
        if fut.done() and not fut.cancelled():
            by_id[t.id] = fut.result()
        else:
            by_id[t.id] = DownloadResult(track=t, status="failed", reason=CANCELLED)
            notify(t, "cancelled")
    return [by_id[t.id] for t in tracks]


def finalize_crate(seed: Track, seed_dir: Path, results: list[DownloadResult]) -> list[DownloadResult]:
    """Crate-wide pass over the results that have a file: energy 1-10 (POPM stars) and set order (TRCK n/total).

    Pass every file in the crate (including ones from earlier runs) so track numbers never collide.
    Fills `energy_rating` / `order` on those results (only where the tag was written) and returns `results`.
    Never raises: a file that can't be tagged is logged and skipped.
    """
    crate: dict[str, DownloadResult] = {}
    for r in results:
        if r.status in FILE_STATUSES and r.path is not None and r.path.is_file():
            crate.setdefault(r.track.id, r)
    if not crate:
        return results
    tracks = [r.track for r in crate.values()]

    try:
        ratings = mixing.energy_ratings(tracks)
    except Exception as e:  # noqa: BLE001
        log.warning("energy ratings failed for %s: %s", seed_dir.name, _describe(e))
        ratings = {}
    for tid, rating in ratings.items():
        if (r := crate.get(tid)) is not None:
            _apply(r, "energy_rating", rating, tagging.set_energy, rating)

    try:
        ordered = [t for t in mixing.order_set(seed, tracks) if t.id in crate]
    except Exception as e:  # noqa: BLE001
        log.warning("set order failed for %s: %s", seed_dir.name, _describe(e))
        return results
    ids = list(dict.fromkeys(t.id for t in ordered))
    ids += [tid for tid in crate if tid not in ids]  # anything order_set dropped goes last
    for n, tid in enumerate(ids, 1):
        _apply(crate[tid], "order", n, tagging.set_track_number, n, len(ids))
    return results


def _apply(result: DownloadResult, attr: str, value: int, write: Callable[..., None], *args: int) -> None:
    """Write one crate tag to result.path (keeping its mtime) and record `value`; log (don't raise) on failure."""
    try:
        with library.preserve_mtime(result.path):
            write(result.path, *args)
    except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
        log.warning("couldn't write %s to %s: %s", attr, result.path.name if result.path else "?", _describe(e))
        return
    setattr(result, attr, value)


def complete_batch(
    seed: Track,
    recs: list[Track],
    scores: dict[str, Score],
    ticked_ids: Iterable[str],
    seed_dir: Path,
    results: list[DownloadResult],
    *,
    bpm_tolerance: float = 0.10,
) -> CompletedBatch:
    """Everything after run_batch, shared by the CLI and the app. Holds library.crate_lock(seed_dir).

    1. finalize_crate over this run's results plus the files already in the folder from earlier runs (every
       track the crate knows: seed, `recs`, and library.load_crate_tracks), so set numbers never collide;
    2. tracks.csv (seed + every rec, ticked or not, with scores; plus rows for earlier files whose track
       tunebat no longer recommends), merged with the previous CSV;
    3. `.m3u8` + rekordbox `.xml` playlists in set order;
    4. skipped.txt for this run's skipped/failed tracks (cancelled ones excluded);
    5. .seedbox/crate.json (raw tunebat data for the seed + recs).
    `recs` are the unique recommendations in display order; `scores` may lack entries (computed with
    `bpm_tolerance`, which also drives the CSV rule column). Never raises: a step that fails is logged and
    recorded in `errors`, and the remaining steps still run.
    """
    ticked = set(ticked_ids)
    done = CompletedBatch(results=results, crate=list(results))

    def step(what: str, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 - the other files are still worth writing
            log.warning("%s failed for %s: %s", what, seed_dir.name, _describe(e))
            done.errors.append(f"{what}: {_describe(e)}")
            return None

    with library.crate_lock(seed_dir):
        known = step("crate metadata", library.load_crate_tracks, seed_dir) or {}
        everything = {**known, **{t.id: t for t in [seed, *recs]}}
        earlier = step("crate scan", _earlier_files, seed_dir, list(everything.values()), results) or []
        done.crate = finalize_crate(seed, seed_dir, results + earlier)
        rows = _csv_rows(seed, recs, scores, ticked, done.crate, bpm_tolerance)
        done.csv_path = step("tracks.csv", library.write_tracks_csv, seed_dir, seed, rows)
        done.playlist_path = step("playlist", library.write_playlist, seed_dir)
        done.xml_path = step("rekordbox XML", library.write_rekordbox_xml, seed_dir)
        real_problems = [r for r in results if r.reason != CANCELLED]  # cancelled only means Ctrl-C / Cancel
        done.skipped_path = step("skipped.txt", library.append_skipped, seed_dir, seed, real_problems)
        done.crate_json_path = step("crate.json", library.write_crate_json, seed_dir, seed, recs)
    return done


def _earlier_files(seed_dir: Path, tracks: list[Track], results: list[DownloadResult]) -> list[DownloadResult]:
    """`exists` results for crate tracks not processed this run whose file is already in seed_dir."""
    processed = {r.track.id for r in results}
    index = library.LibraryIndex(seed_dir)
    return [
        DownloadResult(track=t, status="exists", path=found[0], reason="from an earlier run")
        for t in tracks
        if t.id not in processed and (found := index.find(t.id))
    ]


def _csv_rows(
    seed: Track,
    recs: list[Track],
    scores: dict[str, Score],
    ticked: set[str],
    crate: list[DownloadResult],
    tol: float,
) -> list[library.CsvRow]:
    """tracks.csv rows: the seed, every recommendation (result None if not processed), then crate files of
    tracks that aren't recommended any more."""
    by_id = {r.track.id: r for r in crate}
    rows = [library.CsvRow(seed, "seed", True, "seed", seed.id in ticked, by_id.get(seed.id))]
    listed = {seed.id}

    def add(t: Track, result: DownloadResult | None, s: Score | None) -> None:
        listed.add(t.id)
        s = s or mixing.score(seed, t, tol)
        reason = mixing.passes_rule(seed, t, tol)[1]
        rows.append(library.CsvRow(t, "r", s.passed, reason, t.id in ticked, result, score=s))

    for t in recs:
        if t.id not in listed:
            add(t, by_id.get(t.id), scores.get(t.id))
    for r in crate:
        if r.track.id not in listed:
            add(r.track, r, None)
    return rows


def _wait_all(futures: Iterable[Future], cancel: threading.Event | None = None) -> None:
    """Block until every future is done (or `cancel` is set); short timeouts keep Ctrl-C responsive.

    Checks done() itself: futures cancelled by shutdown(cancel_futures=True) never count as done for wait().
    """
    pending = [f for f in futures if not f.done()]
    while pending and not (cancel is not None and cancel.is_set()):
        wait(pending, timeout=0.25)
        pending = [f for f in pending if not f.done()]


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _describe(e: BaseException) -> str:
    """One readable line for an exception (yt-dlp messages carry ANSI colours and an 'ERROR:' prefix)."""
    text = _ANSI.sub("", str(e)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    line = text.splitlines()[0].strip() if text else ""
    if not line:
        return type(e).__name__
    return line if len(line) <= 200 else line[:199] + "…"
