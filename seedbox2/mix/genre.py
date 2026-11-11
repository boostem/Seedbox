"""A track's genre family for the transition styles (#57): the user's choice, else the genre tag, else CLAP zero-shot.

The families are in `seedbox2/mix/styles/genres.toml`: each has the words that put a tag in it and the prompts CLAP
compares the track's audio with. A tag matches a family when one of its words is in the tag as whole words; the longest
word wins ("Tech House" is house, "Rap/Trap" is trap). CLAP's zero-shot pick is a softmax over the families (each
family's best prompt, the mean over the track's embedded windows); it counts only at `CLAP_MIN` and over. Its prompts'
vectors come from the `clap_text` job: `warm(jobs)` queues the missing ones (the server does at start), and until they
are there CLAP gives no genre. On 56 golden tracks the text alone found the genre 43 % of the time (README, CLAP), so
it is the last resort, and the tag or the user always wins over it.

The user's choice is the `genre_override` analysis (`set_override`, `clear_override`), as the key's is.
"""

from __future__ import annotations

import functools
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .styles import FOLDER

GENRES_FILE = FOLDER / "genres.toml"
OVERRIDE = "genre_override"
OVERRIDE_VERSION = "1"
CLAP_MIN = 0.4  # the zero-shot pick's probability needed to count
CLAP_SCALE = 100.0  # CLAP's logit scale: cosines are close together, the softmax needs them spread


@dataclass(frozen=True)
class Family:
    id: str
    label: str
    tags: tuple[str, ...]
    prompts: tuple[str, ...]


def _norm(text: str) -> str:
    t = text.lower().replace("&", " and ").replace("'n'", " and ").replace(" n ", " and ")
    return " ".join(re.sub(r"[-_/,;:+()\[\]]", " ", t).split())


def load_families(path: Path = GENRES_FILE) -> tuple[Family, ...]:
    raw = tomllib.loads(path.read_text("utf-8"))
    out = []
    for fid, f in raw.items():
        if not (isinstance(f, dict) and isinstance(f.get("label"), str) and f.get("tags") and f.get("prompts")):
            raise ValueError(f"genres.toml: [{fid}] needs label, tags and prompts")
        out.append(Family(fid, f["label"], tuple(_norm(t) for t in f["tags"]), tuple(f["prompts"])))
    return tuple(out)


@functools.cache
def families() -> tuple[Family, ...]:
    return load_families()


def family(fid: str) -> Family | None:
    return next((f for f in families() if f.id == fid), None)


def from_tag(tag: str | None) -> str | None:
    """The family a genre tag names, or None."""
    text = _norm(tag or "")
    if not text:
        return None
    best: tuple[int, str] | None = None
    for f in families():
        for word in f.tags:
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) and (best is None or len(word) > best[0]):
                best = (len(word), f.id)
    return best[1] if best else None


def zero_shot(audio: np.ndarray, prompts: dict[str, np.ndarray]) -> list[dict]:
    """Families by probability, from a track's window vectors [n, 512] and each family's prompt vectors [k, 512]
    (all normalised: a cosine is a dot product)."""
    audio = np.atleast_2d(np.asarray(audio, np.float32))
    ids = [fid for fid, v in prompts.items() if np.size(v)]
    if audio.size == 0 or not ids:
        return []
    sims = np.array([float(np.max(np.atleast_2d(prompts[fid]) @ audio.T, axis=0).mean()) for fid in ids])
    z = np.exp(CLAP_SCALE * (sims - sims.max()))
    p = z / z.sum()
    return sorted(({"genre": fid, "p": round(float(x), 4)} for fid, x in zip(ids, p)), key=lambda r: -r["p"])


def _prompt_vectors() -> dict[str, np.ndarray]:
    """The prompts' vectors that are cached, per family."""
    from seedbox2.analysis import vibe

    out = {}
    for f in families():
        vs = [np.load(p, allow_pickle=False) for p in (vibe.text_path(t) for t in f.prompts) if p.is_file()]
        if vs:
            out[f.id] = np.stack(vs)
    return out


def warm(jobs: Any) -> int:
    """Queue the `clap_text` jobs for prompts not embedded yet; how many."""
    from seedbox2.analysis import vibe

    n = 0
    for f in families():
        for text in f.prompts:
            if not vibe.text_path(text).is_file():
                jobs.submit("clap_text", {"text": vibe.normalise_text(text)}, priority="library")
                n += 1
    return n


def clap_guess(lib: Any, hash_: str) -> list[dict]:
    """The zero-shot ranking for a track, empty without its embeddings or the prompts' vectors."""
    from seedbox2.analysis import vibe

    path = vibe.current(lib, hash_)
    if path is None:
        return []
    prompts = _prompt_vectors()
    return zero_shot(vibe.read_embeddings(path)["vectors"], prompts) if prompts else []


def set_override(lib: Any, hash_: str, genre: str) -> dict:
    if family(genre) is None:
        raise ValueError(f"not a genre family: {genre!r} (one of {[f.id for f in families()]})")
    data = {"genre": genre}
    lib.put_analysis(hash_, OVERRIDE, OVERRIDE_VERSION, data)
    return data


def clear_override(lib: Any, hash_: str) -> None:
    lib.delete_analysis(hash_, OVERRIDE)


def tag_of(lib: Any, hash_: str) -> str | None:
    """The track's genre tag, as the library table shows it."""
    from seedbox2.library.view import hint_genre

    return hint_genre((lib.track(hash_) or {}).get("tags") or {}) or None


def effective(lib: Any, hash_: str, *, clap: bool = True) -> dict:
    """`{genre, label, from, p}`: `from` is user, tag, clap or unknown (genre None)."""
    row = lib.analysis(hash_, OVERRIDE)
    if row and family(row["data"].get("genre", "")):
        g, src, p = row["data"]["genre"], "user", None
    else:
        g, src, p = from_tag(tag_of(lib, hash_)), "tag", None
        if g is None and clap:
            top = clap_guess(lib, hash_)
            if top and top[0]["p"] >= CLAP_MIN:
                g, src, p = top[0]["genre"], "clap", top[0]["p"]
        if g is None:
            src = "unknown"
    f = family(g) if g else None
    return {"genre": g, "label": f.label if f else None, "from": src, "p": p}


def status(lib: Any, hash_: str) -> dict:
    """`GET /analysis/{hash}/genre`: the effective family, the tag, CLAP's ranking and the families to choose from."""
    tag = tag_of(lib, hash_)
    return {"effective": effective(lib, hash_), "tag": tag, "tag_genre": from_tag(tag),
            "clap": clap_guess(lib, hash_)[:5], "families": [{"id": f.id, "label": f.label} for f in families()]}  # fmt: skip
