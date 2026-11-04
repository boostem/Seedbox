"""`python -m seedbox2.bench`: see seedbox2/bench/__init__.py and README.md."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from seedbox2.bench import corpus, results, stages


def _write(data: dict, out: Path | None) -> None:
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _frontend_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--fe-seconds", type=float, default=5.0, help="seconds per UI scenario (default 5)")
    p.add_argument("--cpu-throttle", type=float, default=1.0, help="slow Chrome's CPU down N times (default 1)")
    p.add_argument("--software-gl", action="store_true",
                   help="WebGL on the CPU (SwiftShader): a floor below any integrated GPU, for a machine with a fast one")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m seedbox2.bench", description="Seedbox 2's performance benchmarks")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="analysis, stems, plan and render on the corpus (the default)")
    r.add_argument("-o", "--out", type=Path, help="write the results JSON here (default: stdout)")
    r.add_argument("--stages", default=",".join(stages.ORDER), help=f"comma-separated subset of {','.join(stages.ORDER)}")
    r.add_argument("--tracks", type=int, default=10, help="use the first N tracks (default all 10)")
    r.add_argument("--corpus-dir", type=Path, help="where the corpus is kept (default: Seedbox 2's cache folder)")
    r.add_argument("--synthetic", action="store_true", help="synthetic tracks instead of the downloaded corpus")
    r.add_argument("--seconds", type=float, default=120.0, help="length of each synthetic track (default 120)")
    r.add_argument("--no-tracemalloc", action="store_true", help="skip the second, traced run of each stage (half the time, no Python heap peak)")
    r.add_argument("--frontend", action="store_true", help="also run the frontend benchmark into the same file")
    r.add_argument("--label", help="a name for this machine in the results (default: the host name)")
    _frontend_args(r)

    f = sub.add_parser("frontend", help="fps and long tasks of frontend2 in headless Chrome")
    f.add_argument("-o", "--out", type=Path, help="write the results JSON here (default: stdout)")
    _frontend_args(f)

    c = sub.add_parser("compare", help="regressions between two results files, as markdown (exit 1 if any)")
    c.add_argument("old", type=Path)
    c.add_argument("new", type=Path)
    c.add_argument("--threshold", type=float, default=results.THRESHOLD, help="relative change (default 0.10)")

    rep = sub.add_parser("report", help="a results file as markdown")
    rep.add_argument("file", type=Path)

    args = p.parse_args(argv if argv is not None else sys.argv[1:] or ["run"])

    if args.cmd == "compare":
        text, bad = results.compare_markdown(_load(args.old), _load(args.new), args.threshold)
        sys.stdout.write(text)
        return 1 if bad else 0
    if args.cmd == "report":
        sys.stdout.write(results.report_markdown(_load(args.file)))
        return 0

    from seedbox2.bench import frontend

    if args.cmd == "frontend":
        fe = frontend.run(seconds=args.fe_seconds, cpu_throttle=args.cpu_throttle, software_gl=args.software_gl, log=_log)
        from seedbox2.bench.run import check_budgets, machine

        data = {"schema": 1, "machine": machine(), "frontend": fe}
        data["budgets"] = check_budgets(data)
        _write(data, args.out)
        return 0 if "skipped" not in fe else 2

    from seedbox2.app import apply_data_dirs
    from seedbox2.bench.run import check_budgets, run

    apply_data_dirs()  # musicreco reads the cache folders from the environment
    only = tuple(s.strip() for s in args.stages.split(",") if s.strip())
    if unknown := set(only) - set(stages.ORDER):
        p.error(f"unknown stage(s): {', '.join(sorted(unknown))}")
    base = args.corpus_dir or corpus.default_dir()
    if args.synthetic:
        tracks, name = corpus.synthetic(base / "synthetic", args.tracks, args.seconds), f"synthetic-{args.tracks}x{args.seconds:g}s"
    else:
        tracks, name = corpus.fetch(base, corpus.TRACKS[: args.tracks], log=_log), corpus.CORPUS
        if args.tracks < len(corpus.TRACKS):
            name += f"-first{args.tracks}"
    data = run(tracks, only, trace=not args.no_tracemalloc, log=_log, label=args.label, corpus_name=name)
    if args.frontend:
        data["frontend"] = frontend.run(seconds=args.fe_seconds, cpu_throttle=args.cpu_throttle,
                                        software_gl=args.software_gl, log=_log)
        data["budgets"] = check_budgets(data)
    _write(data, args.out)
    _log(results.report_markdown(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
