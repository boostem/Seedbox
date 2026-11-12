"""Real DJ-mix transitions from the DJ Mix dataset (#55): `python -m seedbox2.eval djmix`, without downloads."""

from __future__ import annotations

import numpy as np
import pytest

from seedbox2.eval import djmix
from seedbox2.eval.djmix import Beats


def test_timestamps() -> None:
    assert djmix.timestamp_s("[05] Artist - Title") == 300.0
    assert djmix.timestamp_s("[06:15] Artist - Title") == 375.0
    assert djmix.timestamp_s("[1:02:30] Artist - Title") == 3750.0
    assert djmix.timestamp_s("Artist - Title") is None


def _mix(i: int, dj: str, genres: list[str], ids: list[str | None], source: str = "soundcloud") -> dict:
    tags = [{"key": f"Category:{dj}"}] + [{"key": f"Category:{g}"} for g in genres]
    return {"id": f"mix{i:04}", "audio_source": source, "audio_url": f"https://example.invalid/{i}", "tags": tags,
            "title": f"mix {i}", "tracklist": [{"id": y, "title": f"[{k * 5:02}] t{k}"} for k, y in enumerate(ids)]}  # fmt: skip


def test_select_takes_the_genres_one_mix_per_dj_and_enough_transitions() -> None:
    ids = [f"y{k}" for k in range(8)]
    data = [
        _mix(1, "DJ A", ["Deep House", "House"], ids),
        _mix(2, "DJ A", ["Techno"], ids),  # same DJ
        _mix(3, "DJ B", ["Trance"], ids),  # genre not asked for
        _mix(4, "DJ C", ["Techno"], ids[:3] + [None] + ids[3:5]),  # too few consecutive pairs
        _mix(5, "DJ D", ["Tech House"], ids, source="html_audio"),  # no downloader for it
        _mix(6, "DJ E", ["Tech House"], ids),
    ]
    got = {m["id"] for m in djmix.select(data, 10)}
    assert "mix0006" in got and len(got & {"mix0001", "mix0002"}) == 1 and len(got) == 2
    assert djmix.genre_of(data[0]) == "deep house"  # the most specific genre
    assert djmix.select(data, 10, genres=["trance"])[0]["id"] == "mix0003"


def _music(n: int, rng: np.random.Generator) -> np.ndarray:
    """Half-beat mel features with the repetition of dance music: an 8-bar loop plus slow variation."""
    loop = rng.normal(0, 1, (64, djmix.BANDS))
    slow = np.repeat(rng.normal(0, 1, (n // 64 + 1, djmix.BANDS)), 64, axis=0)[:n]  # changes every 8 bars
    return np.tile(loop, (n // 64 + 1, 1))[:n] + slow + rng.normal(0, 0.3, (n, djmix.BANDS)) + 5.0


@pytest.fixture(scope="module")
def mix_case() -> dict:
    """Two tracks in a mix: A from its start at mix half beat 100; B comes in at mix half beat 100 + 400 = 500 from
    its own half beat 32, and plays over A's last 96 half beats (A ends at its half beat 496)."""
    rng = np.random.default_rng(1)
    a, b = _music(496, rng), _music(600, rng)
    mix = rng.normal(0, 1, (1300, djmix.BANDS)) + 5.0
    mix[100:500] = a[:400]
    mix[500:596] = 0.5 * a[400:496] + 0.5 * b[32:128]  # the blend
    mix[596:1068] = b[128:600]
    return {"a": a, "b": b, "mix": mix}


def test_match_finds_where_a_track_plays(mix_case: dict) -> None:
    ma = djmix.match(mix_case["a"], mix_case["mix"])
    mb = djmix.match(mix_case["b"], mix_case["mix"])
    assert ma is not None and mb is not None
    assert abs(ma.offset - 100) <= 1 and ma.t0 <= 4 and ma.t1 >= 480  # a half beat of slack either way
    assert abs(mb.offset - 468) <= 1 and abs(mb.t0 - 32) <= 4 and mb.t1 >= 590
    assert djmix.accepted(ma) and djmix.accepted(mb)
    assert not djmix.accepted(djmix.match(mix_case["b"], mix_case["mix"], lo=0, hi=300))  # not where it isn't


def test_transitions_in_track_time(mix_case: dict) -> None:
    def beats(f: np.ndarray, spb: float, t0: float = 0.0) -> Beats:  # half beats of `spb` / 2 seconds
        return Beats(t0 + np.arange(len(f) + 1) * spb / 2, f)

    mixb = beats(mix_case["mix"], 0.5)
    tracks = {"ya": beats(mix_case["a"], 0.5, 0.2), "yb": beats(mix_case["b"], 0.48, 0.1)}
    mix = {"id": "mix0001", "tags": [{"key": "Category:Techno"}],
           "tracklist": [{"id": "ya", "title": "a"}, {"id": "yb", "title": "b"}, {"id": None, "title": "?"}]}  # fmt: skip
    ts, rows = djmix.mix_transitions(mix, mixb, tracks, {"ya": "ha", "yb": "hb"})
    assert [r["ok"] for r in rows] == [True, True, False]
    (t,) = ts
    assert t["a"] == {"hash": "ha"} and t["b"] == {"hash": "hb"} and t["genre"] == "techno"
    # B comes in around A's half beat 400 (0.2 + 400 * 0.25 s) and its own half beat 32 (0.1 + 32 * 0.24 s)
    assert t["a_in_s"] == pytest.approx(100.2, abs=4 * 0.25)
    assert t["b_cue_s"] == pytest.approx(7.78, abs=4 * 0.24)
    assert t["length_s"] == pytest.approx(96 * 0.25, abs=8 * 0.25)
    # without B's hash (not analysed) there is nothing to learn from
    assert djmix.mix_transitions(mix, mixb, tracks, {"ya": "ha"})[0] == []


def test_a_track_out_of_tracklist_order_is_dropped(mix_case: dict) -> None:
    mixb = Beats(np.arange(len(mix_case["mix"]) + 1) * 0.25, mix_case["mix"])
    tracks = {y: Beats(np.arange(len(f) + 1) * 0.25, f) for y, f in (("ya", mix_case["a"]), ("yb", mix_case["b"]))}
    mix = {"id": "m", "tags": [], "tracklist": [{"id": "yb", "title": ""}, {"id": "ya", "title": ""}]}
    ts, rows = djmix.mix_transitions(mix, mixb, tracks, {"ya": "ha", "yb": "hb"})
    assert ts == [] and [r["ok"] for r in rows] == [True, False]
