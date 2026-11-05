"""#70: a whole set as one mix. The tempo line, the streaming renderer, resuming, the formats and the job."""

from __future__ import annotations

import json
import os
from pathlib import Path

import mutagen
import numpy as np
import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from scipy.io import wavfile

from musicreco import mixplan, mixrender
from seedbox2.jobs.worker import Cancelled
from seedbox2.library import Library
from seedbox2.mix import convert, master, setchain, setexport, setrender, stream
from seedbox2.mix.plan import MixPlan, deck_time, lane_value
from seedbox2.server.app import create_app
from seedbox2.sets import export as set_export

from .fixtures import Spec, analysis, click_track, pad_track, track

SR = 44100
SPECS = [
    Spec("t0", bpm=124.0, bars=40, intro_bars=8, outro_bars=8),
    Spec("t1", bpm=126.0, bars=40, intro_bars=8, outro_bars=8),
    Spec("t2", bpm=122.0, bars=40, intro_bars=8, outro_bars=8),
]
FIXTURE = Path(__file__).parents[1] / "frontend2/src/features/nowplaying/setchain.fixture.json"


def _music(s: Spec, seed: int) -> np.ndarray:
    x, y = click_track(s, SR, 1000.0 + 500 * seed), pad_track(s, SR, 220.0 + 50 * seed)
    n = min(x.shape[1], y.shape[1])
    return (0.5 * x[:, :n] + 0.5 * y[:, :n]).astype(np.float32)


def _plans(specs: list[Spec] = SPECS, tech: str | None = None) -> list[MixPlan]:
    return [convert.load(mixplan.generate(track(a), analysis(a), track(b), analysis(b), "", technique=tech), from_track=a.id, to_track=b.id)
            for a, b in zip(specs, specs[1:])]  # fmt: skip


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    folder = tmp_path_factory.mktemp("set")
    out = []
    for seed, s in enumerate(SPECS):
        out.append(folder / f"{s.id}.wav")
        wavfile.write(out[-1], SR, _music(s, seed).T)
    return out


@pytest.fixture(scope="module")
def chain() -> list[MixPlan]:
    return setchain.plan_chain(_plans(), SPECS[-1].duration_s)


@pytest.fixture(scope="module")
def sources(files: list[Path]) -> list[setrender.Source]:
    return [setrender.Source(s.id, f"Artist - {s.id}", p, -2.0) for s, p in zip(SPECS, files)]


def _bus(chain: list[MixPlan], sources: list[setrender.Source]) -> np.ndarray:
    render = setrender.SetRender(chain, sources, SR)
    parts = []
    while not render.done:
        parts.append(render.step())
    return np.concatenate(parts, axis=1)


@pytest.fixture(scope="module")
def bus(chain: list[MixPlan], sources: list[setrender.Source]) -> np.ndarray:
    return _bus(chain, sources)


# --- the tempo line ---------------------------------------------------------------------------------------------------


def test_the_tempo_goes_straight_from_one_blend_to_the_next():
    plans = _plans()
    chain = setchain.plan_chain(plans, SPECS[-1].duration_s)
    out = setchain.blend_span(plans[0])[1]
    into = setchain.blend_span(plans[1])[0]
    v0, v1 = float(lane_value(plans[0].tempo, out)), float(lane_value(plans[1].tempo, into))
    # as stored, one plan's window ends part way through its drift and the next starts at the track's own tempo: a step
    assert abs(float(lane_value(plans[0].tempo, plans[0].window.end_beat)) - plans[1].tempo[0].value) > 1.0
    for beat in np.linspace(out, chain[0].window.end_beat, 8):
        assert min(v0, v1) - 1e-6 <= float(lane_value(chain[0].tempo, beat)) <= max(v0, v1) + 1e-6
    for beat in np.linspace(chain[1].window.start_beat, into, 8):
        assert min(v0, v1) - 1e-6 <= float(lane_value(chain[1].tempo, beat)) <= max(v0, v1) + 1e-6


