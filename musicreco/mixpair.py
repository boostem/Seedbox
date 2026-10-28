"""Plan and render one A → B transition from two audio files, for listening to planner changes:

    python -m musicreco.mixpair A.mp3 B.mp3 [--technique "bass swap"] [--bars 16] -o out.wav

Analyses both files (structure.analyze, no cache), builds the plan with mixplan.generate, prints its summary, notes and
render stats, and writes the mix (.wav as is, .m4a encoded like the app). The Camelot keys come from the files' TKEY
tags unless given; without them the planner treats the key move as unknown.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from . import mixplan, mixrender, structure
from .models import Track


def _tag(path: Path, key: str) -> str:
    try:
        from mutagen import id3

        frame = id3.ID3(path).get(key)
    except Exception:  # noqa: BLE001 - no/odd tags: the caller falls back
        return ""
    return str(frame.text[0]).strip() if frame is not None and getattr(frame, "text", None) else ""


def _track(path: Path, an: dict, camelot: str | None) -> Track:
    return Track(
        id=path.stem, name=_tag(path, "TIT2") or path.stem, artists=[_tag(path, "TPE1")] if _tag(path, "TPE1") else [],
        album="", release_date="", duration_ms=int(an["duration_s"] * 1000), popularity=0, key="",
        camelot=camelot if camelot is not None else _tag(path, "TKEY"), bpm=an["bpm"], energy=0.0,
        danceability=0.0, cover_url=None,
    )  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m musicreco.mixpair", description=__doc__.split("\n\n")[0])
    p.add_argument("a", type=Path, help="outgoing track")
    p.add_argument("b", type=Path, help="incoming track")
    p.add_argument("-o", "--out", type=Path, required=True, help="output .wav or .m4a")
    p.add_argument("--technique", choices=mixplan.TECHNIQUES, help="force a technique (default: the planner's)")
    p.add_argument("--bars", type=int, choices=mixplan.BARS, help="force the blend / lead-in length")
    p.add_argument("--key-a", help="A's Camelot key (default: its TKEY tag)")
    p.add_argument("--key-b", help="B's Camelot key (default: its TKEY tag)")
    p.add_argument("--plan", type=Path, help="also write the plan JSON here")
    args = p.parse_args(argv)

    try:
        ans = []
        for path in (args.a, args.b):
            print(f"analysing {path.name} ...", file=sys.stderr)
            ans.append(structure.analyze(path, track_id=path.stem))
        a, b = _track(args.a, ans[0], args.key_a), _track(args.b, ans[1], args.key_b)
        plan = mixplan.generate(a, ans[0], b, ans[1], str(args.a.parent), technique=args.technique, bars=args.bars)
        print(f"A: {args.a.name} — {ans[0]['bpm']:g} BPM, {a.camelot or 'key ?'}, {ans[0]['bar_count']} bars")
        print(f"B: {args.b.name} — {ans[1]['bpm']:g} BPM, {b.camelot or 'key ?'}, {ans[1]['bar_count']} bars")
        print(mixplan.summary(plan, ans[0], ans[1]))
        for note in plan["notes"]:
            print(f"  · {note}")
        if args.plan:
            args.plan.write_text(json.dumps(plan, indent=1))
        print("rendering ...", file=sys.stderr)
        r = mixrender._render_audio(plan, args.a, args.b)
        if args.out.suffix.lower() == ".m4a":
            mixrender.encode_m4a(r["mix"], r["sr"], args.out)
        else:
            wavfile.write(args.out, r["sr"], np.ascontiguousarray(r["mix"].T))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"render: {r['duration_s']:.1f} s, {r['lufs']:.1f} LUFS, true peak {r['true_peak_db']:.1f} dBTP, "
          f"leveller {r['leveller_db']:.1f} dB, limiter {r['limiter_db']:.1f} dB -> {args.out}")
    for w in r["warnings"]:
        print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
