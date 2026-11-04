"""Labelled datasets as `Reference` records: what is known about one track, each field None when not annotated.

| Name | Annotations | Audio |
|---|---|---|
| `giantsteps-tempo` | tempo (the corrected v2 annotations when present) | the 2-minute Beatport previews in `audio/` |
| `giantsteps-key` | key | the previews in `audio/` |
| `harmonix` | beats, downbeats, sections, tempo | not distributed: put your own copies in `audio/<id>.<ext>` |
| `gtzan` | beats, downbeats, tempo (Marchand & Peeters) | the 1,000 30-second GTZAN excerpts, from one 1.2 GB archive |
| `corpus` | anything in the annotation JSON (`annotate.py`) | `<name>.<ext>` next to `<name>.json` |

`python -m seedbox2.eval fetch` clones the public annotation repositories and downloads the GiantSteps previews.
"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".aiff", ".aif", ".ogg", ".opus")


@dataclass
class Reference:
    dataset: str
    track: str
    audio: Path
    bpm: float | None = None
    key: str | None = None  # "A minor", "Eb major" (mir_eval.key notation)
    beats: list[float] | None = None
    downbeats: list[float] | None = None
    sections: list[tuple[float, str]] | None = None  # (start s, label); a last "end" label marks where the music ends
    vocals: list[tuple[float, float]] | None = None  # sung spans (s); [] means annotated as instrumental
    duration_s: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Dataset:
    name: str
    env: str  # environment variable with its folder
    repo: str | None  # public annotation repository that `fetch` clones
    load: Callable[[Path], Iterator[Reference]]
    describe: str


def _audio_for(folder: Path, stem: str) -> Path | None:
    for ext in AUDIO_EXTS:
        p = folder / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _float_or_none(s: str) -> float | None:
    try:
        v = float(s.split()[0])
    except (ValueError, IndexError):
        return None
    return v if v > 0 else None  # GiantSteps v2 marks tracks without a steady tempo as 0


def load_giantsteps_tempo(root: Path) -> Iterator[Reference]:
    v1, v2 = root / "annotations" / "tempo", root / "annotations_v2" / "tempo"
    for ann in sorted(v1.glob("*.bpm")):
        track = ann.name[: -len(".bpm")]  # "1030011.LOFI"
        audio = _audio_for(root / "audio", track)
        if audio is None:
            continue
        better = v2 / ann.name
        bpm = _float_or_none(_read_text(better if better.is_file() else ann))
        if bpm is not None:
            yield Reference("giantsteps-tempo", track, audio, bpm=bpm)


def load_giantsteps_key(root: Path) -> Iterator[Reference]:
    for ann in sorted((root / "annotations" / "key").glob("*.key")):
        track = ann.name[: -len(".key")]
        audio = _audio_for(root / "audio", track)
        key = normalise_key(_read_text(ann))
        if audio is not None and key is not None:
            yield Reference("giantsteps-key", track, audio, key=key)


def load_harmonix(root: Path) -> Iterator[Reference]:
    ds = root / "dataset"
    bpm = {}
    meta = ds / "metadata.csv"
    if meta.is_file():
        with meta.open(newline="", encoding="utf-8") as f:
            bpm = {r["File"]: _float_or_none(r.get("BPM", "")) for r in csv.DictReader(f)}
    audio_dir = Path(os.environ.get("SEEDBOX2_HARMONIX_AUDIO") or root / "audio")
    for ann in sorted((ds / "beats_and_downbeats").glob("*.txt")):
        track = ann.stem
        audio = _audio_for(audio_dir, track)
        if audio is None:
            continue
        beats, downbeats = [], []
        for line in _read_text(ann).splitlines():
            parts = line.split()
            if len(parts) >= 2:
                beats.append(float(parts[0]))
                if parts[1] in ("1", "1.0"):
                    downbeats.append(float(parts[0]))
        sections = None
        seg = ds / "segments" / f"{track}.txt"
        if seg.is_file():
            sections = [(float(t), lab) for t, lab in (ln.split(maxsplit=1) for ln in _read_text(seg).splitlines()
                                                        if len(ln.split()) >= 2)]
        yield Reference("harmonix", track, audio, bpm=bpm.get(track), beats=beats, downbeats=downbeats,
                        sections=sections)


def load_gtzan(root: Path) -> Iterator[Reference]:
    """GTZAN genre excerpts with the beat/downbeat annotations of Marchand & Peeters (`<time> <beat in bar>` lines).
    Beat This!'s checkpoints (final0, small0) were trained on everything but GTZAN, which makes it the fair test."""
    for ann in sorted((root / "beats").glob("gtzan_*.beats")):
        _, genre, num = ann.stem.split("_", 2)  # gtzan_blues_00000 -> blues.00000.wav
        audio = _audio_for(root / "audio", f"{genre}.{num}")
        if audio is None:
            continue
        beats, downbeats = [], []
        for line in _read_text(ann).splitlines():
            parts = line.split()
            if not parts:
                continue
            beats.append(float(parts[0]))
            if len(parts) > 1 and parts[1] in ("1", "1.0"):
                downbeats.append(float(parts[0]))
        tempo = root / "tempo" / f"{ann.stem}.bpm"
        bpm = _float_or_none(_read_text(tempo)) if tempo.is_file() else None
        yield Reference("gtzan", f"{genre}.{num}", audio, bpm=bpm, beats=beats, downbeats=downbeats or None,
                        extra={"genre": genre})


