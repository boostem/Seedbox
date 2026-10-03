"""tunebat -> yt-dlp DJ crate builder (personal archival / DJ practice).

Search tunebat, pick the exact version of a track (the seed), tick which of tunebat's recommendations
to grab (ranked by how well they mix, with 20 s audition clips), then download them from YouTube as
tagged MP3s into <crates folder>/<Artists - Title of seed>/, numbered in a suggested set order, alongside
tracks.csv, skipped.txt and .m3u8/.xml playlists. The crates folder, parallel downloads and tolerances come
from Seedbox's settings (default ~/Music/MusicReco; MUSICRECO_OUTPUT_ROOT overrides the folder).

Run:  .venv/bin/python tunebat_dl.py
"""

from __future__ import annotations

import logging
import math
import threading
from collections import Counter
from pathlib import Path
from typing import Any

import questionary
from prompt_toolkit.filters import IsDone
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent, merge_key_bindings
from prompt_toolkit.layout import ConditionalContainer, FormattedTextControl, HSplit, Layout, Window
from questionary import Choice
from questionary.prompts.common import InquirerControl
from rich import box
from rich.cells import cell_len, set_cell_size
from rich.console import Console, Group
from rich.live import Live
from rich.logging import RichHandler
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from musicreco import audition, library, mixing, pipeline
from musicreco.models import DownloadResult, Score, SearchPage, Track
from musicreco.settings import Settings, load_settings
from musicreco.tunebat import TunebatClient, TunebatError

PAGE_SIZE = 50
NEXT, PREV, NEW_SEARCH = "→ Next page", "← Previous page", "↺ New search"

STATUS_STYLE = {
    "downloaded": "green",
    "copied": "cyan",
    "exists": "blue",
    "skipped": "yellow",
    "failed": "red",
    "cancelled": "red",
}
STATUS_ORDER = ["downloaded", "copied", "exists", "skipped", "failed"]
STATUS_W = 22  # fits the longest step text, "trying alternative 2/3"

# Camelot move (mixing.score) -> rich colour; prompt_toolkit gets the same colour via pt_style().
MOVE_COLOUR = {
    "same": "green", "+1": "cyan", "-1": "cyan", "relative": "yellow",
    "+2 boost": "magenta", "+7 boost": "magenta", "diagonal": "magenta", "clash": "red",
}  # fmt: skip
SPARKS = "▁▂▃▄▅▆▇█"

console = Console()
settings = Settings()  # defaults until main() loads the user's settings


# --- small helpers -------------------------------------------------------------------------------------


def ask(question: questionary.Question) -> Any:
    """Run a prompt; Ctrl-C / Ctrl-D come back as None instead of raising."""
    try:
        return question.unsafe_ask()
    except (KeyboardInterrupt, EOFError):
        return None


