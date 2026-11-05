"""The time stretch against Seedbox 1's (#52): CPU time per minute of audio, and renders for the blind A/B page.

    python -m seedbox2.eval stretch cpu [AUDIO]             CPU time per minute of audio, on a file or a synthetic track
    python -m seedbox2.eval stretch listen [--pairs 10]     renders into the #/listen folder, from the golden corpus

The renders use one plan and one mixer, Seedbox 1's, on both sides: only the stretching differs. Seedbox 2's mix engine
isn't there yet, and this way the page can only be comparing stretchers. They are the corpus's pairs with a tempo gap,
widest first, leaving out any whose plan doesn't stretch anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from seedbox2.mix import stretch as st

SR = 44100
OURS = "seedbox2-signalsmith"
THEIRS = "seedbox1-rubberband"
MAX_GAP_PCT = 8.0  # a wider gap is a half-time pair or an echo-out: Seedbox 1's plans don't blend across it


def _rubberband(x: np.ndarray, speed: np.ndarray) -> None:
    """What Seedbox 1's `_to_master` spends on a stretch: the music, then the pilot track."""
    import pedalboard

    from musicreco import mixrender

    pedalboard.time_stretch(x, SR, stretch_factor=speed, high_quality=True, transient_mode="crisp")
    mixrender._pilot_map(speed, SR, np.concatenate([[0.0], np.cumsum(1.0 / speed)])[:-1])


def _ours(x: np.ndarray, speed: np.ndarray, profile: st.Profile, semitones: int = 0) -> None:
    beats = np.arange(x.shape[1]) / SR * 2.0  # 120 BPM's beats
    pos = np.cumsum(speed)
    st.stretch(x, pos, beats, SR, semitones=semitones, profile=profile)


def cpu_table(x: np.ndarray, measure: Callable[[], float] = time.process_time) -> list[dict]:
    """CPU seconds per minute of stereo audio, for a constant 3% and a ramp from 0 to 3% over the audio."""
    n = x.shape[1]
    constant, ramp = np.full(n, 1.03), np.linspace(1.0, 1.03, n)
    runs = [("Seedbox 1: Rubber Band R3 + pilot track", "constant 3%", lambda: _rubberband(x, constant)),
            ("Seedbox 1: Rubber Band R3 + pilot track", "ramp 0-3%", lambda: _rubberband(x, ramp)),
            ("Signalsmith, full mix", "constant 3%", lambda: _ours(x, constant, st.FULL)),
            ("Signalsmith, full mix", "ramp 0-3%", lambda: _ours(x, ramp, st.FULL)),
            ("Signalsmith, full mix", "3% and +1 semitone", lambda: _ours(x, constant, st.FULL, 1)),
            ("Signalsmith, drums stem", "constant 3%", lambda: _ours(x, constant, st.DRUMS))]  # fmt: skip
    out = []
    for engine, case, run in runs:
        t0 = measure()
        run()
        spent = measure() - t0
        out.append({"engine": engine, "case": case, "cpu_s_per_minute": round(spent * 60 * SR / n, 2)})
    return out


@contextmanager
def signalsmith_in_seedbox1() -> Iterator[None]:
    """Seedbox 1's renderer, with its stretcher replaced. Decks at their own tempo and without key lock stay its own."""
    from musicreco import mixrender

    original = mixrender._to_master

    def to_master(src: np.ndarray, speed: np.ndarray, jq: np.ndarray, sr: int, key_lock: bool,
                  anchor: int = 0) -> tuple[np.ndarray, str]:  # fmt: skip
        if not key_lock or np.max(np.abs(speed - 1.0)) < 1e-9:
            return original(src, speed, jq, sr, key_lock, anchor)
        # Seams go on the master's beats, which only the caller has; this is test code for a renderer on its way out.
        beats = sys._getframe(1).f_locals["beats_q"]
        return st.stretch(src, jq, beats, sr), ""

    mixrender._to_master = to_master
    try:
        yield
    finally:
        mixrender._to_master = original


