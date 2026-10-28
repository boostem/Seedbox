"""Writes src/mock/fixtures/engine.json: what the Python engine answers for the mock crates, so mock mode replays
the real planner instead of a TypeScript copy of it.

Run from frontend/: python3 scripts/engine-fixtures.py   (deno task fixtures:engine; needs numpy + scipy) after a
change to musicreco's scoring, ordering, transitions, set plan or mix plans, or to fixtures/tunebat.json.

Inputs: the tunebat tracks in fixtures/tunebat.json and a synthetic analysis per track (per-bar features and sections,
deterministic per track id; the mock server draws the waveform from them). Outputs:
  analyses   track id -> TrackAnalysis without its waveform
  recs       seed id -> [{id, score, rule_reason}] best first (engine.rank + mixing.passes_rule); every seed gets the
             same candidates (tunebat's recs for the Favour seed, a few FISHER tracks, Favour itself)
  crates     the mock library: folder, seed, running order (transitions.order_set), energy ratings, age
  ratings    track id -> mixing.energy_ratings over every crate track, for tracks a download adds to a crate
  sets       running order ("id,id,...") -> crate_set's transitions (suggest_set + setplan.annotate) with every
             track analysed, for every crate's running order
  pairs      "a>b" -> transitions.suggest with both analyses; "a>b" in key_only: without them (ready = false)
  names      track id -> its crate folder / file name (library.sanitize_filename)
  plans      folder -> "a>b" -> {"": default, technique: its default length, "technique@bars": ...} -> index into plan_bodies
             (mixplan.generate) for every pair the running orders play; "technique@bars" only for the first crate
"""

from __future__ import annotations

import json
import math
import sys
from itertools import pairwise
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from musicreco import mixing, mixplan, setplan, transitions
from musicreco.library import sanitize_filename
from musicreco.models import Track
from musicreco.structure import phrase_grid

MOCK = HERE.parent / "src" / "mock"
BPM_TOL = 0.1
MIX_LENGTH = "normal"
TECHNIQUES = ("long blend", "phrase blend", "bass swap", "drop swap", "quick cut", "echo out", "filter sweep",
              "loop roll", "slam", "reverb wash")  # fmt: skip
# FISHER tracks from the search fixture used as extra (mostly rule-failing) recommendations
EXTRA_REC_IDS = {"1xtDflWAisSMU7k6QHG71U", "5zX4JjkoT2pgvlWPnjgVNQ", "5bIJREvmwLztjkTP5am1u9"}


# --- deterministic randomness (the same as src/mock/rng.ts) ---------------------------------------------------------


def fnv(s: str) -> int:
    h = 2166136261
    for c in s.encode("utf-16-le")[::2]:  # charCodeAt of the (ASCII) ids
        h = ((h ^ c) * 16777619) & 0xFFFFFFFF
    return h