def fit(text: str, width: int) -> str:
    """Pad or cut `text` to exactly `width` terminal cells, marking cuts with an ellipsis."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return set_cell_size(text, width)
    return set_cell_size(text, width - 1) + "…"


def shrink_to(natural: list[int], room: int, minimum: int = 6) -> list[int]:
    """Column widths: natural sizes, trimming the widest column first until they fit in `room` cells."""
    widths = list(natural)
    while sum(widths) > room and max(widths) > minimum:
        widths[widths.index(max(widths))] -= 1
    return widths


def bpm_str(track: Track) -> str:
    return f"{track.bpm:.0f}" if track.bpm else "?"


def move_colour(move: str) -> str:
    """Rich colour for a Camelot move; an unknown key ("?") is dim."""
    return MOVE_COLOUR.get(move, "dim")


def pt_style(colour: str) -> str:
    """The prompt_toolkit spelling of a rich colour name."""
    return colour if colour == "dim" else f"fg:ansi{colour}"


def move_legend() -> Text:
    return Text.assemble(("same", "green"), " · ", ("±1", "cyan"), " · ", ("relative", "yellow"), " · ",
                         ("boost/diagonal", "magenta"), " · ", ("clash", "red"))  # fmt: skip


def chips_str(score: Score) -> str:
    return " · ".join(score.chips)


def pretty_path(path: Path) -> str:
    """Path relative to the working directory, else to ~, when possible (shorter on screen)."""
    for base, prefix in ((Path.cwd(), ""), (Path.home(), "~/")):
        try:
            return prefix + str(path.resolve().relative_to(base.resolve()))
        except ValueError:
            continue
    return str(path)


def bpm_tol_str() -> str:
    return f"±{settings.bpm_tolerance * 100:g}%"


# --- step 2: pick the seed from search results ---------------------------------------------------------

_SEARCH_LABELS = ("Title", "Artists", "Album")
_SEARCH_FIXED = 10 + 5 + 3 + 3 + 2 * 6  # released, length, key, bpm + 2-space gaps between the 7 columns


def search_widths(tracks: list[Track], width: int) -> list[int]:
    """Widths of the title / artists / album columns for a terminal `width` cells wide."""
    natural = [
        max([cell_len(label)] + [cell_len(text) for text in texts])
        for label, texts in zip(
            _SEARCH_LABELS,
            ([t.name for t in tracks], [t.artist_str for t in tracks], [t.album for t in tracks]),
        )
    ]
    room = width - 4 - _SEARCH_FIXED  # 3 cells for questionary's pointer, 1 spare so lines never wrap
    return shrink_to(natural, room)


def _search_line(cols: tuple[str, str, str, str, str, str, str], widths: list[int]) -> str:
    title, artists, album, released, length, key, bpm = cols
    return "  ".join(
        [fit(title, widths[0]), fit(artists, widths[1]), fit(album, widths[2]),
         f"{released:<10}", f"{length:>5}", f"{key:>3}", f"{bpm:>3}"]
    )  # fmt: skip


def search_row(track: Track, widths: list[int]) -> str:
    """One aligned picker row: title, artists, album, release date, length, Camelot, BPM."""
    cols = (track.name, track.artist_str, track.album, track.release_date, track.duration_str,
            track.camelot, bpm_str(track))  # fmt: skip
    return _search_line(cols, widths)


def search_header(widths: list[int]) -> str:
    return _search_line((*_SEARCH_LABELS, "Released", "Len", "Key", "BPM"), widths).rstrip()


class SearchableRow(str):
    """A picker row that type-to-filter matches on the full title/artists/album, even where cut off on screen.

    questionary filters with `typed.lower() in title.lower()`, so lower() returns the complete text.
    """

    def __new__(cls, shown: str, full_text: str) -> SearchableRow:
        row = super().__new__(cls, shown)
        row.full_text = f"{shown} {full_text}".lower()
        return row

    def lower(self) -> str:
        return self.full_text


def search_choices(page: SearchPage, widths: list[int]) -> list[Choice]:
    """Picker entries: one per track (value = index into page.items), then the paging entries."""
    choices = [
        Choice(
            SearchableRow(search_row(t, widths), f"{t.name} {t.artist_str} {t.album}"),
            value=i,
            description=f"{t.key or '?'} · popularity {t.popularity} · {t.spotify_url}",
        )
        for i, t in enumerate(page.items)
    ]
    if page.has_next:
        choices.append(Choice(NEXT, value=NEXT))
    if page.page > 1:
        choices.append(Choice(PREV, value=PREV))
    choices.append(Choice(NEW_SEARCH, value=NEW_SEARCH))
    return choices


def seed_prompt(page: SearchPage, width: int) -> questionary.Question:
    """Arrow-key picker for one page of results, with a column header and type-to-filter."""
    pages = max(1, math.ceil(page.total_count / PAGE_SIZE))
    widths = search_widths(page.items, width)
    return questionary.select(
        f"Pick the seed — page {page.page} of {pages} ({page.total_count} results)",
        choices=search_choices(page, widths),
        use_search_filter=True,
        use_jk_keys=False,  # j/k must reach the type-to-filter search
        instruction="(type to filter, ↑ for paging)\n   " + search_header(widths),
    )


def pick_seed(client: TunebatClient, term: str) -> Track | None:
    """Page through tunebat results until a track is picked; None means start a new search."""
    page_no = 1
    while True:
        with console.status(f"Searching tunebat for “{term}” (page {page_no})…"):
            page = client.search(term, page_no)
        if not page.items:
            console.print(f"[yellow]No results for “{term}”.[/yellow]")
            return None
        answer = ask(seed_prompt(page, console.width))
        if answer is None or answer == NEW_SEARCH:
            return None
        if answer == NEXT:
            page_no = page.page + 1
        elif answer == PREV:
            page_no = page.page - 1
        else:
            return page.items[answer]


# --- steps 3-4: show the recommendations and choose what to download -----------------------------------


def unique_recs(seed: Track, recs: list[Track]) -> list[Track]:
    """Recommendations without the seed itself or repeats, in tunebat's order."""
    seen = {seed.id}
    out = []
    for t in recs:
        if t.id not in seen:
            seen.add(t.id)
            out.append(t)
    return out


