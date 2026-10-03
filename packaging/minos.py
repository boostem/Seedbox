"""Newest macOS any Mach-O file in a bundle was built for (LC_BUILD_VERSION / LC_VERSION_MIN_MACOSX minos).

    python packaging/minos.py Seedbox.app [--top N]     → prints the max minos (e.g. "12.3") on the last line
Used by build.sh to set LSMinimumSystemVersion to the true requirement of everything bundled.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
_RE = re.compile(r"cmd LC_BUILD_VERSION.*?minos ([0-9.]+)|cmd LC_VERSION_MIN_MACOSX.*?version ([0-9.]+)", re.S)


def macho_files(root: Path) -> list[Path]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            with open(p, "rb") as f:
                if f.read(4) in MAGICS:
                    out.append(p)
    return out


def minos(path: Path) -> tuple[int, ...] | None:
    text = subprocess.run(["otool", "-arch", "arm64", "-l", str(path)], capture_output=True, text=True).stdout
    found = [m.group(1) or m.group(2) for m in _RE.finditer(text)]
    return max((tuple(int(x) for x in v.split(".")) for v in found), default=None)


def main(argv: list[str]) -> int:
    root = Path(argv[1])
    top = int(argv[argv.index("--top") + 1]) if "--top" in argv else 5
    results = [(v, p) for p in macho_files(root) if (v := minos(p)) is not None]
    if not results:
        print("11.0")
        return 0
    counts = Counter(".".join(map(str, v)) for v, _ in results)
    print(f"{len(results)} Mach-O files; minos counts: {dict(sorted(counts.items()))}", file=sys.stderr)
    for v, p in sorted(results, reverse=True)[:top]:
        print(f"  {'.'.join(map(str, v)):>6}  {p.relative_to(root)}", file=sys.stderr)
    best = max(v for v, _ in results)
    print(".".join(map(str, (best + (0,))[:2])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