def rng(seed: str):
    a = fnv(seed)

    def imul(x: int, y: int) -> int:
        return (x * y) & 0xFFFFFFFF

    def nxt() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = a
        t = imul(t ^ (t >> 15), t | 1)
        t ^= (t + imul(t ^ (t >> 7), t | 61)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return nxt


# --- synthetic analyses ---------------------------------------------------------------------------------------------


def r3(x: float) -> float:
    return round(max(0.0, min(1.0, x)) * 1000) / 1000


def analysis(t: dict) -> dict:
    """A plausible TrackAnalysis for a tunebat track: intro, builds, drops, a breakdown and an outro on the bar grid,
    drum-only or musical edges, a vocal on every other 8 bars of the body in most tracks."""
    R = rng(f"an:{t['id']}")
    duration = t["duration_s"] or 200.0
    bpm = t["bpm"] or 124.0
    if bpm < 95:
        bpm *= 2
    bar_s = 240 / bpm
    fdb = 0.04 + R() * 0.31
    bar_count = max(16, math.floor((duration - fdb) / bar_s))
    ri = R()
    intro_bars = 32 if ri < 0.5 else 8 if ri < 0.62 else 16  # some short 8-bar intros (the planner loops them)
    outro_bars = 32 if R() < 0.55 else 16
    vocal_track = R() < 0.7

    plan = [("intro", intro_bars), ("build", 8), ("drop", 32), ("breakdown", 16 if R() < 0.5 else 32), ("build", 8),
            ("drop", 32)]  # fmt: skip
    avail = bar_count - outro_bars
    secs: list[list] = []
    bar = 0
    for kind, length in plan:
        if bar >= avail:
            break
        end = min(avail, bar + length)
        if avail - end < 8:
            end = avail  # no stub sections: fold a short remainder into this one
        secs.append([kind, bar, end])
        bar = end
    if bar < avail:
        secs.append(["body", bar, avail])
    secs.append(["outro", avail, bar_count])

    def at(b: float) -> float:
        return round(min(duration, fdb + b * bar_s), 2)

    def kind_at(b: int) -> list:
        return next((s for s in secs if s[1] <= b < s[2]), secs[-1])

    energy, low, vocal = [], [], []
    for b in range(bar_count):
        kind, s0, s1 = kind_at(b)
        f = (b - s0) / max(1, s1 - s0)
        n = (R() - 0.5) * 0.08
        e = {"intro": 0.42 + f * 0.12, "build": 0.55 + f * 0.3, "drop": 0.9, "breakdown": 0.32, "body": 0.74,
             "outro": 0.58 - f * 0.25}[kind] + n  # fmt: skip
        lo = {"intro": 0.82, "build": 0.45 + f * 0.2, "drop": 0.97, "breakdown": 0.08, "body": 0.85,
              "outro": 0.8 - f * 0.2}[kind] + n  # fmt: skip
        sung = vocal_track and kind in ("breakdown", "drop", "body") and (b >> 3) % 2 == 0
        energy.append(r3(e))
        low.append(r3(lo))
        vocal.append(r3(0.65 + R() * 0.3 if sung else R() * 0.15))

    intro_drums = R() > 0.25
    outro_drums = R() > 0.3
    intro_vocals = not intro_drums and R() < 0.4
    outro_vocals = not outro_drums and R() < 0.5
    sections = [{"kind": k, "start_bar": a, "end_bar": b, "start_s": at(a), "end_s": round(duration, 2) if b == bar_count
                 else at(b)} for k, a, b in secs]  # fmt: skip
    cues = [{"bar": 0, "time_s": round(fdb, 2), "label": f"mix in ({intro_bars}-bar intro)"}]
    drops = 0
    for s in sections:
        if s["kind"] == "drop":
            cues.append({"bar": s["start_bar"], "time_s": s["start_s"], "label": "drop 2" if drops else "drop"})
            drops += 1
        elif s["kind"] == "breakdown":
            cues.append({"bar": s["start_bar"], "time_s": s["start_s"], "label": "breakdown"})
    cues.append({"bar": avail, "time_s": at(avail), "label": f"mix out ({outro_bars}-bar outro)"})
    phrases = phrase_grid([s["start_bar"] for s in sections], 0, bar_count, energy)
    return {
        "version": 5, "track_id": t["id"], "duration_s": duration, "bpm": bpm, "bpm_source": "tunebat",
        "first_downbeat_s": round(fdb, 2), "bar_s": bar_s, "bar_count": bar_count, "phrase_offset": 0,
        "phrases": [{"bar": b, "level": lv} for b, lv in phrases],
        "energy_per_bar": energy, "low_per_bar": low, "vocal_per_bar": vocal, "sections": sections,
        "intro": {"bars": intro_bars, "start_s": round(fdb, 2), "end_s": at(intro_bars), "drums_only": intro_drums,
                  "has_vocals": intro_vocals},
        "outro": {"bars": outro_bars, "start_s": at(avail), "end_s": round(duration, 2), "drums_only": outro_drums,
                  "has_vocals": outro_vocals},
        "cues": cues,
    }  # fmt: skip


# --- the mock library -----------------------------------------------------------------------------------------------


def track(d: dict) -> Track:
    return Track(
        id=d["id"], name=d["name"], artists=list(d["artists"]), album=d["album"], release_date=d["release_date"],
        duration_ms=round(d["duration_s"] * 1000), popularity=d["popularity"], key=d["key"], camelot=d["camelot"],
        bpm=d["bpm"], energy=d["energy"], danceability=d["danceability"], cover_url=d["cover_url"],
    )  # fmt: skip


def main() -> None:
    fx = json.loads((MOCK / "fixtures" / "tunebat.json").read_text("utf-8"))
    pool: dict[str, Track] = {}
    for d in [fx["seed"], *fx["recs"], *fx["search"]["items"]]:
        pool.setdefault(d["id"], track(d))
    favour = pool[fx["seed"]["id"]]
    tunebat_recs = [pool[d["id"]] for d in fx["recs"]]
    extras = [pool[d["id"]] for d in fx["search"]["items"]
              if d["id"] in EXTRA_REC_IDS or (d["artists"][:1] == ["FISHER"] and d["bpm"] >= 120)]  # fmt: skip
    candidates = list({t.id: t for t in [*tunebat_recs, *extras, favour]}.values())

    def recs(seed: Track) -> list[Track]:
        """The candidates for `seed` best first by mixing.score, as engine.rank orders them."""
        cands = [t for t in candidates if t.id != seed.id and t.display_name != seed.display_name]
        return sorted(cands, key=lambda t: -mixing.score(seed, t, tol=BPM_TOL).value)

    def score(seed: Track, t: Track) -> dict:
        s = mixing.score(seed, t, tol=BPM_TOL)
        return {"value": s.value, "passed": s.passed, "move": s.move, "chips": list(s.chips)}

    rec_fx = {
        seed.id: [{"id": t.id, "score": score(seed, t), "rule_reason": mixing.passes_rule(seed, t, tol=BPM_TOL)[1]}
                  for t in recs(seed)]
        for seed in pool.values()
    }  # fmt: skip

    names = {tid: sanitize_filename(t.display_name) for tid, t in pool.items()}
    analyses = {tid: analysis(t.to_json()) for tid, t in pool.items()}
    for an in analyses.values():
        an["duration_s"] = round(an["duration_s"], 3)

    def top(seed: Track, n: int, passed_only: bool = False) -> list[Track]:
        out = [t for t in recs(seed) if not passed_only or mixing.score(seed, t, tol=BPM_TOL).passed]
        return out[:n]

    by_name = {t.name: t for t in pool.values()}
    rain = pool["1xtDflWAisSMU7k6QHG71U"]
    more_baby = by_name.get("More Baby", tunebat_recs[6])
    dont_stop = by_name.get("Don't Stop", tunebat_recs[1])
    atmosphere = next((pool[d["id"]] for d in fx["search"]["items"] if d["name"] == "Atmosphere"),
                      pool[fx["search"]["items"][5]["id"]])  # fmt: skip
    shes_gone = tunebat_recs[0]
    library = [
        (favour, [favour, *top(favour, 7, passed_only=True)], 2.2, None),
        (rain, [rain, *top(rain, 8)], 26, None),
        (more_baby, [more_baby, *top(more_baby, 10)], 74, None),
        (dont_stop, [dont_stop, *top(dont_stop, 7)], 24 * 12, None),
        (atmosphere, [atmosphere, *top(atmosphere, 5)], 24 * 40, None),
        (shes_gone, [shes_gone, *top(shes_gone, 11)], 24 * 64, None),
        (None, tunebat_recs[3:10], 24 * 300, "Warm-up Ideas (old crate)"),
    ]

    crates, pair_ids, orders = [], set(), []
    for seed, tracks, hours, folder in library:
        an = {t.id: analyses[t.id] for t in tracks}
        ordered = transitions.order_set(seed or tracks[0], tracks, an, bpm_tol=BPM_TOL)
        crates.append({
            "folder": folder or names[seed.id],
            "seed_id": seed.id if seed else None,
            "track_ids": [t.id for t in ordered],
            "hours_ago": hours,
            "ratings": mixing.energy_ratings([t for t in ordered if t.energy > 0]),
        })  # fmt: skip
        orders.append(ordered)
        pair_ids |= {(a.id, b.id) for a in tracks for b in tracks if a.id != b.id}
    every = list({t.id: t for c in library for t in c[1]}.values())
    ratings = mixing.energy_ratings([t for t in every if t.energy > 0])

    pairs, key_only = {}, {}
    for a_id, b_id in sorted(pair_ids):
        a, b = pool[a_id], pool[b_id]
        key = f"{a_id}>{b_id}"
        pairs[key] = transitions.suggest(a, analyses[a_id], b, analyses[b_id], bpm_tol=BPM_TOL, mix_length=MIX_LENGTH)
        key_only[key] = transitions.suggest(a, None, b, None, bpm_tol=BPM_TOL, mix_length=MIX_LENGTH)

    sets, plans, bodies, index = {}, {}, [], {}

    def keep(plan: dict) -> int:
        """Index of `plan` in `bodies` (identical plans are stored once)."""
        text = json.dumps(plan, sort_keys=True)
        if text not in index:
            index[text] = len(bodies)
            bodies.append(plan)
        return index[text]

    for n, (crate, ordered) in enumerate(zip(crates, orders)):
        an = {t.id: analyses[t.id] for t in ordered}
        trs = transitions.suggest_set(ordered, an, bpm_tol=BPM_TOL, mix_length=MIX_LENGTH)
        trs = setplan.annotate(ordered, an, trs, mix_length=MIX_LENGTH)
        sets[",".join(crate["track_ids"])] = trs
        for (a, b), tr in zip(pairwise(ordered), trs):
            args = (a, an[a.id], b, an[b.id], crate["folder"])
            own = pairs[f"{a.id}>{b.id}"]
            entry = plans.setdefault(crate["folder"], {})[f"{a.id}>{b.id}"] = {
                "": keep(mixplan.generate(*args, technique=tr["plan_technique"], transition=own, mix_length=MIX_LENGTH))
            }
            for tech in TECHNIQUES:
                entry[tech] = keep(mixplan.generate(*args, technique=tech, transition=own, mix_length=MIX_LENGTH))
                if n == 0:  # the screenshot crate: every blend length the mix editor offers too
                    for bars in mixplan.BARS:
                        entry[f"{tech}@{bars}"] = keep(mixplan.generate(*args, technique=tech, bars=bars,
                                                                        transition=own, mix_length=MIX_LENGTH))  # fmt: skip

    out = {
        "analyses": analyses, "names": names, "recs": rec_fx, "crates": crates, "ratings": ratings, "sets": sets,
        "pairs": pairs, "key_only": key_only, "plans": plans, "plan_bodies": bodies,
    }  # fmt: skip
    dest = MOCK / "fixtures" / "engine.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", "utf-8")
    print(f"wrote {dest.relative_to(ROOT)}: {dest.stat().st_size // 1024} KB, {len(analyses)} analyses, "
          f"{len(pairs)} pairs, {sum(map(len, plans.values()))} planned pairs, {len(bodies)} plans")  # fmt: skip


if __name__ == "__main__":
    main()