def rank(seed: Track, recs: list[Track]) -> tuple[list[Track], dict[str, Score]]:
    """Recommendations best-first by mixing.score (ties keep tunebat's order), plus each one's Score."""
    scores = {t.id: mixing.score(seed, t, settings.bpm_tolerance) for t in recs}
    return sorted(recs, key=lambda t: -scores[t.id].value), scores


def recommendations_table(seed: Track, recs: list[Track], scores: dict[str, Score]) -> Table:
    """The seed plus every "r" track, best first: score and key in the Camelot move's colour, rule failures dim."""
    passing = sum(s.passed for s in scores.values())
    caption = Text(f"{passing}/{len(recs)} pass the rule (Camelot adjacent + BPM {bpm_tol_str()}, half/double-time "
                   "ok); dim rows fail it. Key moves: ") + move_legend()  # fmt: skip
    table = Table(title=f"Seed: {seed.display_name}", caption=caption, header_style="bold", box=box.SIMPLE_HEAD)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Score", justify="right", no_wrap=True)
    table.add_column("Key", justify="right", no_wrap=True)
    table.add_column("BPM", justify="right", no_wrap=True)
    table.add_column("Track")  # wrappable columns are the ones rich shrinks to fit the terminal...
    table.add_column("Len", justify="right", no_wrap=True)
    table.add_column("Why")
    table.add_row("seed", "", seed.camelot, bpm_str(seed), _cut(seed.display_name), seed.duration_str, "seed",
                  style="bold")  # fmt: skip
    for n, t in enumerate(recs, 1):
        s = scores[t.id]
        colour = move_colour(s.move)
        table.add_row(str(n), Text(str(s.value), style=colour), Text(t.camelot or "?", style=colour), bpm_str(t),
                      _cut(t.display_name), t.duration_str, _cut(chips_str(s)),
                      style="" if s.passed else "dim")  # fmt: skip
    return table


def _cut(text: str) -> Text:
    """...and these cells then truncate with an ellipsis instead of wrapping onto a second line."""
    return Text(text, no_wrap=True, overflow="ellipsis")


CHECK_KEYS = "(space toggle, a all, i invert, p play, s stop, ↵ go)"
AUDITION_HINT = "p plays a 20 s clip of the highlighted track, s stops it"
_CHECK_FIXED = 5 + 3 + 3 + 5 + 2 * 5  # score, key, BPM, length + 2-space gaps between the 6 columns
_SCORE_KEY = 5 + 2 + 3  # leading cells of a row ("Score  Key") drawn in the move colour


def checklist_widths(seed: Track, recs: list[Track], scores: dict[str, Score], width: int) -> list[int]:
    """Widths of the track and why columns for a terminal `width` cells wide."""
    natural = [max(cell_len(t.display_name) for t in [seed, *recs]),
               max([4] + [cell_len(chips_str(s)) for s in scores.values()])]  # fmt: skip
    return shrink_to(natural, width - 6 - _CHECK_FIXED)  # 5 cells for pointer + tick box, 1 spare


def _check_line(score: str, key: str, bpm: str, name: str, length: str, why: str, widths: list[int]) -> str:
    return f"{score:>5}  {key:>3}  {bpm:>3}  {fit(name, widths[0])}  {length:>5}  {fit(why, widths[1])}".rstrip()


def checklist_title(track: Track, score: Score | None, widths: list[int]) -> list[tuple[str, str]]:
    """A checklist row as styled text; `score` None marks the seed."""
    if score is None:
        line = _check_line("", track.camelot, bpm_str(track), track.display_name, track.duration_str, "seed", widths)
        return [("bold", line)]
    line = _check_line(str(score.value), track.camelot or "?", bpm_str(track), track.display_name,
                       track.duration_str, chips_str(score), widths)  # fmt: skip
    rest = "" if score.passed else "dim"
    return [(f"{rest} {pt_style(move_colour(score.move))}".strip(), line[:_SCORE_KEY]), (rest, line[_SCORE_KEY:])]