def load_corpus(root: Path) -> Iterator[Reference]:
    from .annotate import read_annotation  # noqa: PLC0415 (annotate imports this module)

    for ann in sorted(root.rglob("*.json")):
        audio = _audio_for(ann.parent, ann.stem)
        if audio is not None:
            ref = read_annotation(ann, audio)
            ref.dataset, ref.track = "corpus", str(ann.relative_to(root).with_suffix(""))
            yield ref


DATASETS: dict[str, Dataset] = {d.name: d for d in (
    Dataset("giantsteps-tempo", "SEEDBOX2_GIANTSTEPS_TEMPO", "https://github.com/GiantSteps/giantsteps-tempo-dataset",
            load_giantsteps_tempo, "664 EDM Beatport previews, tempo (Knees et al. 2015; v2 by Schreiber & Müller 2018)"),
    Dataset("giantsteps-key", "SEEDBOX2_GIANTSTEPS_KEY", "https://github.com/GiantSteps/giantsteps-key-dataset",
            load_giantsteps_key, "604 EDM Beatport previews, key (Knees et al. 2015)"),
    Dataset("harmonix", "SEEDBOX2_HARMONIX", "https://github.com/urinieto/harmonixset",
            load_harmonix, "912 pop/EDM tracks, beats, downbeats and sections (Nieto et al. 2019); bring your own audio"),
    Dataset("gtzan", "SEEDBOX2_GTZAN", "https://github.com/TempoBeatDownbeat/gtzan_tempo_beat",
            load_gtzan, "1,000 30 s excerpts of 10 genres, beats and downbeats (Tzanetakis 2002; Marchand & Peeters "
            "2015); not in Beat This!'s training data"),
    Dataset("corpus", "SEEDBOX2_CORPUS", None, load_corpus, "our own tracks, annotated with `annotate`"),
)}


def data_root() -> Path:
    """Where `fetch` puts the public datasets: `$SEEDBOX2_EVAL_DATA`, else `eval-data` in Seedbox 2's cache folder."""
    if env := os.environ.get("SEEDBOX2_EVAL_DATA", "").strip():
        return Path(env).expanduser()
    from seedbox2.paths import data_dirs  # noqa: PLC0415

    cache = os.environ.get("SEEDBOX_CACHE_DIR", "").strip()
    return (Path(cache).expanduser() if cache else data_dirs()["SEEDBOX_CACHE_DIR"]) / "eval-data"


def folder_for(name: str, root: Path | None = None) -> Path | None:
    """A dataset's folder: its environment variable, else `<data root>/<name>` (not for the corpus)."""
    ds = DATASETS[name]
    if env := os.environ.get(ds.env, "").strip():
        return Path(env).expanduser()
    return None if ds.repo is None else (root or data_root()) / name


def load(name: str, folder: Path, limit: int | None = None) -> list[Reference]:
    refs = []
    for ref in DATASETS[name].load(folder):
        refs.append(ref)
        if limit is not None and len(refs) >= limit:
            break
    return refs


# ---------------------------------------------------------------------------------------------------------------
# keys

_PITCHES = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
# Camelot wheel: number -> pitch class of the minor (A) and major (B) key
_CAMELOT_MINOR = {1: 8, 2: 3, 3: 10, 4: 5, 5: 0, 6: 7, 7: 2, 8: 9, 9: 4, 10: 11, 11: 6, 12: 1}


