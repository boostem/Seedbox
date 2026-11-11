"""Transition styles as data (#57): the style files, when each applies, what each writes over its base generator's plan
(a snapshot per style in tests/snapshots/styles/), the genre families, and the API.

After changing a style on purpose: `SEEDBOX_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest tests/test_seedbox2_styles.py`
and look at the diff of the snapshots.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from musicreco import mixplan
from seedbox2.library.repo import Library
from seedbox2.mix import convert, genre, offline, stemplan, styles
from seedbox2.mix.plan import MixPlan

from .fixtures import Spec, analysis, track

SNAPSHOTS = Path(__file__).parent / "snapshots" / "styles"
UPDATE = os.environ.get("SEEDBOX_UPDATE_SNAPSHOTS") == "1"

# drum and bass: A's last build ends 16 bars from its end on a drop, B's first drop comes after an 8-bar intro and build
DNB_A = Spec("dnb_a", bpm=174.0, bars=128, outro_bars=8, outro_drums=False, drops=[104])
DNB_B = Spec("dnb_b", bpm=174.0, bars=128, intro_bars=8, drops=[16])
# a sung pair with drops (choruses) on both sides, for the blends and cuts
POP_A = Spec("pop_a", bars=128, outro_bars=16, drops=[40, 88], vocals=[(24, 112)])
POP_B = Spec("pop_b", bars=128, intro_bars=16, intro_drums=False, drops=[32], vocals=[(0, 64)])
CLUB_A = Spec("club_a", bars=128, outro_bars=32)
CLUB_B = Spec("club_b", bars=128, intro_bars=32)

# style -> the pair its snapshot plans, and the context beyond what the analyses say
CASES: dict[str, tuple[Spec, Spec, dict]] = {
    "long_blend": (CLUB_A, CLUB_B, {}),
    "phrase_blend": (CLUB_A, CLUB_B, {}),
    "bass_swap": (CLUB_A, CLUB_B, {}),
    "drop_swap": (POP_A, POP_B, {}),
    "quick_cut": (POP_A, POP_B, {}),
    "echo_out": (CLUB_A, CLUB_B, {}),
    "filter_sweep": (CLUB_A, CLUB_B, {}),
    "loop_roll": (CLUB_A, CLUB_B, {}),
    "slam": (DNB_A, DNB_B, {}),
    "reverb_wash": (CLUB_A, CLUB_B, {}),
    "spinback": (CLUB_A, CLUB_B, {}),
    "brake": (CLUB_A, CLUB_B, {}),
    "stutter": (CLUB_A, CLUB_B, {}),
    "double_drop": (DNB_A, DNB_B, {"genre_a": "drum_and_bass", "genre_b": "drum_and_bass"}),
    "rolling_blend": (Spec("roll_a", bpm=174.0, bars=160, outro_bars=32), Spec("roll_b", bpm=174.0, bars=160, intro_bars=32),
                      {"genre_a": "drum_and_bass", "genre_b": "drum_and_bass"}),
    "chorus_swap": (POP_A, POP_B, {"genre_a": "pop", "genre_b": "pop"}),
    "acapella_intro": (CLUB_A, POP_B, {"genre_a": "house", "genre_b": "house", "b_stems": True}),
    "echo_freeze": (POP_A, POP_B, {"genre_a": "hip_hop", "genre_b": "hip_hop"}),
    "filter_wash": (CLUB_A, CLUB_B, {"genre_a": "trance", "genre_b": "trance"}),
    "power_cut": (DNB_A, DNB_B, {"genre_a": "dubstep", "genre_b": "dubstep"}),
}


def _ctx(a: Spec, b: Spec, **extra) -> styles.Context:
    return styles.Context(**{**styles.context(analysis(a), analysis(b)).__dict__, **extra})


def _generate(a: Spec, b: Spec):
    a_an, b_an = analysis(a), analysis(b)
    return lambda technique, bars: mixplan.generate(track(a), a_an, track(b), b_an, "", technique=technique, bars=bars)


def _load(raw: dict) -> MixPlan:
    return convert.load(raw, from_track="a", to_track="b")


def _styled(style_id: str) -> MixPlan:
    a, b, extra = CASES[style_id]
    style = styles.by_id(style_id)
    assert style is not None
    base = _load(_generate(a, b)(style.base, style.bars))
    out = styles.apply(style, base, _ctx(a, b, **extra))
    assert out is not None, f"{style_id} doesn't apply to its own case"
    return out


# --- the files ------------------------------------------------------------------------------------------------------


def test_every_style_has_a_snapshot_case():
    assert sorted(CASES) == sorted(s.id for s in styles.all_styles())


def test_one_technique_list():
    assert styles.TECHNIQUES[: len(mixplan.TECHNIQUES)] == mixplan.TECHNIQUES  # the ports, in Seedbox 1's order
    assert len(set(styles.TECHNIQUES)) == len(styles.TECHNIQUES) == 20
    for new in ("double drop", "rolling blend", "chorus swap", "acapella intro", "echo freeze", "filter wash", "power cut"):
        assert new in styles.TECHNIQUES
        assert styles.render_technique(new) in mixplan.TECHNIQUES
    assert styles.render_technique("something an editor typed") == "long blend"
    assert all(styles.kind_of(t) in styles.KINDS for t in styles.TECHNIQUES)


@pytest.mark.parametrize(("text", "error"), [
    ('technique = "x"\nlabel = "X"\nkind = "wobble"\nbase = "slam"', "kind"),
    ('technique = "x"\nlabel = "X"\nkind = "cut"\nbase = "scratch"', "base"),
    ('technique = "x"\nlabel = "X"\nkind = "cut"\nbase = "slam"\n[lanes.a]\nvolume = [["middle", 1.0]]', "middle"),
    ('technique = "x"\nlabel = "X"\nkind = "cut"\nbase = "slam"\n[lanes.a]\nwobble = [["end", 1.0]]', "wobble"),
    ('technique = "x"\nlabel = "X"\nkind = "cut"\nbase = "slam"\n[when]\nvocals = ["choir"]', "choir"),
    ('technique = "x"\nlabel = "X"\nkind = "cut"\nbase = "slam"\ncolour = "red"', "colour"),
])  # fmt: skip
def test_a_bad_style_file_is_refused(tmp_path: Path, text: str, error: str):
    for s in styles.SEED1_TECHNIQUES:  # a folder needs every port
        (tmp_path / f"{s.replace(' ', '_')}.toml").write_text(f'technique = "{s}"\nlabel = "{s}"\nkind = "cut"\nbase = "{s}"\n')
    (tmp_path / "bad.toml").write_text(text)
    with pytest.raises(styles.StyleError, match=error):
        styles.load_folder(tmp_path)


def test_a_folder_without_a_port_is_refused(tmp_path: Path):
    (tmp_path / "slam.toml").write_text('technique = "slam"\nlabel = "Slam"\nkind = "cut"\nbase = "slam"\n')
    with pytest.raises(styles.StyleError, match="ports"):
        styles.load_folder(tmp_path)


def test_where():
    span = styles.Span(-32.0, 64.0, 0.0)
    assert [span.at(w) for w in ("start", "end", "swap", "cut", "0.5", "end-2b", "start+1bar", "0.25+4b")] == \
        [-32.0, 64.0, 0.0, 0.0, 16.0, 62.0, -28.0, -4.0]  # fmt: skip


# --- snapshots ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("style_id", sorted(CASES))
def test_snapshot(style_id: str):
    got = styles.snapshot(_styled(style_id))
    path = SNAPSHOTS / f"{style_id}.json"
    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=1) + "\n")
    assert got == json.loads(path.read_text()), f"{style_id} changed: rerun with SEEDBOX_UPDATE_SNAPSHOTS=1 if meant"


@pytest.mark.parametrize("style_id", sorted(CASES))
def test_every_style_plays(style_id: str):
    """Through the stem rules (with and without stems) and Seedbox 1's renderer's validation."""
    plan = _styled(style_id)
    a, b, _ = CASES[style_id]
    for has in (False, True):
        ta = stemplan.track_from_analysis(analysis(a), has_stems=has)
        tb = stemplan.track_from_analysis(analysis(b), has_stems=has)
        out, _ = stemplan.plan(plan, ta, tb)
        assert mixplan.validate(offline.to_seedbox1(out))["technique"] == styles.render_technique(plan.technique)
    assert plan.technique == styles.by_id(style_id).technique  # type: ignore[union-attr]


def test_ports_are_the_generator_s_plans():
    for style in styles.all_styles():
        if style.is_port:
            a, b, _ = CASES[style.id]
            assert _styled(style.id) == _load(_generate(a, b)(style.base, None))


def test_double_drop_lines_the_drops_up():
    plan = _styled("double_drop")
    a_vol = [float(np.asarray(mixplan.lane_value([k.model_dump() for k in plan.a.lanes.volume], x))) for x in (0.0, 32.0, 63.0)]
    assert a_vol[0] == a_vol[1] == 1.0 and a_vol[2] < 0.1  # A's drop plays on under B's, then goes
    b_low = [k.model_dump() for k in plan.b.lanes.eq_low]
    assert mixplan.lane_value(b_low, -1.0) == -30.0 and mixplan.lane_value(b_low, 0.0) == 0.0  # B's bass on the drop
    a_low = [k.model_dump() for k in plan.a.lanes.eq_low]
    assert mixplan.lane_value(a_low, 0.0) == -30.0
    assert plan.b.play_from_beat == -32.0 and plan.a.play_to_beat >= 64.0
    assert not plan.a.loops


def test_a_style_whose_need_fails_is_passed_over():
    """No drops on the cut (club tracks): a double drop doesn't apply even in drum and bass."""
    ctx = _ctx(CLUB_A, CLUB_B, genre_a="drum_and_bass", genre_b="drum_and_bass", bpm=174.0)
    base = _load(_generate(CLUB_A, CLUB_B)("slam", None))
    assert styles.apply(styles.by_id("double_drop"), base, ctx) is None  # type: ignore[arg-type]


