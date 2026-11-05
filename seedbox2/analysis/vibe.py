"""The vibe (#48): CLAP embeddings of a few windows per track, and search over them.

- `choose_windows`: 3-5 stretches of 10 s: `intro`, `main` (the main drop), `outro`, and when the track has room,
  `breakdown` and `second` (a second high point). From the `sections` analysis when it's there, else from the energy
  curve.
- The `clap` job embeds them into `<cache>/embeddings/<hash>.npz` (the `embedding` artifact).
- `clap_text` embeds a text in a worker and caches the vector on disk. It opens the text encoder for that one job and
  closes it: the graph is large, and any of the light workers could otherwise keep a copy.
- `Index` holds every track's vectors for a query: a brute-force dot product is about 5 ms over 5,000 tracks.
- `similar` and `search` rank tracks, with `compatible` filtering by BPM and key.

`similar` compares each window of the seed with the same window of the others, so an intro resembles an intro. `search`
takes a track's best window, since a text like "vocal house" may only be true of its main part.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from seedbox2.analysis import clap, energy, waveform
from seedbox2.jobs import register
from seedbox2.models import runtime, store
from seedbox2.models.registry import MODELS

log = logging.getLogger(__name__)

AUDIO_MODEL = "clap_audio"
TEXT_MODEL = "clap_text"
TOKENIZER = "clap_tokenizer"
ARTIFACT_KIND = "embedding"
SECTIONS_ANALYSER = "sections"
WINDOW_NAMES = ("intro", "main", "outro", "breakdown", "second")
MIN_APART_S = 30.0  # the optional windows must say something new
TEXT_TIMEOUT_S = 60.0
DEFAULT_BPM_PCT = 6.0  # the tempo change a key-locked stretch does without artefacts; DJs keep 86 % under 5 %
RANK_SLICE = 200
PROMPT = "this is {} music"


class VibeError(ValueError):
    pass


class ModelNotReady(VibeError):
    """The CLAP models aren't downloaded."""


# --- which windows --------------------------------------------------------------------------------------------------


def _rolling(curve: np.ndarray, bars: np.ndarray, seconds: float) -> tuple[np.ndarray, np.ndarray]:
    """Candidate starts (the bars') and the mean energy of the `seconds` after each. Starts that don't fit are left out."""
    starts = bars[:-1]
    fits = starts + seconds <= bars[-1] + 1e-6
    weights = curve * np.diff(bars)
    total = np.concatenate([[0.0], np.cumsum(weights)])
    ends = np.interp(starts[fits] + seconds, bars, total)
    return starts[fits], (ends - total[:-1][fits]) / seconds


def _audible(bars: np.ndarray, curve: np.ndarray) -> tuple[float, float]:
    on = np.flatnonzero(curve > 0.02)
    return (float(bars[on[0]]), float(bars[on[-1] + 1])) if on.size else (float(bars[0]), float(bars[-1]))