def stretched_pairs(corpus: object, found: dict[str, Path]) -> list:
    """The corpus's pairs with audio here and a gap to stretch across, widest first."""
    from .golden import tempo_gap_pct

    def gap(p: object) -> float:
        return tempo_gap_pct(corpus.tracks[p.a].bpm, corpus.tracks[p.b].bpm)

    usable = [p for p in corpus.pairs if p.a in found and p.b in found and 0.2 < gap(p) <= MAX_GAP_PCT]
    return sorted(usable, key=gap, reverse=True)


def cmd_listen(a: argparse.Namespace) -> int:
    from seedbox2.library import default_path

    from . import golden, listening, mixes, mixmetrics

    corpus = golden.load_corpus()
    found = golden.find_audio(corpus.tracks.values(), golden.audio_folders(a.audio), log=lambda s: print(s, file=sys.stderr))
    pairs = stretched_pairs(corpus, found)
    if not pairs:
        print("no pair with a tempo gap has its audio here: see `python -m seedbox2.eval golden check`", file=sys.stderr)
        return 1
    folder = a.folder or default_path().parent / listening.FOLDER
    rows: list[dict] = []
    for p in pairs:
        if len(rows) == a.pairs:
            break
        tracks = corpus.tracks[p.a], corpus.tracks[p.b]
        pair = mixes.Pair(p.id, found[p.a], found[p.b], tracks[0].key, tracks[1].key, p.technique, p.bars)
        row: dict = {"pair": p.id}
        renders = []
        mixes.analysis_of(pair)  # analysis is cached from here on, so the timings below are of the renders alone
        for version, stretcher in ((THEIRS, None), (OURS, signalsmith_in_seedbox1)):
            t0 = time.process_time()
            if stretcher:
                with stretcher():
                    r = mixes.render_seedbox1(pair)
            else:
                r = mixes.render_seedbox1(pair)
            cpu = time.process_time() - t0
            renders.append((version, r))
            m = mixmetrics.measure(r)
            row[version] = {"cpu_s": round(cpu, 1), "beat_median_ms": m["beat"]["median_ms"],
                            "stretch_pct": m["key"]["max_stretch_pct"]}  # fmt: skip
        if np.array_equal(renders[0][1].mix, renders[1][1].mix):  # its plan didn't stretch anything: nothing to compare
            print(f"{p.id}: not stretched, skipping", file=sys.stderr)
            continue
        for version, r in renders:
            style = str((r.plan or {}).get("technique") or "")
            listening.write_render(folder, p.id, version, style, r.mix, r.sr)
        print(json.dumps(row), flush=True)
        rows.append(row)
    if a.json:
        a.json.write_text(json.dumps(rows, indent=1))
    print(f"{len(rows)} pairs in {folder}: rate them on #/listen")
    return 0


def cmd_cpu(a: argparse.Namespace) -> int:
    from musicreco.mixrender import decode_stereo

    from .mixes import synth_track

    x = decode_stereo(a.audio, SR) if a.audio else synth_track(124.0, 40, "8A", 4, 4, [(8, 32)], SR)
    x = x[:, : a.seconds * SR]
    rows = cpu_table(x)
    width = max(len(r["engine"]) for r in rows)
    print(f"{x.shape[1] / SR:.0f} s of audio on one core")
    for r in rows:
        print(f"{r['engine']:{width}}  {r['case']:20} {r['cpu_s_per_minute']:6.2f} s per minute")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m seedbox2.eval stretch", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("cpu", help="CPU time per minute of audio")
    c.add_argument("audio", nargs="?", type=Path, help="default: a synthetic track")
    c.add_argument("--seconds", type=int, default=60)
    c.set_defaults(run=cmd_cpu)
    s = sub.add_parser("listen", help="A/B renders of the golden corpus for #/listen")
    s.add_argument("--pairs", type=int, default=10)
    s.add_argument("--audio", action="append", default=[], type=Path, help="a folder with the corpus's audio")
    s.add_argument("--folder", type=Path, help="default: listening/ next to the library")
    s.add_argument("--json", type=Path, help="write the CPU and metrics of each render here")
    s.set_defaults(run=cmd_listen)
    a = p.parse_args(argv)
    return a.run(a)