def checklist_choices(seed: Track, recs: list[Track], scores: dict[str, Score], widths: list[int]) -> list[Choice]:
    """Seed first and ticked, then the recommendations ticked iff they pass the rule. Values are Spotify IDs."""
    choices = [Choice(checklist_title(seed, None, widths), value=seed.id, checked=True,
                      description=f"{seed.key or '?'} · the seed")]  # fmt: skip
    for t in recs:
        s = scores[t.id]
        why = f"{t.key or '?'} · rule {mixing.passes_rule(seed, t, settings.bpm_tolerance)[1]} · {chips_str(s)}"
        choices.append(Choice(checklist_title(t, s, widths), value=t.id, checked=settings.pretick_all or s.passed, description=why))
    return choices


def checklist_prompt(seed: Track, recs: list[Track], scores: dict[str, Score], width: int) -> questionary.Question:
    widths = checklist_widths(seed, recs, scores, width)
    question = questionary.checkbox(
        "Download which tracks?",
        choices=checklist_choices(seed, recs, scores, widths),
        instruction=f"{CHECK_KEYS}\n     " + _check_line("Score", "Key", "BPM", "Track", "Len", "Why", widths),
    )
    add_audition(question, {t.id: t for t in [seed, *recs]})
    return question


def add_audition(question: questionary.Question, tracks: dict[str, Track]) -> None:
    """Checklist extras: `p` auditions the highlighted row, `s` stops, and a status line sits under the list."""
    app = question.application
    ic = next((w.content for w in app.layout.find_all_windows() if isinstance(w.content, InquirerControl)), None)
    if ic is None:  # questionary's internals changed: keep the plain checklist rather than crash
        return
    keys = KeyBindings()

    @keys.add("p", eager=True)
    def _play(_event: KeyPressEvent) -> None:
        audition.play(tracks[ic.get_pointed_at().value])

    @keys.add("s", eager=True)
    def _stop(_event: KeyPressEvent) -> None:
        audition.stop()

    app.key_bindings = merge_key_bindings([app.key_bindings, keys])
    status = Window(FormattedTextControl(audition_status), height=1)
    app.layout = Layout(HSplit([app.layout.container, ConditionalContainer(status, filter=~IsDone())]))


def audition_status() -> list[tuple[str, str]]:
    """The checklist's status line: what's loading/playing, why a clip failed, or how to play one."""
    text = audition.status()
    if not text:
        return [("dim", f"  ♪ {AUDITION_HINT}")]
    return [("fg:ansired" if text.startswith("✗") else "fg:ansicyan", f"  ♪ {text}")]


def choose_tracks(seed: Track, recs: list[Track], scores: dict[str, Score]) -> list[Track]:
    """Checklist prompt; returns the ticked tracks (seed first) or [] for none / Ctrl-C."""
    console.print("[dim]Best matches first. " + ("Everything is pre-ticked; untick what you don't want." if settings.pretick_all
                  else "Seed and rule matches are pre-ticked; tick others to opt in.") + "[/dim]")
    question = checklist_prompt(seed, recs, scores, console.width)
    audition.set_on_change(question.application.invalidate)  # redraw the status line from audition's threads
    try:
        picked = ask(question)
    finally:
        audition.set_on_change(None)
        audition.stop()
    ids = set(picked or [])
    return [t for t in [seed, *recs] if t.id in ids]


# --- step 5: download with live progress, then report --------------------------------------------------


class BatchView:
    """Live per-track status display; update() is called from the download worker threads."""

    def __init__(self, tracks: list[Track]):
        self.tracks = tracks
        self.status = {t.id: "queued" for t in tracks}
        self.spinners = {t.id: Spinner("dots", style="cyan") for t in tracks}
        self._lock = threading.Lock()

    def update(self, track: Track, status: str) -> None:
        with self._lock:
            self.status[track.id] = status

    def __rich__(self) -> Group:
        with self._lock:
            status = dict(self.status)
        done = sum(s in STATUS_STYLE for s in status.values())
        if any(s in ("cancelling", "cancelled") for s in status.values()):
            note = Text("Cancelling… (Ctrl-C again to stop waiting)", style="yellow")
        else:
            note = Text(f"Downloading, {settings.workers} at a time…", style="bold")
        head = Table.grid(padding=(0, 1))
        bar = ProgressBar(total=len(self.tracks), completed=done, width=30)
        head.add_row(bar, f"{done}/{len(self.tracks)}", note)

        rows = Table.grid(padding=(0, 1))
        for t in self.tracks:
            s = status[t.id]
            icon: Any = self.spinners[t.id] if s not in STATUS_STYLE and s != "queued" else Text("•")
            rows.add_row(icon, Text(f"{s:<{STATUS_W}}", style=STATUS_STYLE.get(s, "dim")),
                         Text(t.display_name, no_wrap=True, overflow="ellipsis"))  # fmt: skip
        return Group(head, rows)