def test_both_plans_play_the_same_tempo_where_one_takes_over(chain: list[MixPlan]):
    for before, plan in zip(chain, chain[1:]):
        leave, enter = setchain.handoff_beat(before), setchain.enter_beat(plan, before)
        assert float(deck_time(before.b, leave)) == pytest.approx(float(deck_time(plan.a, enter)), abs=1e-6)
        assert float(lane_value(before.tempo, leave)) == pytest.approx(float(lane_value(plan.tempo, enter)), abs=1e-3)


def test_the_blends_keep_their_tempo(chain: list[MixPlan]):
    plans = _plans()
    for stored, played in zip(plans, chain):
        start, end = setchain.blend_span(stored)
        for beat in np.linspace(start, end, 17)[:-1]:
            assert float(lane_value(played.tempo, beat)) == pytest.approx(float(lane_value(stored.tempo, beat)), abs=1e-6)


def test_an_edited_plan_keeps_its_keyframes():
    plans = _plans()
    plans[1] = plans[1].model_copy(update={"edited": True})
    chain = setchain.plan_chain(plans, SPECS[-1].duration_s)
    window = plans[1].window
    for k in plans[1].tempo:
        if window.start_beat <= k.beat:
            assert float(lane_value(chain[1].tempo, k.beat)) == pytest.approx(k.value, abs=1e-6)


def test_the_live_engine_reads_the_same_chain():
    """`planChain` in session.ts has a test against this file too. Regenerate with SEEDBOX_WRITE_FIXTURE=1."""
    plans = _plans()
    chain = setchain.plan_chain(plans, SPECS[-1].duration_s)
    data = {"plans": [p.model_dump() for p in plans], "duration": SPECS[-1].duration_s,
            "tempo": [[k.model_dump() for k in p.tempo] for p in chain],
            "windows": [p.window.model_dump() for p in chain]}  # fmt: skip
    data = json.loads(json.dumps(data))
    if os.environ.get("SEEDBOX_WRITE_FIXTURE"):
        FIXTURE.write_text(json.dumps(data, indent=0) + "\n")
    stored = json.loads(FIXTURE.read_text())
    assert stored["tempo"] == data["tempo"]
    assert stored["windows"] == data["windows"]


# --- the bus ----------------------------------------------------------------------------------------------------------


