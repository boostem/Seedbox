"""Plan v3 through the offline renderer: Seedbox 1's mixrender, which takes its plan format, plus the stem lanes, with
Seedbox 2's track gain and master chain (#58) in place of its overlap trim and leveller.

Seedbox 2's own stretching lands with #52; until then a v3 plan is rendered by turning it back into what mixrender
reads. `render_adapted` is the whole path from a generated plan to audio: make it stem-aware (`stemplan`), fit the
automation to the tracks, render.
`export` is that audio in a file, with a tracklist holding what the render measures.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np

from musicreco import mixrender

from ..analysis import loudness, waveform
from ..analysis import stems as stem_cache
from . import automation, convert, stemplan, styles
from . import master as master_chain
from .plan import STEM_LANES, Deck, MixPlan, deck_grid, fold_stem_lanes

Stems = dict[str, Path]
MEASURED_PASSES = 3


def cached_stems(hash_: str) -> Stems:
    """The track's stem files in the cache, by name, if it has all four."""
    if not hash_:
        return {}
    return waveform.stem_files(stem_cache.folder_for(hash_)) or {}


def _deck(deck: Deck) -> dict:
    return {
        "track_id": deck.track_id, "native_bpm": deck.grid[0].bpm, "grid": [s.model_dump() for s in deck.grid],
        "play_from_beat": deck.play_from_beat, "play_to_beat": deck.play_to_beat,
        "echo_beats": deck.sends.echo_beats, "echo_tap": deck.sends.echo_tap, "reverb_size": deck.sends.reverb_size,
        "reverb_tap": deck.sends.reverb_tap, "lanes": deck.lanes.model_dump(),
        "loops": [lp.model_dump() for lp in deck.loops], "fx": [e.model_dump() for e in deck.fx],
    }  # fmt: skip


def to_seedbox1(plan: MixPlan) -> dict:
    """What mixrender reads. Stem lanes aren't in it: pass them to `render_audio` as `deck_stems`."""
    return {
        "version": 2, "folder": "", "from_id": plan.a.track_id, "to_id": plan.b.track_id,
        "technique": styles.render_technique(plan.technique),
        "bars": plan.bars, "key_lock": plan.key_lock, "window": plan.window.model_dump(),
        "tempo": [k.model_dump() for k in plan.tempo], "a": _deck(plan.a), "b": _deck(plan.b),
        "markers": [m.model_dump() for m in plan.markers], "notes": plan.notes, "edited": plan.edited,
    }  # fmt: skip


def _decode(files: Stems, sr: int) -> dict[str, np.ndarray]:
    return {name: mixrender.decode_stereo(path, sr) for name, path in files.items()}


def track_gain_db(audio: np.ndarray, sr: int, spans: list[dict] | None = None) -> float:
    """`loudness.track_gain_db` of decoded audio, for a track with no stored analysis to read it from. 0 if silent."""
    blocks = mixrender._block_power(audio, sr, loudness.BLOCK_S, loudness.HOP_S)
    data = {"lufs": np.asarray(loudness.energy_to_lufs(blocks)).tolist()}
    return loudness.track_gain_db(data, spans, master_chain.DECK_LUFS) or 0.0


def deck_audio(plan: MixPlan, path_a: Path, path_b: Path, stems_a: Stems | None = None, stems_b: Stems | None = None,
               sr: int = mixrender.SR) -> tuple[automation.DeckAudio, automation.DeckAudio]:  # fmt: skip
    """Both decks' audio as `automation.adapt` wants it, with the track gains the renderer will apply."""
    out = []
    for path, files in ((path_a, stems_a), (path_b, stems_b)):
        audio = mixrender.decode_stereo(Path(path), sr)
        out.append(automation.DeckAudio(audio, sr, track_gain_db(audio, sr), _decode(files, sr) if files else None))
    return out[0], out[1]


def _stem_tracks(deck: Deck, audio: automation.DeckAudio) -> list[tuple[np.ndarray, list[dict]]] | None:
    if deck.stem_lanes is None or not audio.stems:
        return None
    return [(audio.stems[lane.removeprefix("gain_")], [k.model_dump() for k in getattr(deck.stem_lanes, lane)])
            for lane in STEM_LANES if lane.removeprefix("gain_") in audio.stems]  # fmt: skip


def render(plan: MixPlan, path_a: Path, path_b: Path, audio_a: automation.DeckAudio, audio_b: automation.DeckAudio,
           sr: int = mixrender.SR, master: Callable[[np.ndarray, int], tuple[np.ndarray, dict]] | None = None) -> dict:  # fmt: skip
    """A plan as it plays on these tracks: `mixrender.render_audio`'s result, through `master` (default: for streaming)."""
    playing = fold_stem_lanes(plan, lambda track: (audio_a if track == plan.a.track_id else audio_b).stems is not None)
    deck_stems = {"A": _stem_tracks(playing.a, audio_a), "B": _stem_tracks(playing.b, audio_b)}
    return mixrender.render_audio(to_seedbox1(playing), Path(path_a), Path(path_b), sr,
                                  deck_stems={k: v for k, v in deck_stems.items() if v},
                                  trims_db={"A": audio_a.trim_db, "B": audio_b.trim_db},
                                  master=master or master_chain.Master(sr=sr))  # fmt: skip