# --- choosing ---------------------------------------------------------------------------------------------------------


def _choose(a: Spec, b: Spec, **extra) -> tuple[MixPlan, dict]:
    return styles.plan(_ctx(a, b, **extra), _generate(a, b), _load)


@pytest.mark.parametrize(("a", "b", "extra", "style"), [
    (DNB_A, DNB_B, {"genre_a": "drum_and_bass", "genre_b": "drum_and_bass"}, "double_drop"),
    (DNB_A, DNB_B, {"genre_a": "dubstep", "genre_b": "dubstep"}, "double_drop"),
    (DNB_A, DNB_B, {"genre_a": "trap", "genre_b": "trap"}, "power_cut"),
    (DNB_A, DNB_B, {}, "slam"),  # no genre: the generator's pick
    (CASES["rolling_blend"][0], CASES["rolling_blend"][1], {"genre_a": "drum_and_bass"}, "rolling_blend"),
    (POP_A, POP_B, {"genre_a": "hip_hop", "genre_b": "hip_hop"}, "chorus_swap"),
    (CLUB_A, POP_B, {"genre_a": "house", "b_stems": True}, "acapella_intro"),
    (CLUB_A, POP_B, {"genre_a": "house"}, "long_blend"),  # B's vocal can't come in alone without its stems
    (CLUB_A, CLUB_B, {"genre_a": "trance", "genre_b": "trance"}, "filter_wash"),
    (CLUB_A, CLUB_B, {"genre_a": "hip_hop"}, "quick_cut"),  # hip-hop cuts
])  # fmt: skip
def test_the_genre_picks_the_style(a: Spec, b: Spec, extra: dict, style: str):
    plan, report = _choose(a, b, **extra)
    assert report["style"] == style, report
    assert plan.technique == styles.by_id(style).technique  # type: ignore[union-attr]


