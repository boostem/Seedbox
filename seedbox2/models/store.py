"""Model files on disk: `<cache>/models/<name>/<version>/<file>`, downloaded with resume and checked by SHA-256.

A download writes `<file>.part` next to where the file goes and continues it with an HTTP Range request after an
interruption (a dropped connection, a quit app). The finished file is hashed, and only a file whose SHA-256 matches
the registry is moved into place, so a file at its final path is always a verified one; `installed()` then only
checks it is there with the right size. Once a version is in place, the model's other versions are removed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from seedbox2.models.registry import ModelSpec

log = logging.getLogger(__name__)

MODELS_SUBDIR = "models"
CHUNK = 1 << 16  # bytes per network read (a dropped connection loses at most the block being read)
HASH_CHUNK = 1 << 20
CONNECT_TIMEOUT_S = 15
READ_TIMEOUT_S = 60
RETRIES = 3  # attempts per download; each one resumes where the last stopped
RETRY_WAIT_S = 2.0
USER_AGENT = "Seedbox2-model-manager"

Progress = Callable[[int, int], None]  # (bytes done, bytes in total)


class DownloadError(Exception):
    """The download failed (network, HTTP status); a later one resumes from the partial file."""


class ChecksumMismatch(DownloadError):
    """The downloaded file isn't the one the registry pins; it was deleted."""


class DownloadCancelled(DownloadError):  # noqa: N818 - the reason, not an error
    """Stopped by the cancel event; the partial file stays for the next attempt."""


def models_dir(cache_dir: Path | None = None) -> Path:
    """The models folder: `models/` in Seedbox 2's cache folder ($SEEDBOX_CACHE_DIR)."""
    if cache_dir is None:
        from seedbox2 import paths

        cache_dir = paths.folders()["cache"]
    return Path(cache_dir) / MODELS_SUBDIR


def model_path(spec: ModelSpec, root: Path) -> Path:
    return Path(root) / spec.name / spec.version / spec.file


def part_path(spec: ModelSpec, root: Path) -> Path:
    path = model_path(spec, root)
    return path.with_name(path.name + ".part")


def installed(spec: ModelSpec, root: Path) -> bool:
    """The verified file is in place (only verified files are moved there, so the size check is enough)."""
    try:
        return model_path(spec, root).stat().st_size == spec.size
    except OSError:
        return False


def partial_bytes(spec: ModelSpec, root: Path) -> int:
    try:
        return part_path(spec, root).stat().st_size
    except OSError:
        return 0


def sha256_of(path: Path, cancel: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK):
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled
            h.update(chunk)
    return h.hexdigest()


def verify(spec: ModelSpec, root: Path) -> bool:
    """Hash the installed file again (slow: reads it all). False if it is missing or changed."""
    path = model_path(spec, root)
    return path.is_file() and sha256_of(path) == spec.sha256


def delete(spec: ModelSpec, root: Path) -> bool:
    """Remove every version of the model, partial downloads too. True if there was anything to remove."""
    folder = Path(root) / spec.name
    if not folder.exists():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True


def _remove_other_versions(spec: ModelSpec, root: Path) -> None:
    folder = Path(root) / spec.name
    for child in folder.iterdir() if folder.is_dir() else ():
        if child.name != spec.version:
            shutil.rmtree(child, ignore_errors=True)


def download(spec: ModelSpec, root: Path, *, progress: Progress | None = None, cancel: threading.Event | None = None,
             session: Any = None, retries: int | None = None, retry_wait_s: float | None = None) -> Path:
    """Download the model into `root` (resuming a partial file), verify its SHA-256 and move it into place.

    Returns its path (at once if it is already installed). Raises DownloadCancelled when `cancel` is set,
    ChecksumMismatch (the partial file is deleted) or DownloadError after `retries` attempts.
    """
    import requests

    retries = RETRIES if retries is None else retries
    retry_wait_s = RETRY_WAIT_S if retry_wait_s is None else retry_wait_s
    path = model_path(spec, root)
    if installed(spec, root):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = part_path(spec, root)
    http = session or requests.Session()
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        if attempt:
            log.info("retrying the %s download (%s)", spec.name, last)
            if cancel is not None and cancel.wait(retry_wait_s):
                raise DownloadCancelled
        try:
            _fetch(spec, part, http, progress, cancel)
            break
        except DownloadCancelled:
            raise
        except (requests.RequestException, OSError, DownloadError) as e:
            last = e
    else:
        raise DownloadError(f"couldn't download {spec.title}: {last}") from last

    digest = sha256_of(part, cancel)
    if digest != spec.sha256:
        part.unlink(missing_ok=True)
        raise ChecksumMismatch(f"{spec.title}: the download's SHA-256 is {digest}, expected {spec.sha256}")
    os.replace(part, path)
    _remove_other_versions(spec, root)
    log.info("downloaded %s %s to %s", spec.name, spec.version, path)
    return path


def _fetch(spec: ModelSpec, part: Path, http: Any, progress: Progress | None,
           cancel: threading.Event | None) -> None:  # fmt: skip
    have = part.stat().st_size if part.exists() else 0
    if have > spec.size:  # not the file we want: start again
        part.unlink(missing_ok=True)
        have = 0
    if have == spec.size:
        return
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    timeout = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)
    with http.get(spec.url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as r:
        if r.status_code == 416 and have:  # the range starts at or past the end: what we have is suspect
            part.unlink(missing_ok=True)
            raise DownloadError("the server refused to resume; starting again")
        if r.status_code not in (200, 206):
            raise DownloadError(f"HTTP {r.status_code} from {spec.url}")
        if have and r.status_code == 200:  # the server ignored the Range: the body is the whole file
            have = 0
        mode = "ab" if have else "wb"
        done = have
        last_report = 0.0
        if progress:
            progress(done, spec.size)
        with open(part, mode) as f:
            for chunk in r.iter_content(CHUNK):
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if done > spec.size:
                    raise DownloadError(f"{spec.url} is larger than the {spec.size} bytes expected")
                now = time.monotonic()
                if progress and (now - last_report >= 0.1 or done == spec.size):
                    last_report = now
                    progress(done, spec.size)
    if done < spec.size:
        raise DownloadError(f"the connection closed after {done} of {spec.size} bytes")