def test_the_set_plays_every_track_in_one_timeline(bus: np.ndarray, chain: list[MixPlan]):
    where = setrender.layout(chain, SR)
    assert bus.shape[1] == where.total
    assert where.track_starts == sorted(where.track_starts) and len(where.track_starts) == 3
    # longer than any track, shorter than all three end to end, with no silence anywhere
    durations = [s.duration_s for s in SPECS]
    assert max(durations) < where.total / SR < sum(durations)
    second = bus[:, SR // 2 : -SR // 2].reshape(2, -1)
    rms = np.sqrt((second[0, : second.shape[1] // SR * SR].reshape(-1, SR) ** 2).mean(axis=1))
    assert rms.min() > 1e-3


def test_no_click_where_a_plan_takes_over(bus: np.ndarray, chain: list[MixPlan]):
    """Pads and clicks: at a handoff the sample steps are no bigger than anywhere around it."""
    where = setrender.layout(chain, SR)
    for g in where.starts[1:]:
        around = np.abs(np.diff(bus[:, g - SR : g + SR], axis=1)).max()
        at = np.abs(np.diff(bus[:, g - 2000 : g + 2000], axis=1)).max()
        assert at <= around + 1e-6


def test_same_audio_however_it_is_cut_up(chain: list[MixPlan], sources: list[setrender.Source], bus: np.ndarray, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(setrender, "CHUNK", 5000)
    other = _bus(chain, sources)
    assert other.shape == bus.shape
    assert 20 * np.log10(np.abs(other - bus).max() + 1e-12) < -90


def _two_tracks(files: list[Path], music) -> tuple[list[Spec], list[Path]]:
    """Two tracks at one tempo, so that nothing is stretched."""
    folder = files[0].parent
    specs = [SPECS[0], Spec("same", bpm=124.0, bars=40, intro_bars=8, outro_bars=8)]
    paths = [folder / f"{music.__name__}{i}.wav" for i in range(2)]
    for i, (spec, path) in enumerate(zip(specs, paths)):
        wavfile.write(path, SR, music(spec, i).T)
    return specs, paths


def test_a_block_seam_at_the_tracks_own_tempo_is_a_copy(files: list[Path], monkeypatch: pytest.MonkeyPatch):
    specs, paths = _two_tracks(files, _music)
    chain = setchain.plan_chain(_plans(specs), specs[1].duration_s)
    sources = [setrender.Source(s.id, s.id, p) for s, p in zip(specs, paths)]
    whole = _bus(chain, sources)
    monkeypatch.setattr(setrender, "BLOCK_BEATS", 16)
    assert 20 * np.log10(np.abs(_bus(chain, sources) - whole).max() + 1e-12) < -90


def test_a_block_seam_on_stretched_audio_keeps_its_level(chain: list[MixPlan], sources: list[setrender.Source], bus: np.ndarray, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(setrender, "BLOCK_BEATS", 16)
    cut = _bus(chain, sources)
    hop = SR // 5  # 200 ms: the stretcher's phase moves shorter stretches around, at a seam or not

    def level(x: np.ndarray) -> np.ndarray:
        n = x.shape[1] // hop
        return 10 * np.log10((x[0, : n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-9)

    moved = np.abs(level(cut) - level(bus))
    assert moved.max() < 1.5 and np.percentile(moved, 99) < 0.5


def _pads(s: Spec, seed: int) -> np.ndarray:
    return (0.5 * pad_track(s, SR, 220.0 + 50 * seed)).astype(np.float32)


def test_a_plan_sounds_like_the_seedbox1_mixer_it_shares(files: list[Path]):
    """One plan at one tempo, nothing stretched: against `mixrender` with the same gains and no master. Pads, because the two
    round a deck's start differently, by under a sample. No loops: their seams are `stretch.py`'s here, 20 ms to its 5."""
    specs, paths = _two_tracks(files, _pads)
    plan = _plans(specs)[0]
    plan = plan.model_copy(update={"a": plan.a.model_copy(update={"loops": []}), "b": plan.b.model_copy(update={"loops": []})})
    old = mixrender.render_audio(mixrender_plan(plan), paths[0], paths[1], SR, trims_db={"A": -1.0, "B": -1.0}, master=lambda x, sr: (x, {}))
    new = _bus([plan], [setrender.Source("a", "a", paths[0], -1.0), setrender.Source("b", "b", paths[1], -1.0)])
    assert new.shape == old["mix"].shape
    assert 20 * np.log10(np.abs(new - old["mix"]).max() + 1e-12) < -28


def mixrender_plan(plan: MixPlan) -> dict:
    from seedbox2.mix.offline import to_seedbox1

    return to_seedbox1(plan)


def test_stems_play_only_where_a_lane_moves(tmp_path: Path, files: list[Path]):
    plan = _plans()[0]
    lanes = {n: [{"beat": plan.window.start_beat, "value": 1.0, "curve": "linear"}] for n in ("gain_drums", "gain_bass", "gain_vocals", "gain_other")}
    lanes["gain_bass"] = [{"beat": 10.0, "value": 1.0, "curve": "linear"}, {"beat": 20.0, "value": 0.0, "curve": "linear"}, {"beat": 30.0, "value": 1.0, "curve": "linear"}]
    deck = plan.a.model_dump() | {"stem_lanes": lanes}
    plan = MixPlan.model_validate(plan.model_dump() | {"a": deck})
    source = setrender.Source("t0", "a", files[0], 0.0, {n: files[0] for n in ("drums", "bass", "vocals", "other")})
    clock = setrender.make_clock(plan, plan.window.start_beat, SR)
    blocks = setrender._Blocks(plan.a, source, clock, True, 0, 10, SR)
    assert blocks._stems_at(np.array([12.0, 18.0]))
    assert blocks._stems_at(np.array([28.0, 35.0]))
    assert not blocks._stems_at(np.array([40.0, 60.0]))


# --- resuming ---------------------------------------------------------------------------------------------------------


class Stop(Exception):
    pass


def _read_spool(spool: setexport.Spool, files: int) -> np.ndarray:
    return np.concatenate(list(spool.chunks(files)), axis=1)


def test_a_crash_resumes_from_the_last_track_bit_for_bit(tmp_path: Path, chain: list[MixPlan], sources: list[setrender.Source], bus: np.ndarray):
    spool = setexport.Spool(tmp_path / "spool")
    done = []

    def crash(f: float, _segment: int) -> None:
        if f > 0.8:
            raise Stop

    with pytest.raises(Stop):
        setexport.render_bus(chain, sources, spool, sr=SR, progress=crash)
    assert spool.state_file.exists()
    render, files = spool.load()
    assert 0 < render.at < render.total
    total, files = setexport.render_bus(chain, sources, spool, sr=SR, progress=lambda f, _: done.append(f))
    assert done[0] > 0.3, "it started over"
    out = _read_spool(spool, files)
    assert total == out.shape[1]
    assert np.array_equal(out, bus)


def test_a_torn_checkpoint_starts_over(tmp_path: Path, chain: list[MixPlan], sources: list[setrender.Source], bus: np.ndarray):
    spool = setexport.Spool(tmp_path / "spool")
    spool.folder.mkdir()
    spool.state_file.write_bytes(b"not a pickle")
    (spool.folder / "00000.f32").write_bytes(b"\0" * 80)
    _total, files = setexport.render_bus(chain, sources, spool, sr=SR)
    assert np.array_equal(_read_spool(spool, files), bus)


def test_the_spool_belongs_to_its_plans(chain: list[MixPlan], sources: list[setrender.Source]):
    key = setexport.spool_key(chain, sources)
    assert setexport.spool_key(chain, sources) == key
    assert setexport.spool_key(chain[:1], sources) != key
    assert setexport.spool_key(chain, [sources[0], sources[2], sources[1]]) != key


# --- the master and the files -----------------------------------------------------------------------------------------


def test_the_meter_agrees_with_a_measurement_of_the_whole(bus: np.ndarray):
    meter = master.Meter(SR)
    for a in range(0, bus.shape[1], 7001):
        meter.feed(bus[:, a : a + 7001])
    found, whole = meter.result(), master.measure(bus, SR)
    assert found["integrated_lufs"] == pytest.approx(whole["integrated_lufs"], abs=0.02)
    assert found["lra_lu"] == pytest.approx(whole["lra_lu"], abs=0.05)
    assert found["true_peak_dbtp"] == pytest.approx(whole["true_peak_dbtp"], abs=0.02)


@pytest.fixture(scope="module")
def spooled(tmp_path_factory: pytest.TempPathFactory, bus: np.ndarray) -> tuple[setexport.Spool, int]:
    spool = setexport.Spool(tmp_path_factory.mktemp("spool"))
    bus.T.astype("<f4").tofile(spool.file(0))
    return spool, 1


def test_the_makeup_reaches_the_target_as_the_whole_mix_would(spooled: tuple[setexport.Spool, int], bus: np.ndarray):
    spool, files = spooled
    assert setexport.find_makeup(spool, files, -14.0, SR) == pytest.approx(master.find_makeup(bus, -14.0, SR), abs=0.01)


CHAPTERS = [setexport.Chapter(0.0, 90.0, "One = first"), setexport.Chapter(90.0, 180.0, "Two"), setexport.Chapter(180.0, 280.0, "Three")]


@pytest.mark.parametrize("name", list(setexport.FORMATS))
def test_each_format(name: str, tmp_path: Path, spooled: tuple[setexport.Spool, int], bus: np.ndarray):
    spool, files = spooled
    fmt = setexport.FORMATS[name]
    out = tmp_path / f"mix.{fmt.extension}"
    done = setexport.write_mix(spool, files, fmt, out, -14.0, 3.0, sr=SR, title="My set", chapters=CHAPTERS)
    assert out.exists() and not out.with_name(out.name + ".part").exists()
    assert done["measured"]["true_peak_dbtp"] <= master.CEILING_DBTP + 0.05
    assert done["master"]["makeup_db"] == 3.0
    length = mutagen.File(out).info.length if name != "wav" else stream.decode_window(out, 0, 1, SR).shape[1] and bus.shape[1] / SR
    assert length == pytest.approx(bus.shape[1] / SR, abs=0.2)
    decoded = stream.decode_window(out, 20.0, 1.0, SR)
    assert np.abs(decoded).max() > 0.01
    if name == "mp3":
        tags = ID3(out)
        frames = sorted(tags.getall("CHAP"), key=lambda c: c.start_time)
        assert [c.start_time for c in frames] == [0, 90000, 180000]
        assert frames[0].sub_frames["TIT2"].text[0] == "One = first"
        assert mutagen.File(out).info.bitrate >= 300000
    if name == "aac":
        chapters = MP4(out).chapters
        assert [round(c.start) for c in chapters] == [0, 90, 180]
        assert chapters[1].title == "Two"
        assert 200000 < MP4(out).info.bitrate < 300000
    if name == "flac":
        assert mutagen.File(out).info.bits_per_sample == 24


def test_a_mix_that_fails_leaves_no_file(tmp_path: Path, spooled: tuple[setexport.Spool, int]):
    spool, files = spooled
    out = tmp_path / "mix.mp3"

    def stop() -> None:
        raise Stop

    with pytest.raises(Stop):
        setexport.write_mix(spool, files, setexport.FORMATS["mp3"], out, -14.0, 0.0, sr=SR, progress=stop)
    assert list(tmp_path.iterdir()) == []


def test_the_cue_sheet():
    measured = {"integrated_lufs": -14.04, "lra_lu": 3.21, "true_peak_dbtp": -1.6}
    text = setexport.cue_sheet([(0.0, 'A "quoted" one', "Someone"), (3725.5, "Two", None)], "My set", "mix.mp3", "MP3", measured, -14.0)
    assert 'FILE "mix.mp3" MP3' in text
    assert "TRACK 01 AUDIO" in text and "TITLE \"A 'quoted' one\"" in text and 'PERFORMER "Someone"' in text
    assert "INDEX 01 00:00:00" in text and "INDEX 01 62:05:37" in text
    assert "REM INTEGRATED_LUFS -14.0" in text and "REM LRA_LU 3.2" in text and "REM TRUE_PEAK_DBTP -1.6" in text


def test_an_export_never_replaces_another(tmp_path: Path):
    assert setexport.free_name(tmp_path, "mix", "mp3").name == "mix.mp3"
    (tmp_path / "mix.mp3").touch()
    assert setexport.free_name(tmp_path, "mix", "mp3").name == "mix (2).mp3"


def test_not_enough_disk_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(setexport.shutil, "disk_usage", lambda _: type("U", (), {"free": 10**9})())
    with pytest.raises(ValueError, match="disk space"):
        setexport.check_disk(tmp_path, 7200.0)


# --- the job ----------------------------------------------------------------------------------------------------------


class Ctx:
    def __init__(self, library: Library, cancel_at: float | None = None) -> None:
        self.library, self.cancel_at = library, cancel_at
        self.reports: list[tuple[float, str | None]] = []
        self.settings = None

    def release_sessions(self) -> None:
        pass

    def progress(self, fraction: float, message: str | None = None) -> None:
        self.reports.append((fraction, message))
        if self.cancel_at is not None and fraction >= self.cancel_at:
            raise Cancelled


@pytest.fixture
def library(tmp_path: Path, files: list[Path], monkeypatch: pytest.MonkeyPatch) -> Library:
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(tmp_path / "music" / "MusicReco"))
    lib = Library.open(tmp_path / "lib.sqlite3")
    for s, f in zip(SPECS, files):
        lib.upsert_track(s.id, s.duration_s, title=f"Title {s.id}", artist="Artist")
        with lib.db.write() as c:
            c.execute("INSERT INTO track_paths (path, hash, size, mtime_ns) VALUES (?, ?, 1, 1)", (str(f), s.id))
    for s, t, plan in zip(SPECS, SPECS[1:], _plans()):
        lib.put_plan(s.id, t.id, plan.model_dump(), user_edited=False)
    return lib


def _payload(lib: Library, fmt: str = "mp3") -> dict:
    set_id = lib.save_set("Friday: late", [s.id for s in SPECS])
    return {"set_id": set_id, "hashes": [s.id for s in SPECS], "format": fmt, "target": "streaming", "name": "Friday: late"}


def test_the_job_writes_the_mix_and_its_sheets(library: Library):
    ctx = Ctx(library)
    result = set_export.set_export_job(_payload(library), ctx)
    out = Path(result["path"])
    assert out.name == "Friday- late.mp3" and out.parent == set_export.mix_folder()
    assert "MusicReco" not in str(out), "the music folder is watched: the mix would come back as a track"
    assert [t["name"] for t in result["tracks"]] == [f"Artist - Title {s.id}" for s in SPECS]
    assert abs(result["measured"]["integrated_lufs"] + 14.0) < 0.3
    text = Path(result["tracklist"]).read_text()
    assert "Artist - Title t1" in text and "Integrated:" in text and "True peak:" in text
    cue = Path(result["cue"]).read_text()
    assert 'FILE "Friday- late.mp3" MP3' in cue and cue.count("TRACK ") == 3
    fractions = [f for f, _ in ctx.reports]
    assert fractions == sorted(fractions) and fractions[-1] == pytest.approx(1.0, abs=0.01)
    messages = [m for _, m in ctx.reports]
    assert any(m.startswith("Rendering track 2 of 3") for m in messages) and any("Encoding" in m for m in messages)
    assert not list(set_export.spool_root().rglob("*.f32")), "the spool goes when it's done"


def test_the_jobs_only_route_makes_no_audio_to_listen_to():
    """The app plays a set on the live engine: the export's route is the only way to a render, and it only queues a job."""
    source = (Path(__file__).parents[1] / "seedbox2/server/routers/sets.py").read_text("utf-8")
    assert "setrender" not in source and "write_mix" not in source and "render_bus" not in source


def test_a_cancelled_job_leaves_nothing_behind(library: Library):
    with pytest.raises(Cancelled):
        set_export.set_export_job(_payload(library), Ctx(library, cancel_at=0.4))
    assert not list(set_export.spool_root().rglob("*.f32"))


def test_the_job_says_what_time_is_left(monkeypatch: pytest.MonkeyPatch):
    clock = [0.0]
    monkeypatch.setattr(set_export.time, "monotonic", lambda: clock[0])
    ctx = Ctx(None)  # type: ignore[arg-type]
    bar = set_export._Bar(ctx)
    bar(0.1, "Rendering")
    clock[0] = 100.0
    bar(0.3, "Rendering")
    assert ctx.reports[0][1] == "Rendering"
    assert ctx.reports[1][1] == "Rendering · about 6 min left"  # 0.2 in 100 s, 0.7 to go


def test_a_set_with_a_missing_plan_is_refused(library: Library):
    with pytest.raises(set_export.NotReady) as e:
        set_export.check(library, [SPECS[0].id, SPECS[2].id])
    assert e.value.pairs == [{"from": "t0", "to": "t2"}]


# --- the API ----------------------------------------------------------------------------------------------------------


def test_the_export_route(tmp_path: Path, files: list[Path], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SEEDBOX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUSICRECO_OUTPUT_ROOT", str(tmp_path / "music" / "MusicReco"))
    app = create_app(library_path=tmp_path / "lib.sqlite3", start_jobs=False)
    with TestClient(app) as client:
        lib = app.state.library if hasattr(app.state, "library") else None
        lib = lib or Library.open(tmp_path / "lib.sqlite3")
        for s in SPECS:
            lib.upsert_track(s.id, s.duration_s, title=s.id)
        set_id = client.post("/api/sets", json={"name": "S", "hashes": [s.id for s in SPECS]}).json()["id"]
        status = client.get(f"/api/sets/{set_id}/export").json()
        assert [f["id"] for f in status["formats"]] == ["wav", "flac", "mp3", "aac"] and status["job"] is None
        refused = client.post(f"/api/sets/{set_id}/export", json={"format": "mp3"})
        assert refused.status_code == 409 and len(refused.json()["detail"]["pairs"]) == 2
        for s, t, plan in zip(SPECS, SPECS[1:], _plans()):
            lib.put_plan(s.id, t.id, plan.model_dump(), user_edited=False)
        job = client.post(f"/api/sets/{set_id}/export", json={"format": "mp3"})
        assert job.status_code == 202 and job.json()["kind"] == "set_export"
        assert client.get(f"/api/sets/{set_id}/export").json()["job"]["id"] == job.json()["id"]
        assert client.post(f"/api/sets/{set_id}/export", json={"format": "ogg"}).status_code == 422
        assert client.post("/api/sets/999/export", json={"format": "mp3"}).status_code == 404
