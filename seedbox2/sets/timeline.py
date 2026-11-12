"""The whole-set timeline (#75): what `#/sets` draws for a running order, and the plans its transitions play.

`timeline()` answers with each track's sections and energy curve and where each transition sits in the two files, read
from the pair's saved plan. A pair without a plan comes back `pending` and goes to the `set_plan` job, so dragging a track
never waits for planning; the `job.done` event says when to ask again.

`set_plan` plans one pair on its own, in the transition style its genres, tempo, energy and vocals call for (#57,
`seedbox2.mix.styles`) over Seedbox 1's generator on our analysis, made stem-aware by
`seedbox2.mix.stemplan` (#53: bass and vocal hand-offs on the stem lanes, no doubled kicks, mashup moments), and saves it.
A track whose stems aren't cached yet plays those lanes on its EQ. The set's plan (#61, `setplan`) picks each pair's style,
length and cue points with the rest of the set in view; the job gets that pick and plans the pair on it, or as before when
the style doesn't fit the pair's window after all. #56's `adapt` isn't run here. A track's vocals are the stem's when it has them
(`vocals.plan_input_for`). A saved plan is left alone: a person's edit, an import or an
earlier job's result always wins.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import numpy as np

from seedbox2.analysis import beats, energy, loudness, sections, vibe, vocals
from seedbox2.analysis import stems as stem_cache
from seedbox2.analysis.grid import Grid
from seedbox2.jobs import register
from seedbox2.mix import convert
from seedbox2.mix import genre, master, stemplan, styles
from seedbox2.mix.plan import MixPlan, deck_time
from seedbox2.sets.features import load_features

log = logging.getLogger(__name__)
JOB = "set_plan"
PLAN_SOURCE = "set"
_MAX_CURVE_POINTS = 64  # a whole set is drawn from these: a point per bar is more than a track's width shows
_BEATS_PER_BAR = 4


def _curve(data: dict | None) -> list[list[float]]:
    """A track's energy as `[file time, level]`, thinned to what the timeline can show."""
    if not data:
        return []
    bars, levels = data.get("bar_s") or [], data.get("curve") or []
    n = min(len(bars) - 1, len(levels))
    if n <= 0:
        return []
    step = max(1, int(np.ceil(n / _MAX_CURVE_POINTS)))
    return [[round(float(bars[min(i + step // 2, n - 1)]), 2), round(float(np.mean(levels[i:i + step])), 3)]
            for i in range(0, n, step)]  # fmt: skip


def _analysis(lib: Any, hash_: str, analyser: str, version: str) -> dict | None:
    row = lib.analysis(hash_, analyser, version)
    return row["data"] if row else None


def track_view(lib: Any, hash_: str) -> dict:
    track = lib.track(hash_) or {}
    data = _analysis(lib, hash_, energy.ANALYSER, energy.VERSION)
    grid = _analysis(lib, hash_, beats.ANALYSER, beats.VERSION)
    level = _analysis(lib, hash_, energy.LOUDNESS_ANALYSER, energy.VERSION) or {}
    return {
        "hash": hash_, "duration_s": float(track.get("duration") or 0.0), "sections": vibe.spans_of(lib, hash_, data),
        "curve": _curve(data), "bar_s": round(240.0 / float(grid["bpm"]), 4) if grid and grid.get("bpm") else None,
        "integrated_lufs": level.get("integrated_lufs"),
        "track_gain_db": loudness.track_gain_db(level, vibe.spans_of(lib, hash_, data), master.DECK_LUFS) if level else None,
    }  # fmt: skip


def transition_range(plan: MixPlan) -> tuple[float, float]:
    """Where both decks sound: `transitionRange` in `features/mix/lanes.ts`. The whole window when they never do."""
    start = max(plan.a.play_from_beat, plan.b.play_from_beat, plan.window.start_beat)
    end = min(plan.a.play_to_beat, plan.b.play_to_beat, plan.window.end_beat)
    return (start, end) if end > start else (plan.window.start_beat, plan.window.end_beat)


def placement(plan: MixPlan) -> dict:
    """Where the transition sits in each file, and how long it lasts on the master clock."""
    from musicreco.mixplan import master_seconds

    start, end = transition_range(plan)
    tempo = [k.model_dump() for k in plan.tempo]
    seconds = float(master_seconds(tempo, end) - master_seconds(tempo, start))
    return {
        "technique": plan.technique, "bars": plan.bars, "edited": plan.edited, "seconds": round(seconds, 3),
        "a_in_s": round(float(deck_time(plan.a, start)), 3), "a_out_s": round(float(deck_time(plan.a, end)), 3),
        "b_in_s": round(float(deck_time(plan.b, start)), 3), "b_out_s": round(float(deck_time(plan.b, end)), 3),
    }  # fmt: skip


def _failed_pairs(lib: Any) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for job in lib.jobs("failed"):
        if job.get("kind") == JOB and isinstance(job.get("payload"), dict):
            out[(job["payload"].get("from"), job["payload"].get("to"))] = job.get("error") or "planning failed"
    return out


def timeline(lib: Any, hashes: list[str], jobs: Any = None, *, retry: bool = False,
             picks: Any = None, use_anyway: Sequence[str] = ()) -> dict:  # fmt: skip
    """The running order `hashes` as the timeline draws it. With `jobs`, what's missing is queued: a pair's plan once both
    tracks have beats (the first thing a track gets), else those beats. `retry` tries a failed pair again. `picks()`
    gives the set plan's pick per pair (`setplan.picks`), asked only when a pair is queued. The caller has seen to it
    that the tracks are ready (#99, `seedbox2.library.ready`); `use_anyway` goes to the jobs, which check again."""
    tracks = {h: track_view(lib, h) for h in set(hashes)}
    failed = _failed_pairs(lib)
    out = []
    chosen: dict | None = None
    for a, b in zip(hashes, hashes[1:]):
        row = lib.plan(a, b)
        item: dict[str, Any] = {"from": a, "to": b, "state": "pending"}
        if row is not None:
            try:
                item |= {"state": "ready", "user_edited": bool(row["user_edited"]),
                         **placement(convert.load(row["plan"], from_track=a, to_track=b))}  # fmt: skip
            except ValueError as e:
                item |= {"state": "failed", "error": f"this plan can't be read: {e}"}
        elif (a, b) in failed and not retry:
            item |= {"state": "failed", "error": failed[(a, b)]}
        else:
            missing = [h for h in (a, b) if _analysis(lib, h, beats.ANALYSER, beats.VERSION) is None]
            if missing:
                item["state"] = "waiting"
            if jobs is not None:
                for h in missing:
                    beats.submit(jobs, h, front=True)
                if not missing:
                    if chosen is None:
                        chosen = _picks(picks)
                    pick = chosen.get((a, b))
                    anyway = [h for h in (a, b) if h in use_anyway]
                    jobs.submit(JOB, {"from": a, "to": b, **({"pick": pick} if pick else {}),
                                      **({"use_anyway": anyway} if anyway else {})}, front=True)  # fmt: skip
        out.append(item)
    return {"tracks": [tracks[h] for h in hashes], "transitions": out}


def _picks(picks: Any) -> dict:
    if picks is None:
        return {}
    try:
        return picks()
    except Exception:  # noqa: BLE001 - the set's plan is a hint: a pair plans on its own without it
        log.warning("the set plan failed; pairs plan on their own", exc_info=True)
        return {}


# --- planning a pair -------------------------------------------------------------------------------------------------


def _per_bar(starts: np.ndarray, bar_s: float, data: dict | None, values: list | None) -> list[float]:
    """Values per bar of the energy analysis, resampled onto the bars of the plan's grid."""
    if not data or not values or len(data.get("bar_s") or []) < 2:
        return [0.0] * starts.size
    edges = np.asarray(data["bar_s"], np.float64)
    at = np.searchsorted(edges, starts + bar_s / 2, side="right") - 1
    v = np.asarray(values, np.float64)
    return np.round(v[np.clip(at, 0, v.size - 1)], 3).tolist()


def seedbox1_analysis(lib: Any, hash_: str, duration: float) -> dict | None:
    """Our analyses in the shape `musicreco.mixplan.generate` reads. None without beats: its plan would be on no grid."""
    data = _analysis(lib, hash_, beats.ANALYSER, beats.VERSION)
    if not data or not data.get("segments") or not data.get("bpm"):
        return None
    grid = Grid.from_json(data["segments"])
    bpm = float(data["bpm"])
    bar_s = 240.0 / bpm
    first = int(np.ceil(float(grid.beat_at(0.0)) / _BEATS_PER_BAR - 1e-9))
    first_downbeat = float(grid.time_at(first * _BEATS_PER_BAR))
    count = max(1, int((duration - first_downbeat) / bar_s))
    starts = first_downbeat + np.arange(count) * bar_s
    level = _analysis(lib, hash_, energy.ANALYSER, energy.VERSION)
    spans = vibe.spans_of(lib, hash_, level)
    edge = {s["kind"]: s for s in spans if s.get("kind") in ("intro", "outro")}
    return {
        "track_id": hash_, "duration_s": duration, "bpm": bpm, "bpm_source": "detected", "first_downbeat_s": first_downbeat,
        "bar_s": bar_s, "bar_count": count, "energy_per_bar": _per_bar(starts, bar_s, level, (level or {}).get("curve")),
        "low_per_bar": _per_bar(starts, bar_s, level, ((level or {}).get("features") or {}).get("bass")),
        "vocal_per_bar": [0.0] * count, "sections": [s for s in spans if s.get("kind") != "main"],
        "intro": edge.get("intro") or {"start_s": 0.0, "end_s": first_downbeat},
        "outro": edge.get("outro") or {"start_s": duration, "end_s": duration},
        "cues": [], "grid": data["segments"],
    }  # fmt: skip


def plan_pair(lib: Any, from_hash: str, to_hash: str, pick: dict | None = None) -> MixPlan:
    """A new plan for the pair, not saved. With the set plan's `pick` (`setplan.picks`: style, bars, cue points) the
    pair is planned in that style on those cues, unless the style doesn't fit the generator's window: then as without."""
    from musicreco import mixplan, transitions
    from musicreco.models import Track

    features = load_features(lib, [from_hash, to_hash])
    analyses, decks, known = [], [], []
    for h in (from_hash, to_hash):
        row = lib.track(h)
        if row is None:
            raise ValueError(f"no track {h}")
        an = seedbox1_analysis(lib, h, float(row["duration"]))
        if an is None:
            raise ValueError(f"track {h} has no beat analysis yet")
        an = vocals.plan_input_for(lib, h, an)
        f = features[h]
        analyses.append(an)
        section_row = lib.analysis(h, sections.ANALYSER, sections.VERSION)
        known.append(stemplan.track_from_analysis(an, has_stems=stem_cache.current(lib, h) is not None,
                                                  camelot=f.camelot or None,
                                                  sections=section_row["data"] if section_row else None))  # fmt: skip
        decks.append(Track(id=h, name=row.get("title") or h, artists=[row["artist"]] if row.get("artist") else [], album="",
                           release_date="", duration_ms=int(float(row["duration"]) * 1000), popularity=0, key="",
                           camelot=f.camelot or "", bpm=an["bpm"], energy=f.energy or 0.0, danceability=0.0,
                           cover_url=None))  # fmt: skip
    ctx = styles.context(analyses[0], analyses[1], genre_a=genre.effective(lib, from_hash)["genre"],
                         genre_b=genre.effective(lib, to_hash)["genre"], energy_a=features[from_hash].energy,
                         energy_b=features[to_hash].energy, a_stems=known[0].has_stems, b_stems=known[1].has_stems,
                         b_acapella=bool(known[1].acapella))  # fmt: skip

    def load(raw: dict) -> MixPlan:
        return convert.load(raw, from_track=from_hash, to_track=to_hash)

    def generate(technique: str | None, bars: int | None) -> dict:
        return mixplan.generate(decks[0], analyses[0], decks[1], analyses[1], "", technique=technique, bars=bars)

    styled = None
    style = styles.by_id(str(pick.get("style"))) if pick else None
    if pick and style is not None:
        want = int(pick.get("bars") or 0)
        bars_ok = want if want in mixplan.BARS else None
        engine = style.base if style.base in transitions.TECHNIQUES else "long blend" if want >= 16 else "phrase blend"
        cue = {"ready": True, "from_id": from_hash, "to_id": to_hash, "technique": engine,
               "out_start_s": float(pick["a_out_s"]), "in_start_s": float(pick["b_in_s"]), "overlap_bars": want}  # fmt: skip

        def on_cues(technique: str | None, bars: int | None) -> dict:
            if technique == style.base and bars is None:
                bars = bars_ok if style.kind != "cut" else None
            return mixplan.generate(decks[0], analyses[0], decks[1], analyses[1], "", technique=technique, bars=bars,
                                    transition=cue)  # fmt: skip

        try:
            got, report = styles.plan(ctx, on_cues, load, (style,))
            styled = got if report["style"] == style.id else None
        except ValueError:
            styled = None
    if styled is None:
        styled, _ = styles.plan(ctx, generate, load)
    plan, _ = stemplan.plan(styled, known[0], known[1])
    return plan


@register(JOB, pool="light")
def set_plan_job(payload: dict, ctx: Any) -> dict:
    """Payload `{"from": hash, "to": hash, "pick"?: the set plan's pick, "use_anyway"?: hashes}`: plans the pair and
    saves it, unless it has a plan by now. Fails if a track isn't ready (#99): a plan on guesses would be a bad mix."""
    from seedbox2.library import ready

    a, b = payload["from"], payload["to"]
    lib = ctx.library
    if lib.plan(a, b) is not None:
        return {"from": a, "to": b, "planned": False}
    ready.require(lib, [a, b], payload.get("use_anyway") or ())
    pick = payload.get("pick")
    plan = plan_pair(lib, a, b, pick if isinstance(pick, dict) else None)
    lib.put_plan(a, b, plan.model_dump(), user_edited=False, source=PLAN_SOURCE)
    return {"from": a, "to": b, "planned": True, "technique": plan.technique}