def choose_windows(energy_data: dict, sections: list[dict] | None = None, seconds: float = clap.WINDOW_S) -> list[dict]:
    """`[{name, start_s}]` in WINDOW_NAMES' order, each `seconds` long and on a bar. A track too short for a window has
    only `main`, from the start."""
    bars = np.asarray(energy_data["bar_s"], np.float64)
    curve = np.asarray(energy_data["curve"], np.float64)
    sections = sections or []
    starts, level = _rolling(curve, bars, seconds)
    if starts.size == 0:
        return [{"name": "main", "start_s": 0.0}]

    def best(mask: np.ndarray, lowest: bool = False) -> float | None:
        if not mask.any():
            return None
        pick = np.where(mask, level, np.inf if lowest else -np.inf)
        return float(starts[int(np.argmin(pick) if lowest else np.argmax(pick))])

    def inside(kind: str) -> np.ndarray:
        mask = np.zeros(starts.size, bool)
        for s in sections:
            if s.get("kind") == kind:
                mask |= (starts >= float(s["start_s"]) - 1e-3) & (starts + seconds <= float(s["end_s"]) + 1e-3)
        return mask

    def apart(*taken: float | None) -> np.ndarray:
        mask = np.ones(starts.size, bool)
        for t in taken:
            if t is not None:
                mask &= np.abs(starts - t) >= MIN_APART_S
        return mask

    first, last = _audible(bars, curve)
    main = best(inside("drop"))
    main = main if main is not None else best(np.ones(starts.size, bool))
    intro = float(starts[int(np.searchsorted(starts, first - 1e-6).clip(0, starts.size - 1))])
    in_outro = inside("outro")
    tail = np.flatnonzero(starts + seconds <= last + 1e-6)
    outro = float(starts[np.flatnonzero(in_outro)[0]]) if in_outro.any() else float(starts[tail[-1] if tail.size else -1])
    out = [("main", main)]
    if abs(intro - main) >= seconds:
        out.append(("intro", intro))
    if abs(outro - main) >= seconds and abs(outro - intro) >= seconds:
        out.append(("outro", outro))
    middle = (starts >= first + MIN_APART_S) & (starts + seconds <= last - MIN_APART_S)
    busy = np.flatnonzero(level >= 0.6 * np.percentile(level, 90))  # a breakdown is a dip between the busy parts
    between = (starts >= starts[busy[0]]) & (starts <= starts[busy[-1]]) if busy.size else np.zeros(starts.size, bool)
    breakdown = best(inside("breakdown") & apart(main, intro, outro))
    if breakdown is None:
        breakdown = best(middle & between & apart(main, intro, outro), lowest=True)
    if breakdown is not None:
        out.append(("breakdown", breakdown))
    second = best(middle & apart(main, intro, outro, breakdown))
    if second is not None:
        out.append(("second", second))
    order = {n: i for i, n in enumerate(WINDOW_NAMES)}
    return [{"name": n, "start_s": round(t, 3)} for n, t in sorted(out, key=lambda w: order[w[0]])]


def regions(energy_data: dict) -> list[dict]:
    """Rough sections from the energy curve, until the track has real ones: `intro` until the curve first reaches 60 %
    of its busy level (its 90th percentile), `outro` after it last does, `main` between."""
    bars = np.asarray(energy_data["bar_s"], np.float64)
    curve = np.asarray(energy_data["curve"], np.float64)
    if curve.size == 0:
        return []
    high = np.flatnonzero(curve >= 0.6 * np.percentile(curve, 90))
    a, b = (int(high[0]), int(high[-1]) + 1) if high.size else (0, curve.size)
    out = [("intro", 0, a), ("main", a, b), ("outro", b, curve.size)]
    return [{"kind": k, "start_s": float(bars[i]), "end_s": float(bars[j])} for k, i, j in out if j > i]


def spans_of(lib: Any, hash_: str, energy_data: dict | None) -> list[dict]:
    """The track's sections, or `regions` of its energy until it has them."""
    return sections_of(lib, hash_) or (regions(energy_data) if energy_data else [])


def audio_ready() -> bool:
    return store.installed(MODELS[AUDIO_MODEL], store.models_dir())


# --- audio ----------------------------------------------------------------------------------------------------------


def read_window(path: Path | str, start_s: float, seconds: float = clap.WINDOW_S) -> np.ndarray:
    """A stretch of the file as 48 kHz mono, seeking rather than decoding what comes before it."""
    return np.concatenate(list(waveform.decode_chunks(
        path, clap.SAMPLE_RATE * int(seconds) + clap.SAMPLE_RATE, sample_rate=clap.SAMPLE_RATE, channels=1,
        start_s=start_s, seconds=seconds)) or [np.zeros(0, np.float32)])  # fmt: skip


def embeddings_dir() -> Path:
    from seedbox2.paths import folders

    return folders()["cache"] / "embeddings"


def artifact_path(hash_: str, root: Path | None = None) -> Path:
    return (embeddings_dir() if root is None else root) / hash_[:2] / f"{hash_}.npz"


def model_version() -> str:
    return f"{AUDIO_MODEL}:{MODELS[AUDIO_MODEL].version}"