def test_without_a_genre_nothing_changes():
    """An unknown genre plans exactly what the generator alone planned, for every pair the snapshots use."""
    for a, b, _ in CASES.values():
        plan, report = _choose(a, b)
        assert plan == _load(_generate(a, b)(None, None)), (a.id, b.id, report)


def test_conditions():
    ctx = styles.Context(genre_a="drum_and_bass", genre_b="house", bpm=87.0, energy_a=0.9, energy_b=0.9, auto="slam")
    dd = styles.by_id("double_drop")
    assert styles.why_not(dd, ctx) == "genre of B"  # type: ignore[arg-type]
    ctx = styles.Context(**{**ctx.__dict__, "genre_b": None})
    assert styles.why_not(dd, ctx) is None  # type: ignore[arg-type]  # 87 BPM is half of 174; B's genre unknown
    assert styles.why_not(dd, styles.Context(**{**ctx.__dict__, "energy_b": 0.3})) == "energy of B"  # type: ignore[arg-type]
    assert styles.why_not(dd, styles.Context(**{**ctx.__dict__, "tempo_gap": 0.05})) == "tempo gap"  # type: ignore[arg-type]
    rb = styles.by_id("rolling_blend")
    assert styles.why_not(rb, ctx) == "not instead of the generator's pick"  # type: ignore[arg-type]
    assert styles.context(analysis(POP_A), analysis(POP_B)).vocals == "both"
    assert styles.context(analysis(CLUB_A), analysis(POP_B)).vocals == "b"
    assert styles.context(analysis(CLUB_A), analysis(CLUB_B)).vocals == "none"
    assert styles.tempo_gap(87.0, 174.0) == 0.0