def summary_table(results: list[DownloadResult]) -> Table:
    """One row per processed track: outcome, genre and the most useful detail."""
    table = Table(header_style="bold", show_lines=False)
    table.add_column("Status", no_wrap=True)
    table.add_column("Track", overflow="fold", ratio=3)
    table.add_column("Genre", overflow="fold", ratio=1)
    table.add_column("Details", overflow="fold", ratio=3)
    for r in results:
        genre = Text(r.genre or "—", style="" if r.genre else "dim")
        if r.genre_source:
            genre.append(f" ({r.genre_source})", style="dim")
        table.add_row(Text(r.status, style=STATUS_STYLE.get(r.status, "")), r.track.display_name,
                      genre, result_detail(r))  # fmt: skip
    return table


def result_detail(r: DownloadResult) -> str:
    if r.status == "downloaded" and r.youtube:
        return f"{r.youtube.title} [{r.youtube.source}]"
    if r.status == "skipped" and r.closest:
        length = f"{r.closest.duration_s:.0f}s" if r.closest.duration_s is not None else "?"
        return f"{r.reason or 'no match'} — closest: {r.closest.title} ({length})"
    if r.status == "exists" and r.path:
        return "already in this folder"
    return r.reason or ""


def status_counts(results: list[DownloadResult]) -> Text:
    counts = Counter(r.status for r in results)
    parts = [(f"{counts[s]} {s}", STATUS_STYLE[s]) for s in STATUS_ORDER if counts[s]]
    return Text("  ·  ").join(Text(text, style=style) for text, style in parts)