def write_embeddings(path: Path, windows: list[dict], vectors: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial.npz")
    try:
        np.savez(partial, vectors=vectors.astype(np.float32), names=np.array([w["name"] for w in windows]),
                 starts=np.array([w["start_s"] for w in windows], np.float64))  # fmt: skip
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def read_embeddings(path: Path | str) -> dict:
    with np.load(path, allow_pickle=False) as z:
        return {"vectors": z["vectors"], "names": [str(n) for n in z["names"]], "starts": z["starts"].tolist()}


def current(lib: Any, hash_: str) -> Path | None:
    for art in lib.artifacts(hash_, ARTIFACT_KIND):
        if art["model_version"] == model_version() and Path(art["path"]).is_file():
            return Path(art["path"])
    return None


def sections_of(lib: Any, hash_: str) -> list[dict]:
    """The track's sections (#46), newest analysis, or none yet."""
    row = lib.analysis(hash_, SECTIONS_ANALYSER)
    sections = row["data"].get("sections") if row and isinstance(row["data"], dict) else None
    return sections if isinstance(sections, list) else []


@register("clap", pool="light")
def clap_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"hash": ...}`: embeds the windows chosen from the track's energy and sections."""
    hash_ = payload["hash"]
    lib = ctx.library
    if lib.track(hash_) is None:
        raise VibeError(f"no track {hash_}")
    if current(lib, hash_) is not None:
        return {"hash": hash_, "cached": True}
    row = lib.analysis(hash_, energy.ANALYSER, energy.VERSION)
    if row is None:
        raise VibeError("the track has no energy analysis yet")
    session = ctx.session_or_none(AUDIO_MODEL)
    if session is None:
        return {"hash": hash_, "skipped": "model_missing"}
    sources = [Path(p) for p in lib.paths(hash_) if Path(p).is_file()]
    if not sources:
        raise VibeError("none of the track's files is there any more")
    windows = choose_windows(row["data"], sections_of(lib, hash_))
    vectors = []
    for i, w in enumerate(windows):
        vectors.append(clap.embed_audio(session, read_window(sources[0], w["start_s"])))
        ctx.progress((i + 1) / len(windows))
    path = artifact_path(hash_)
    write_embeddings(path, windows, np.stack(vectors))
    lib.put_artifact(hash_, ARTIFACT_KIND, model_version(), path)
    return {"hash": hash_, "windows": [w["name"] for w in windows], "model": model_version()}


def submit(jobs: Any, hash_: str, *, priority: int | str = "library") -> dict:
    return jobs.submit("clap", {"hash": hash_}, priority=priority)


def on_job_finished(jobs: Any, lib: Any, row: dict) -> None:
    """The supervisor's listener: a track gets its embeddings once its energy is known. Later sections don't re-embed."""
    payload = row.get("payload")
    if row.get("kind") != "energy" or row.get("state") != "done" or not isinstance(payload, dict):
        return
    hash_ = payload.get("hash")
    if hash_ and current(lib, hash_) is None and audio_ready():
        submit(jobs, hash_)


def queue_missing(jobs: Any, lib: Any) -> int:
    """Tracks with energy but no embeddings: the model wasn't there when they were analysed."""
    if not audio_ready():
        return 0
    queued = 0
    for t in lib.hashes_without_artifact(ARTIFACT_KIND):
        if lib.analysis(t["hash"], energy.ANALYSER, energy.VERSION) is not None:
            submit(jobs, t["hash"], priority="background")
            queued += 1
    return queued


# --- text -----------------------------------------------------------------------------------------------------------


def text_path(text: str, root: Path | None = None) -> Path:
    key = f"{MODELS[TEXT_MODEL].version}\0{prompt(text)}"
    return (embeddings_dir() if root is None else root) / "text" / f"{hashlib.sha1(key.encode()).hexdigest()}.npy"


def normalise_text(text: str) -> str:
    return " ".join(text.split())


def prompt(text: str) -> str:
    """What the text encoder reads: CLAP was trained on captions, not on bare words (README)."""
    return PROMPT.format(normalise_text(text))


@register("clap_text", pool="light", resumable=False)
def clap_text_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"text": ...}`. The vector goes to a file, not into the job row."""
    text = normalise_text(payload["text"])
    path = text_path(text)
    if path.is_file():
        return {"path": str(path), "cached": True}
    root = store.models_dir()
    if not (store.installed(MODELS[TEXT_MODEL], root) and store.installed(MODELS[TOKENIZER], root)):
        return {"skipped": "model_missing"}
    session = runtime.load_session(TEXT_MODEL, root=root)
    tokenizer = clap.Tokenizer.load(store.model_path(MODELS[TOKENIZER], root))
    vector = clap.embed_text(session, tokenizer, prompt(text))
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial.npy")
    np.save(partial, vector)
    partial.replace(path)
    return {"path": str(path)}


def text_vector(jobs: Any, text: str, timeout: float = TEXT_TIMEOUT_S) -> np.ndarray:
    """The text's vector, from the cache or a worker, ahead of the queue. ModelNotReady if it can't be made."""
    text = normalise_text(text)
    if not text:
        raise VibeError("empty text")
    path = text_path(text)
    if not path.is_file():
        job = jobs.submit("clap_text", {"text": text}, priority="set", front=True)
        row = jobs.wait(job["id"], timeout=timeout) or {}
        if row.get("state") != "done":
            raise VibeError(f"embedding the text didn't finish: {row.get('error') or row.get('state')}")
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        if result.get("skipped"):
            raise ModelNotReady("the CLAP models aren't downloaded")
    return np.load(path, allow_pickle=False)


# --- search ---------------------------------------------------------------------------------------------------------


class Index:
    """Every track's vectors in one matrix, rebuilt from the changed files when the artifact list changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._cache: dict[str, dict] = {}
        self.vectors = np.zeros((0, clap.DIM), np.float32)
        self.owner = np.zeros(0, np.int32)
        self.names: list[str] = []
        self.hashes: list[str] = []

    def refresh(self, lib: Any) -> None:
        rows = [r for r in lib.artifacts_of_kind(ARTIFACT_KIND) if r["model_version"] == model_version()]
        stamp = tuple(sorted((r["hash"], r["created_at"]) for r in rows))
        with self._lock:
            if stamp == self._stamp:
                return
            cache: dict[str, dict] = {}
            for r in rows:
                key = (r["created_at"], r["path"])
                old = self._cache.get(r["hash"])
                if old is not None and old["key"] == key:
                    cache[r["hash"]] = old
                    continue
                try:
                    cache[r["hash"]] = {**read_embeddings(r["path"]), "key": key}
                except (OSError, ValueError, KeyError) as e:
                    log.warning("skipping the embeddings of %s: %s", r["hash"], e)
            self._cache = cache
            self.hashes = list(cache)
            parts = [cache[h]["vectors"] for h in self.hashes]
            self.vectors = np.concatenate(parts) if parts else np.zeros((0, clap.DIM), np.float32)
            self.owner = np.repeat(np.arange(len(parts), dtype=np.int32), [p.shape[0] for p in parts]) if parts else np.zeros(0, np.int32)
            self.names = [n for h in self.hashes for n in cache[h]["names"]]
            self._stamp = stamp

    def windows(self, hash_: str) -> dict | None:
        return self._cache.get(hash_)

    def best_per_track(self, query: np.ndarray) -> np.ndarray:
        """For each track, the best dot product of any of its windows with the query."""
        out = np.full(len(self.hashes), -np.inf, np.float32)
        if self.vectors.size:
            np.maximum.at(out, self.owner, self.vectors @ query)
        return out

    def by_seed(self, seed: dict, window: str | None = None) -> np.ndarray:
        """For each track, the mean over the seed's windows of the similarity with the same window (or the track's best
        window, when it has no such window). `window` limits it to one of the seed's windows."""
        names = np.array(self.names)
        out = np.zeros(len(self.hashes), np.float32)
        used = 0
        for name, v in zip(seed["names"], seed["vectors"], strict=True):
            if window is not None and name != window:
                continue
            used += 1
            sims = self.vectors @ v
            best = np.full(len(self.hashes), -np.inf, np.float32)
            np.maximum.at(best, self.owner, sims)
            same = np.full(len(self.hashes), np.nan, np.float32)
            mask = names == name
            same[self.owner[mask]] = sims[mask]
            out += np.where(np.isnan(same), best, same)
        if not used:
            raise VibeError(f"the track has no {window} window")
        return out / used