def normalise_key(text: str | None) -> str | None:
    """"A minor", "Am", "F#m", "Ebmaj", "8A" or "a min" as mir_eval's "A minor"; None if it is not a key."""
    s = (text or "").strip().replace("♯", "#").replace("♭", "b")
    if not s:
        return None
    low = s.lower().replace(" ", "")
    if low[:-1].isdigit() and low[-1:] in ("a", "b") and 1 <= int(low[:-1]) <= 12:
        minor_pc = _CAMELOT_MINOR[int(low[:-1])]
        return f"{_NAMES[minor_pc]} minor" if low[-1] == "a" else f"{_NAMES[(minor_pc + 3) % 12]} major"
    if low[0] not in _PITCHES:
        return None
    pc, rest = _PITCHES[low[0]], low[1:]
    if rest[:1] in ("#", "b"):  # no mode name starts with b, so a b here is a flat
        pc, rest = pc + (1 if rest[0] == "#" else -1), rest[1:]
    mode = {"": "major", "maj": "major", "major": "major", "m": "minor", "min": "minor", "minor": "minor"}.get(rest)
    return None if mode is None else f"{_NAMES[pc % 12]} {mode}"


# ---------------------------------------------------------------------------------------------------------------
# fetch

# the GTZAN archive (genres/<genre>/<genre>.<n>.wav), pinned to a revision of its Hugging Face mirror
_GTZAN_ARCHIVE = ("https://huggingface.co/datasets/marsyas/gtzan/resolve/d2146561ecc7df707d9e6b8318885fe6a39668a2/"
                  "data/genres.tar.gz")
_GTZAN_SHA256 = "b28cc067ff6199bd826f5d1a6931458586d64acae7f44b2e882b7a97af057531"


def _fetch_gtzan(folder: Path, log: Callable[[str], None]) -> None:
    import tarfile

    audio = folder / "audio"
    if audio.is_dir() and len(list(audio.glob("*.wav"))) >= 1000:
        return
    archive = folder / "genres.tar.gz"
    if not archive.is_file():
        log(f"gtzan: downloading {_GTZAN_ARCHIVE} (1.2 GB)")
        part = archive.with_suffix(".part")
        with urllib.request.urlopen(_GTZAN_ARCHIVE, timeout=60) as r, part.open("wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        part.rename(archive)
    h = hashlib.sha256()
    with archive.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    if h.hexdigest() != _GTZAN_SHA256:
        archive.unlink()
        raise RuntimeError("gtzan: the archive's SHA-256 does not match; run fetch again")
    audio.mkdir(exist_ok=True)
    with tarfile.open(archive) as tar:
        for m in tar:
            name = Path(m.name).name
            if m.isfile() and name.endswith(".wav") and not name.startswith("._"):
                with tar.extractfile(m) as src, (audio / name).open("wb") as dst:  # type: ignore[union-attr]
                    shutil.copyfileobj(src, dst)
    archive.unlink()
    log(f"gtzan: {len(list(audio.glob('*.wav')))} excerpts")


_GIANTSTEPS_AUDIO = ("https://www.cp.jku.at/datasets/giantsteps/backup/", "https://geo-samples.beatport.com/lofi/")


def fetch(name: str, folder: Path, limit: int | None = None, log: Callable[[str], None] = print) -> None:
    """Clone a dataset's annotations into `folder` and, for GiantSteps, download (the first `limit`) previews."""
    ds = DATASETS[name]
    if ds.repo is None:
        raise ValueError(f"{name} is not a public dataset")
    if not (folder / ".git").is_dir():
        if shutil.which("git") is None:
            raise RuntimeError("git is needed to fetch the annotations")
        folder.parent.mkdir(parents=True, exist_ok=True)
        log(f"cloning {ds.repo} into {folder}")
        subprocess.run(["git", "clone", "-q", "--depth", "1", ds.repo, str(folder)], check=True)
    if name == "gtzan":
        _fetch_gtzan(folder, log)
        return
    if not name.startswith("giantsteps"):
        log(f"{name}: annotations only; put the audio in {folder / 'audio'} as <id>.<ext>")
        return
    md5s = sorted((folder / "md5").glob("*.md5"))[:limit]
    (folder / "audio").mkdir(exist_ok=True)
    for i, m in enumerate(md5s, 1):
        mp3 = folder / "audio" / (m.name[: -len(".md5")] + ".mp3")
        want = _read_text(m).split()[0]
        if mp3.is_file() and hashlib.md5(mp3.read_bytes()).hexdigest() == want:
            continue
        for base in _GIANTSTEPS_AUDIO:
            try:
                with urllib.request.urlopen(base + mp3.name, timeout=60) as r:
                    data = r.read()
            except OSError:
                continue
            if hashlib.md5(data).hexdigest() == want:
                mp3.write_bytes(data)
                break
        else:
            log(f"could not download {mp3.name}")
            continue
        if i % 25 == 0 or i == len(md5s):
            log(f"{name}: {i}/{len(md5s)} previews")