# --- genre ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("tag", "family"), [
    ("Drum & Bass", "drum_and_bass"), ("Drum 'n' Bass", "drum_and_bass"), ("DnB", "drum_and_bass"), ("Tech House", "house"),
    ("Hip-Hop/Rap", "hip_hop"), ("Rap/Trap", "trap"), ("Dance / Pop", "pop"), ("R&B/Soul", "rnb"), ("Melodic Techno", "techno"),
    ("Psy-Trance", "trance"), ("UK Garage", "uk_garage"), ("Garage Rock", None), ("Future Bass", None), ("", None), (None, None),
])  # fmt: skip
def test_genre_from_tag(tag, family):
    assert genre.from_tag(tag) == family


def test_zero_shot():
    rng = np.random.default_rng(1)
    vecs = {f: v / np.linalg.norm(v) for f, v in (("house", rng.normal(size=512)), ("techno", rng.normal(size=512)))}
    audio = vecs["techno"] + 0.3 * rng.normal(size=512) / np.sqrt(512)
    audio = np.stack([audio / np.linalg.norm(audio)] * 3)
    ranked = genre.zero_shot(audio, {k: v[None] for k, v in vecs.items()})
    assert ranked[0]["genre"] == "techno" and ranked[0]["p"] > 0.99
    assert genre.zero_shot(np.zeros((0, 512)), {k: v[None] for k, v in vecs.items()}) == []


def test_every_style_genre_is_a_family():
    ids = {f.id for f in genre.families()}
    for s in styles.all_styles():
        assert set(s.when.genres) <= ids and set(s.boost) <= ids, s.id


def test_genre_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    lib = Library.open(tmp_path / "library.sqlite3")
    lib.upsert_track("h", 200.0, tags={"genre": "Deep House"})
    assert genre.effective(lib, "h") == {"genre": "house", "label": "House", "from": "tag", "p": None}
    lib.upsert_track("x", 200.0)
    monkeypatch.setattr(genre, "clap_guess", lambda lib, h: [{"genre": "trance", "p": 0.7}])
    assert genre.effective(lib, "x")["from"] == "clap" and genre.effective(lib, "x")["genre"] == "trance"
    monkeypatch.setattr(genre, "clap_guess", lambda lib, h: [{"genre": "trance", "p": 0.2}])
    assert genre.effective(lib, "x")["from"] == "unknown"
    genre.set_override(lib, "h", "techno")
    assert genre.effective(lib, "h")["from"] == "user" and genre.effective(lib, "h")["genre"] == "techno"
    with pytest.raises(ValueError):
        genre.set_override(lib, "h", "polka")
    genre.clear_override(lib, "h")
    assert genre.effective(lib, "h")["genre"] == "house"
    lib.close()


# --- the API ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    library = Library.open(tmp_path / "library.sqlite3")
    app = create_app()
    app.state.library = library
    with TestClient(app) as c:
        c.library = library  # type: ignore[attr-defined]
        yield c
    library.close()


def test_api_styles(client):
    body = client.get("/api/mixes/styles").json()
    assert body["techniques"] == list(styles.TECHNIQUES)
    assert {s["id"] for s in body["styles"]} == set(CASES)
    dd = next(s for s in body["styles"] if s["id"] == "double_drop")
    assert dd["kind"] == "swap" and dd["when"]["genres"] == ["drum_and_bass", "dubstep"] and dd["when"]["needs"] == ["drops"]
    assert {"id": "house", "label": "House"} in body["genres"]


def test_api_genre_override(client):
    assert client.get("/api/analysis/none/genre").status_code == 404
    client.library.upsert_track("h", 200.0, tags={"genre": "Drum & Bass"})
    body = client.get("/api/analysis/h/genre").json()
    assert body["effective"]["genre"] == "drum_and_bass" and body["effective"]["from"] == "tag"
    assert body["tag"] == "Drum & Bass" and body["clap"] == []
    assert client.put("/api/analysis/h/genre/override", json={"genre": "polka"}).status_code == 422
    body = client.put("/api/analysis/h/genre/override", json={"genre": "dubstep"}).json()
    assert body["effective"] == {"genre": "dubstep", "label": "Dubstep", "from": "user", "p": None}
    assert client.delete("/api/analysis/h/genre/override").json()["effective"]["from"] == "tag"