def tempo_compatible(a: float, b: float, pct: float = DEFAULT_BPM_PCT) -> bool:
    """Within `pct` percent, also at half or double time."""
    return any(abs(a * f / b - 1) * 100 <= pct for f in (0.5, 1.0, 2.0))


def compatible(row: dict, *, bpm: float | None, bpm_pct: float, camelot: str | None) -> bool:
    """Does a library row (`view.describe`) pass the filters? A filter on something unknown about the track fails."""
    from musicreco.mixing import camelot_adjacent

    if bpm is not None and (not row.get("bpm") or not tempo_compatible(bpm, float(row["bpm"]), bpm_pct)):
        return False
    return camelot is None or (bool(row.get("key")) and camelot_adjacent(camelot, row["key"]))


def rank(lib: Any, index: Index, sims: np.ndarray, *, bpm: float | None = None, bpm_pct: float = DEFAULT_BPM_PCT,
         camelot: str | None = None, limit: int = 20, exclude: Iterable[str] = ()) -> dict:  # fmt: skip
    """The best tracks that pass the filters: `{results: [{hash, similarity, title, artist, bpm, key, energy}],
    candidates, filtered}`."""
    skip = set(exclude)
    order = [int(i) for i in np.argsort(-sims, kind="stable") if index.hashes[int(i)] not in skip]
    results, filtered = [], 0
    for at in range(0, len(order), RANK_SLICE):  # best first, so the library's rows are read only as far as needed
        part = order[at : at + RANK_SLICE]
        rows = {r["hash"]: r for r in lib.view_rows(hashes=[index.hashes[i] for i in part])}
        for i in part:
            row = rows.get(index.hashes[i])
            if row is None or len(results) >= limit:
                continue
            if not compatible(row, bpm=bpm, bpm_pct=bpm_pct, camelot=camelot):
                filtered += 1
                continue
            results.append({"hash": row["hash"], "similarity": round(float(sims[i]), 4), "title": row.get("title"),
                            "artist": row.get("artist"), "bpm": row.get("bpm"), "key": row.get("key"),
                            "energy": row.get("energy")})  # fmt: skip
        if len(results) >= limit:
            break
    return {"results": results, "candidates": len(order), "filtered": filtered}


