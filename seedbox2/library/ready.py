"""Only ready tracks go into sets and mixes (#99).

A track that isn't analysed yet would be ordered without its key, tempo or energy, and mixed on guesses (a plan grid for
beats, 8-bar phrases for sections, EQ for stems), and the user would get a bad mix with no reason. So set building and
ordering, mix planning, the now-playing set and set export take usable tracks only (`seedbox2.library.view`: ready, or
failed and "use anyway"), and refuse the rest with what each of them is missing (`NotReady`, a 409 in the API).
Playing one track on its own is always allowed.

"Use anyway" is kept with the track (`Library.set_use_anyway`, `PUT /library/tracks/{hash}/use-anyway`), and a request
may send it too (`use_anyway`: hashes), for this request only. Either way it counts only for a failed track: one whose
analysis is still queued or running waits.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any


class NotReady(Exception):
    """Some of the tracks aren't ready. `tracks`: `{hash, title, artist, state, missing}` for each, in their order."""

    def __init__(self, tracks: list[dict]) -> None:
        self.tracks = tracks
        n = len(tracks)
        names = ", ".join(_name(t) for t in tracks[:3]) + (f" and {n - 3} more" if n > 3 else "")
        super().__init__(f"{n} track{'s' * (n != 1)} {'is' if n == 1 else 'are'}n't ready to mix: {names}. "
                         "Wait for the analysis to finish, retry it if it failed, or use a failed track anyway.")  # fmt: skip

    def detail(self) -> dict:
        """The 409's body: `{message, tracks}`."""
        return {"message": str(self), "tracks": self.tracks}


def _name(t: dict) -> str:
    return " - ".join(x for x in (t.get("artist"), t.get("title")) if x) or t["hash"][:8]


def stems_required(lib: Any) -> bool:
    """Whether a ready track needs its stems: not when stems are off in the settings."""
    from seedbox2.analysis import stems
    from seedbox2.library.service import SETTINGS_FILE, Settings

    path = getattr(getattr(lib, "db", None), "path", None)
    if path is None:
        return True
    return stems.read_settings(Settings(Path(path).parent / SETTINGS_FILE))["enabled"]


def rows(lib: Any, hashes: Iterable[str] | None = None) -> list[dict]:
    """The library-table rows (`Library.view_rows`) of these tracks (None: all), judged as the library view judges them."""
    from seedbox2.analysis import waveform

    return lib.view_rows(waveform_version=waveform.MODEL_VERSION, hashes=hashes, stems_required=stems_required(lib))


def _usable(row: dict, use_anyway: set[str]) -> bool:
    status = row["status"]
    return bool(status.get("usable")) or (row["hash"] in use_anyway and status["state"] == "failed")


def unready(lib: Any, hashes: Iterable[str], use_anyway: Iterable[str] = ()) -> list[dict]:
    """Those of these tracks that can't go into a set or mix, once each, in their order: `{hash, title, artist, state,
    missing}`. Tracks that aren't in the library are left to the caller."""
    order = list(dict.fromkeys(hashes))
    anyway = set(use_anyway)
    found = {r["hash"]: r for r in rows(lib, order)} if order else {}
    return [{"hash": h, "title": r.get("title"), "artist": r.get("artist"), "state": r["status"]["state"],
             "missing": r["status"]["missing"]}
            for h in order if (r := found.get(h)) is not None and not _usable(r, anyway)]  # fmt: skip


def require(lib: Any, hashes: Iterable[str], use_anyway: Iterable[str] = ()) -> None:
    """NotReady if any of these tracks isn't usable."""
    if bad := unready(lib, hashes, use_anyway):
        raise NotReady(bad)


def usable_hashes(lib: Any, use_anyway: Iterable[str] = ()) -> set[str]:
    """Every track in the library that may go into a set or mix."""
    anyway = set(use_anyway)
    return {r["hash"] for r in rows(lib) if _usable(r, anyway)}