def stars(rating: int | None) -> Text:
    """Energy 1-10 as 1-5 stars, rounding half up (the same mapping as the POPM rating tag)."""
    if rating is None:
        return Text("—", style="dim")
    n = min(5, max(1, (rating + 1) // 2))
    return Text.assemble(("★" * n, "yellow"), ("☆" * (5 - n), "dim"))


def sparkline(values: list[float | None], lo: float, hi: float) -> str:
    """One block character per value, scaled from lo to hi (None -> blank)."""
    top = len(SPARKS) - 1

    def block(v: float | None) -> str:
        if v is None:
            return " "
        if hi <= lo:
            return SPARKS[top // 2]
        return SPARKS[round(min(1.0, max(0.0, (v - lo) / (hi - lo))) * top)]

    return "".join(block(v) for v in values)


def played_bpm(bpm: float, ref: float) -> float:
    """`bpm`, halved or doubled when that's nearer `ref` (how it would actually be mixed)."""
    if bpm <= 0 or ref <= 0:
        return bpm
    return min((bpm, bpm * 2, bpm / 2), key=lambda b: abs(b - ref))


def set_order_view(results: list[DownloadResult]) -> Group | None:
    """The crate in its suggested running order, with energy stars and energy/BPM curves; None if unordered."""
    ordered = sorted((r for r in results if r.order is not None), key=lambda r: r.order or 0)
    if not ordered:
        return None
    caption = Text("Key colour = move from the previous track: ") + move_legend()
    table = Table(title="Set order (track numbers)", caption=caption, header_style="bold", box=box.SIMPLE_HEAD)
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Key", justify="right", no_wrap=True)
    table.add_column("BPM", justify="right", no_wrap=True)
    table.add_column("Energy", no_wrap=True)
    table.add_column("Track")
    prev: Track | None = None
    for r in ordered:
        t = r.track
        colour = "bold" if prev is None else move_colour(mixing.score(prev, t).move)
        table.add_row(str(r.order), Text(t.camelot or "?", style=colour), bpm_str(t), stars(r.energy_rating),
                      _cut(t.display_name))  # fmt: skip
        prev = t

    ref = ordered[0].track.bpm
    bpms = [played_bpm(r.track.bpm, ref) or None for r in ordered]
    known = [b for b in bpms if b] or [0.0]
    curve = Text.assemble(
        ("  Energy ", "dim"), (sparkline([r.energy_rating for r in ordered], 1, 10), "yellow"),
        ("   BPM ", "dim"), (sparkline(bpms, min(known), max(known)), "cyan"),
        (f" {min(known):.0f}–{max(known):.0f}", "dim"),
    )  # fmt: skip
    return Group(table, curve)


def build_crate(client: TunebatClient, picked: Track) -> None:
    """Steps 3-5 for one seed: recommendations -> checklist -> download -> set order -> tracks.csv / skipped.txt."""
    with console.status(f"Fetching tunebat recommendations for {picked.display_name}…"):
        detail = client.get_track(picked.id)
    seed = detail.seed
    recs, scores = rank(seed, unique_recs(seed, detail.recommended))

    console.print()
    console.print(recommendations_table(seed, recs, scores))
    if not recs:
        console.print("[yellow]No tunebat recommendations for this track; only the seed is listed.[/yellow]")

    selected = choose_tracks(seed, recs, scores)
    if not selected:
        console.print("[dim]Nothing ticked — back to search.[/dim]")
        return

    dest = library.seed_dir(seed)
    n = len(selected)
    question = f"Download {n} track{'s' * (n != 1)} into “{pretty_path(dest)}/”?"
    if not ask(questionary.confirm(question, default=True)):
        _remove_if_empty(dest)
        console.print("[dim]Cancelled — back to search.[/dim]")
        return

    view = BatchView(selected)
    with Live(view, console=console, refresh_per_second=10, transient=True):
        results = pipeline.run_batch(selected, seed, dest, workers=settings.workers, on_update=view.update,
                                     tolerance_s=settings.tolerance_s)  # fmt: skip
    with console.status("Rating energy, ordering the set and writing the crate files…"):
        # Earlier runs' files are rated + numbered too, so track numbers never collide on a re-run.
        done = pipeline.complete_batch(seed, recs, scores, {t.id for t in selected}, dest, results,
                                       bpm_tolerance=settings.bpm_tolerance)  # fmt: skip

    console.print(summary_table(results))
    console.print(status_counts(results))
    if any(r.reason == pipeline.CANCELLED for r in results):
        console.print("[yellow]Interrupted: cancelled tracks show as failed; rerun to get them.[/yellow]")
    if order := set_order_view(done.crate):
        console.print(order)

    # .m3u8/.xml carry the set order for DJ apps that ignore track numbers (.xml: AutoPilot / rekordbox import).
    for written in (done.csv_path, done.playlist_path, done.xml_path, done.skipped_path):
        if written:
            console.print(f"Wrote [bold]{pretty_path(written)}[/bold]")


def _remove_if_empty(folder: Path) -> None:
    """Undo library.seed_dir() creating a folder for a crate the user then declined."""
    try:
        folder.rmdir()
    except OSError:
        pass  # not empty (an earlier crate) or already gone


# --- main loop -----------------------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
    )
    global settings
    settings = load_settings()
    console.rule("[bold]tunebat → yt-dlp crate builder")
    console.print(f"[dim]Crates go to {pretty_path(library.downloads_root())}/. "
                  "Empty search or Ctrl-C quits. Ctrl-C inside a step goes back to search.[/dim]")  # fmt: skip
    legacy_notice()
    console.print()
    try:
        search_loop(TunebatClient())
    finally:
        audition.cleanup()  # stop any clip still playing and delete the session's clips


def legacy_notice() -> None:
    """Point out crates still sitting in the old project downloads/ folder (Seedbox moves them on first launch)."""
    old = library.LEGACY_DOWNLOADS_ROOT
    try:
        if old.is_symlink() or not old.is_dir() or old.resolve() == library.downloads_root().resolve():
            return
        crates = [d for d in old.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except OSError:
        return
    if crates:
        n = len(crates)
        console.print(f"[yellow]{n} older crate{'s' * (n != 1)} still in {pretty_path(old)}/ — opening Seedbox once "
                      "moves them to the crates folder.[/yellow]")  # fmt: skip


def search_loop(client: TunebatClient) -> None:
    """Search -> seed -> crate, until an empty search or Ctrl-C at the search prompt."""
    while True:
        term = ask(questionary.text("Search tunebat:"))
        if not term or not term.strip():
            console.print("Bye — happy mixing!")
            return
        try:
            seed = pick_seed(client, term.strip())
            if seed is not None:
                build_crate(client, seed)
        except TunebatError as e:
            console.print(f"[red]tunebat error: {e}[/red]")
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted — back to search.[/yellow]")
        except Exception:  # an unexpected bug shouldn't lose the session; show it and carry on
            console.print_exception(max_frames=8)
        console.print()


if __name__ == "__main__":
    main()