def similar(lib: Any, index: Index, hash_: str, *, window: str | None = None, use_bpm: bool = True,
            use_key: bool = True, bpm_pct: float = DEFAULT_BPM_PCT, limit: int = 20) -> dict | None:  # fmt: skip
    """The tracks nearest to this one, compatible with its BPM and key unless told otherwise. None if it has no
    embeddings yet."""
    index.refresh(lib)
    seed = index.windows(hash_)
    if seed is None:
        return None
    rows = lib.view_rows(hashes=[hash_])
    row = rows[0] if rows else {}
    bpm = row.get("bpm") if use_bpm else None
    camelot = row.get("key") if use_key else None
    out = rank(lib, index, index.by_seed(seed, window), bpm=bpm, bpm_pct=bpm_pct, camelot=camelot, limit=limit, exclude=[hash_])
    return {"seed": {"hash": hash_, "bpm": bpm, "key": camelot}, **out}


def search(jobs: Any, lib: Any, index: Index, text: str, *, bpm: float | None = None,
           bpm_pct: float = DEFAULT_BPM_PCT, camelot: str | None = None, limit: int = 20) -> dict:  # fmt: skip
    """The tracks whose best window is nearest to the text: what the set assistant (#63) and the gap suggestions (#64)
    call."""
    started = time.monotonic()
    query = text_vector(jobs, text)
    index.refresh(lib)
    out = rank(lib, index, index.best_per_track(query), bpm=bpm, bpm_pct=bpm_pct, camelot=camelot, limit=limit)
    return {"text": normalise_text(text), **out, "seconds": round(time.monotonic() - started, 3)}
