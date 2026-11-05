"""`python -m seedbox2.eval`: fetch the datasets, annotate our corpus, score the analysers.

    python -m seedbox2.eval fetch giantsteps-tempo giantsteps-key [--limit 50]
    python -m seedbox2.eval analysis [--dataset NAME[=FOLDER]]... [--analyser NAME]... [--limit N]
                                     [--baseline FILE --max-drop 2] [--out FILE] [--jobs N]
    python -m seedbox2.eval annotate AUDIO... [--analyser seedbox1] [--rekordbox FILE.xml] [--force]
    python -m seedbox2.eval import-rekordbox FILE.xml
    python -m seedbox2.eval mixes [PAIRS.toml] [--json FILE]   (objective mix metrics, #54)
    python -m seedbox2.eval listen [PAIRS.toml] [--techniques A,B]   (renders for the #/listen page, #86)
    python -m seedbox2.eval stems --musdb DIR --model FILE...  (HTDemucs through ONNX against torch, #44)
    python -m seedbox2.eval golden [--help]                    (the golden corpus and render regression, #87)
    python -m seedbox2.eval energy [--help]                    (calibrate the track energy against hand ratings, #48)
    python -m seedbox2.eval stretch [--help]                   (time stretch: CPU per minute, A/B renders, #52)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import annotate as ann_mod
from . import datasets, run
from .analysers import ANALYSERS


def _dataset_args(specs: list[str] | None, root: Path | None) -> list[tuple[str, Path]]:
    """NAME or NAME=FOLDER; by default every dataset whose folder exists."""
    out = []
    names = specs or list(datasets.DATASETS)
    for spec in names:
        name, _, folder = spec.partition("=")
        if name not in datasets.DATASETS:
            raise SystemExit(f"unknown dataset {name!r}; known: {', '.join(datasets.DATASETS)}")
        path = Path(folder).expanduser() if folder else datasets.folder_for(name, root)
        if path is None or not path.is_dir():
            if specs:
                hint = f"set ${datasets.DATASETS[name].env}" if datasets.DATASETS[name].repo is None else \
                    f"run `python -m seedbox2.eval fetch {name}`"
                raise SystemExit(f"{name}: no folder at {path} ({hint})")
            continue
        out.append((name, path))
    return out


def cmd_fetch(a: argparse.Namespace) -> int:
    root = Path(a.data).expanduser() if a.data else None
    for name in a.names or ["giantsteps-tempo", "giantsteps-key", "harmonix"]:
        folder = datasets.folder_for(name, root)
        if folder is None:
            raise SystemExit(f"{name} is not a public dataset")
        datasets.fetch(name, folder, a.limit)
    return 0


def cmd_analysis(a: argparse.Namespace) -> int:
    root = Path(a.data).expanduser() if a.data else None
    chosen = _dataset_args(a.dataset, root)
    if not chosen:
        print("no datasets found: run `python -m seedbox2.eval fetch` or set $SEEDBOX2_CORPUS", file=sys.stderr)
        return 2
    analysers = a.analyser or ["seedbox1"]
    for name in analysers:
        if name not in ANALYSERS:
            raise SystemExit(f"unknown analyser {name!r}; known: {', '.join(ANALYSERS)}")
    refs = {name: datasets.load(name, folder, a.limit) for name, folder in chosen}
    for name, rs in refs.items():
        if not rs:
            print(f"{name}: no annotated tracks with audio in {dict(chosen)[name]}", file=sys.stderr)
    refs = {k: v for k, v in refs.items() if v}
    rows = run.evaluate(refs, analysers, a.jobs, log=lambda m: print(m, file=sys.stderr))
    record = run.run_record(rows, refs, analysers)
    print(run.table(record["summary"]), end="")
    for r in rows:
        if "error" in r:
            print(f"error: {r['dataset']}/{r['track']} ({r['analyser']}): {r['error']}", file=sys.stderr)
    out = run.save(record, Path(a.out) if a.out else None)
    print(f"saved {out}")
    if a.write_baseline:
        Path(a.write_baseline).write_text(json.dumps(run.strip_tracks(record), indent=1) + "\n", encoding="utf-8")
        print(f"wrote baseline {a.write_baseline}")
    if a.baseline:
        base = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
        bad = run.regressions(record["summary"], base, a.max_drop)
        if bad:
            print(f"regressions against {a.baseline} (more than {a.max_drop:g} points):", file=sys.stderr)
            for line in bad:
                print(f"  {line}", file=sys.stderr)
            return 1
        print(f"no regression > {a.max_drop:g} points against {a.baseline}")
    return 0


def cmd_annotate(a: argparse.Namespace) -> int:
    fn = ANALYSERS[a.analyser]
    items = []
    for audio in map(Path, a.audio):
        target = audio.with_suffix(".json")
        if target.exists() and not a.force:
            print(f"{target} exists (it may hold corrections); --force overwrites it", file=sys.stderr)
            ann = json.loads(target.read_text(encoding="utf-8"))
        else:
            ann = ann_mod.to_annotation(fn(audio))
            ann_mod.write_annotation(target, ann)
            print(f"wrote {target}")
        items.append((audio, ann))
    if a.rekordbox:
        Path(a.rekordbox).write_text(ann_mod.to_rekordbox(items), encoding="utf-8")
        print(f"wrote {a.rekordbox}: import it into Rekordbox, fix the grid and the memory cues, export the "
              f"collection and run `python -m seedbox2.eval import-rekordbox <exported.xml>`")
    return 0


def cmd_import_rekordbox(a: argparse.Namespace) -> int:
    corrections = ann_mod.from_rekordbox(Path(a.xml).read_text(encoding="utf-8"))
    done = 0
    for audio, fix in corrections.items():
        target = audio.with_suffix(".json")
        if not target.is_file():
            continue
        ann = ann_mod.merge(json.loads(target.read_text(encoding="utf-8")), fix)
        if a.verified:
            ann["verified"] = True
        ann_mod.write_annotation(target, ann)
        done += 1
        print(f"updated {target}")
    print(f"{done} annotation(s) updated, {len(corrections) - done} track(s) without one skipped")
    return 0


def cmd_stems(a: argparse.Namespace) -> int:
    from . import stems

    return stems.main(a)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m seedbox2.eval", description=__doc__.splitlines()[0])
    p.add_argument("--data", help="folder of the public datasets (default $SEEDBOX2_EVAL_DATA or the cache folder)")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="clone the annotations, download the GiantSteps previews, GTZAN, Jamendo and MUSDB18")
    f.add_argument("names", nargs="*", choices=[n for n in datasets.DATASETS if n != "corpus"], metavar="NAME")
    f.add_argument("--limit", type=int, help="only the first N previews (sorted by id), or N of MUSDB18's tracks")
    f.set_defaults(fn=cmd_fetch)

    an = sub.add_parser("analysis", help="score analysers against the datasets and print the table")
    an.add_argument("--dataset", action="append", metavar="NAME[=FOLDER]",
                    help=f"repeatable; default: all found ({', '.join(datasets.DATASETS)})")
    an.add_argument("--analyser", action="append", metavar="NAME", help=f"repeatable; known: {', '.join(ANALYSERS)}")
    an.add_argument("--limit", type=int, help="first N tracks of each dataset")
    an.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    an.add_argument("--out", help="results file (default $SEEDBOX2_EVAL_RESULTS/analysis-<time>.json)")
    an.add_argument("--baseline", help="a previous run (or baseline) to compare against")
    an.add_argument("--max-drop", type=float, default=2.0, help="points a metric may drop (default 2)")
    an.add_argument("--write-baseline", metavar="FILE", help="also write this run's summary as a baseline")
    an.set_defaults(fn=cmd_analysis)

    at = sub.add_parser("annotate", help="write <audio>.json from an analysis, to correct by hand")
    at.add_argument("audio", nargs="+")
    at.add_argument("--analyser", default="seedbox1", choices=list(ANALYSERS))
    at.add_argument("--rekordbox", metavar="FILE.xml", help="also write a Rekordbox collection XML")
    at.add_argument("--force", action="store_true", help="overwrite existing annotations")
    at.set_defaults(fn=cmd_annotate)

    rb = sub.add_parser("import-rekordbox", help="read grid, key and cue corrections back from a Rekordbox XML")
    rb.add_argument("xml")
    rb.add_argument("--verified", action="store_true", help="mark the updated annotations as checked")
    rb.set_defaults(fn=cmd_import_rekordbox)

    sub.add_parser("listen", help="renders for the #/listen blind test (#86); --help after it", add_help=False)
    st = sub.add_parser("stems", help="HTDemucs through ONNX against torch on MUSDB18 clips (#44; needs torch, demucs)")
    st.add_argument("--musdb", required=True, help="a folder of *.stem.mp4")
    st.add_argument("--model", action="append", required=True, help="an ONNX file; repeat for several")
    st.add_argument("--clips", type=int, default=30)
    st.add_argument("--threads", type=int, default=3)
    st.add_argument("--max-loss", type=float, default=0.1, help="dB of mean SI-SDR below torch's that still passes")
    st.set_defaults(fn=cmd_stems)
    sub.add_parser("mixes", help="objective mix metrics of rendered transitions (#54); --help after it", add_help=False)
    sub.add_parser("energy", help="calibrate the track energy against hand ratings (#48); --help after it",
                   add_help=False)  # fmt: skip
    sub.add_parser("stretch", help="time stretch: CPU per minute and A/B renders (#52); --help after it", add_help=False)
    sub.add_parser("golden", help="render the golden corpus and catch regressions (#87); --help after it",
                   add_help=False)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "listen":  # has its own parser
        from . import listening

        return listening.main(argv[1:])
    if argv and argv[0] == "mixes":  # has its own parser
        from . import mixes

        return mixes.main(argv[1:])
    if argv and argv[0] == "energy":  # has its own parser
        from . import energy

        return energy.main(argv[1:])
    if argv and argv[0] == "stretch":  # has its own parser
        from . import stretchtest

        return stretchtest.main(argv[1:])
    if argv and argv[0] == "golden":  # has its own parser
        from . import golden

        return golden.main(argv[1:])
    a = parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