def _measured(r: dict, plan: dict) -> tuple[np.ndarray, np.ndarray, float] | None:
    """(master beats, deviation in LU, the worst of it) of a render's loudness against the line between the tracks."""
    from ..eval import mixmetrics  # noqa: PLC0415

    render = mixmetrics.Render.from_mixrender(r, plan)
    found = mixmetrics.loudness_deviation(render, mixmetrics.overlap(render))
    if found is None:
        return None
    t, dev, _, _ = found
    return np.asarray(r["clock"].beat(t)), dev, float(np.abs(dev).max())


def tracks_for(plan: MixPlan, audio_a: automation.DeckAudio, audio_b: automation.DeckAudio,
               keys: tuple[str | None, str | None] = (None, None)) -> tuple[stemplan.Track, stemplan.Track]:  # fmt: skip
    """What the stem planner knows of both tracks from their decoded stems (vocal phrases, levels) and Camelot keys."""
    return (stemplan.track_from_audio(audio_a.stems, audio_a.sr, deck_grid(plan.a), camelot=keys[0]),
            stemplan.track_from_audio(audio_b.stems, audio_b.sr, deck_grid(plan.b), camelot=keys[1]))  # fmt: skip


def render_adapted(raw: dict | MixPlan, path_a: Path, path_b: Path, stems_a: Stems | None = None, stems_b: Stems | None = None,
                   sr: int = mixrender.SR, master: master_chain.Master | None = None, *,
                   keys: tuple[str | None, str | None] = (None, None),
                   tracks: tuple[stemplan.Track, stemplan.Track] | None = None) -> tuple[dict, dict]:  # fmt: skip
    """A generated Seedbox 1 plan (or one converted already) made stem-aware (`stemplan`, #53), its automation fitted to the tracks, rendered and
    corrected from what the render measures, up to MEASURED_PASSES times; a pass that makes it worse is dropped. The
    stem rules are put back after every change. (the render, the plan in Seedbox 1's format, with `adaptive` holding
    the report, `stems` the stem planner's and `plan_v3` the plan as rendered, stem lanes and all)."""
    plan = raw if isinstance(raw, MixPlan) else convert.from_seedbox1(raw)
    audio_a, audio_b = deck_audio(plan, path_a, path_b, stems_a, stems_b, sr)
    track_a, track_b = tracks or tracks_for(plan, audio_a, audio_b, keys)
    plan, stem_report = stemplan.plan(plan, track_a, track_b)
    adapted = automation.adapt(plan, audio_a, audio_b)
    best_plan, report = stemplan.enforce(adapted.plan, stem_report), adapted.report
    best = render(best_plan, path_a, path_b, audio_a, audio_b, sr, master)
    if "cuts_db" in report:  # there is a blend to ride
        worst = None
        for _ in range(MEASURED_PASSES):
            found = _measured(best, to_seedbox1(best_plan))
            if found is None:
                break
            beats, dev, worst = found
            next_plan = automation.ride_measured(best_plan, beats, dev)
            if next_plan is None:
                break
            next_plan = stemplan.enforce(next_plan, stem_report)
            r = render(next_plan, path_a, path_b, audio_a, audio_b, sr, master)
            again = _measured(r, to_seedbox1(next_plan))
            if again is None or again[2] >= worst:
                break
            best, best_plan, worst = r, next_plan, again[2]
        report = {**report, "measured_lu": None if worst is None else round(worst, 2)}
    stem_report = {**stem_report, "checks": stemplan.check(best_plan, track_a, track_b)}
    return best, {**to_seedbox1(best_plan), "adaptive": report, "stems": stem_report, "plan_v3": best_plan.model_dump()}


def export(plan: MixPlan, path_a: Path, path_b: Path, out_path: Path, *, names: tuple[str, str] | None = None,
           stems_a: Stems | None = None, stems_b: Stems | None = None, target_lufs: float = master_chain.TARGETS[master_chain.DEFAULT_TARGET],
           sr: int = mixrender.SR) -> dict:  # fmt: skip
    """A plan to an audio file and a tracklist next to it (`<out>.txt`). The render is measured and the make-up corrected
    until it meets the target; the result has `measured`, what the tracklist says, and the `makeup_db` that
    makes the live engine play it at this level. A whole set is `setexport`'s (#70)."""
    audio_a, audio_b = deck_audio(plan, path_a, path_b, stems_a, stems_b, sr)
    found: list[float] = []

    def closed_loop(mix: np.ndarray, rate: int) -> tuple[np.ndarray, dict]:
        found.append(master_chain.find_makeup(mix, target_lufs, rate))
        return master_chain.Master(target_lufs, found[-1], rate)(mix, rate)

    r = render(plan, path_a, path_b, audio_a, audio_b, sr, closed_loop)
    measured = master_chain.measure(r["mix"], sr)
    mixrender.encode_m4a(r["mix"], sr, Path(out_path))
    a, b = names or (plan.a.track_id, plan.b.track_id)
    w0 = plan.window.start_beat
    entries = [(float(r["clock"].time(max(deck.play_from_beat, w0))), name) for deck, name in ((plan.a, a), (plan.b, b))]
    text = master_chain.tracklist(sorted(entries), measured, target_lufs)
    tracklist = Path(out_path).with_suffix(".txt")
    tracklist.write_text(text, "utf-8")
    return {"measured": measured, "makeup_db": found[0], "tracklist": str(tracklist), "master": r["master"]}
