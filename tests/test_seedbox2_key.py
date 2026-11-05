"""Seedbox 2 key detection (#45): tuning, chroma, matching, key changes, confidence, the job, overrides and hints."""

from __future__ import annotations

import numpy as np
import pytest

from seedbox2.analysis import key as K
from seedbox2.jobs import REGISTRY
from seedbox2.library.repo import Library
from seedbox2.library.view import describe, key_hints

SR = K.SAMPLE_RATE


def tones(midi: list[int], seconds: float, *, cents: float = 0.0, sr: int = SR, partials: int = 4) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    x = np.zeros(t.size)
    for m in midi:
        f = 440.0 * 2 ** ((m - 69 + cents / 100) / 12)
        for p in range(1, partials + 1):
            if f * p < sr / 2:
                x += np.sin(2 * np.pi * f * p * t) / p
    return x / np.max(np.abs(x))


def progression(root: int, minor: bool, seconds: float, *, cents: float = 0.0) -> np.ndarray:
    """I-IV-V-I in a key, 2 s per chord, with a bass note on the root: what the templates are about."""
    third = 3 if minor else 4
    bars = [(0, third, 7), (5, 5 + third, 12), (7, 11, 14), (0, third, 7)]
    out = []
    for i in range(int(seconds / 2)):
        chord = bars[i % 4]
        out.append(tones([root + 12 + n for n in chord] + [root + chord[0]], 2.0, cents=cents))
    return np.concatenate(out)


@pytest.fixture
def lib(tmp_path):
    library = Library.open(tmp_path / "library.sqlite3")
    yield library
    library.close()


# --- notation ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "index", "camelot", "open_key"), [
    ("A minor", 12 + 9, "8A", "1m"),
    ("C major", 0, "8B", "1d"),
    ("Db minor", 12 + 1, "12A", "5m"),
    ("Gb major", 6, "2B", "7d"),
    ("E major", 4, "12B", "5d"),
])  # fmt: skip
def test_notations(name, index, camelot, open_key):
    assert K.notations(index) == {"key": name, "camelot": camelot, "open_key": open_key}
    assert K.index_of_camelot(camelot) == index


def test_notations_cover_every_key_once():
    all_keys = [K.notations(i) for i in range(24)]
    assert len({k["camelot"] for k in all_keys}) == len({k["open_key"] for k in all_keys}) == 24
    assert K.open_key_of("13A") is None and K.index_of_camelot("nope") is None


# --- tuning and chroma ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cents", [-40.0, -25.0, 30.0, 45.0])
def test_tuning_finds_a_detuned_track(cents):
    assert K.estimate_tuning(progression(57, True, 32, cents=cents)) == pytest.approx(cents, abs=3)


def test_tuning_leaves_a440_alone():
    assert K.estimate_tuning(progression(57, True, 32)) == 0.0
    assert K.estimate_tuning(np.zeros(SR // 2)) == 0.0
    assert K.estimate_tuning(np.zeros(SR * 10)) == 0.0


def test_cq_frames_put_each_note_on_its_semitone():
    x = tones([57], 8.0, partials=1)  # A3
    frames = K.cq_frames(x, np.arange(0.0, 8.5, 0.5))
    assert frames.shape == (16, K.OCTAVES * 12)
    assert int(frames.mean(axis=0).argmax()) == 57 - K.LOW_MIDI


def test_cq_frames_follow_the_tuning():
    x = tones([57], 8.0, cents=45.0, partials=1)
    edges = np.arange(0.0, 8.5, 0.5)
    tuned = K.cq_frames(x, edges, tuning_cents=45.0)[:, 57 - K.LOW_MIDI].mean()
    untuned = K.cq_frames(x, edges)[:, 57 - K.LOW_MIDI].mean()
    assert tuned > 1.5 * untuned


def test_cq_frames_follow_time():
    x = np.concatenate([tones([57], 4.0, partials=1), tones([60], 4.0, partials=1)])
    frames = K.cq_frames(x, np.array([0.0, 4.0, 8.0]))
    assert frames[0].argmax() == 57 - K.LOW_MIDI
    assert frames[1].argmax() == 60 - K.LOW_MIDI


def test_cq_frames_of_nothing():
    assert K.cq_frames(np.zeros(0), np.array([0.0, 1.0])).sum() == 0
    assert K.cq_frames(np.ones(100), np.array([0.0])).shape == (0, K.OCTAVES * 12)


def test_chroma_is_by_pitch_class_with_a_peak_of_one():
    frames = np.zeros((2, K.OCTAVES * 12))
    frames[0, 57 - K.LOW_MIDI] = 1.0  # A
    frames[1, 60 - K.LOW_MIDI] = 1.0  # C
    c = K.chroma_of(frames)
    assert c[0].argmax() == 9 and c[1].argmax() == 0
    assert c.max(axis=1) == pytest.approx([1.0, 1.0])


# --- matching ------------------------------------------------------------------------------------------------------


def test_each_template_is_its_own_best_match():
    t = K.templates()
    assert (K.correlations(t).argmax(axis=1) == np.arange(24)).all()


def test_the_profiles_are_faraldos():
    assert K.PROFILES["edma"][0][7] == 0.80 and K.PROFILES["edma"][1][3] == 0.58
    assert K.PROFILES["bgate"][0][1] == 0.0 and K.PROFILES["bgate"][1][7] == 0.74


@pytest.mark.parametrize(("root", "minor", "camelot"), [(57, True, "8A"), (60, False, "8B"), (55, True, "6A"),
                                                        (62, False, "10B"), (53, True, "4A")])  # fmt: skip
def test_analyse_finds_the_key_of_a_progression(root, minor, camelot):
    r = K.analyse_signal(progression(root, minor, 64))
    assert r["best_guess"]["camelot"] == camelot
    assert r["camelot"] == camelot and r["confidence"] >= K.MIN_CONFIDENCE
    assert r["changes"] == [] and r["source"] == "mix" and r["beat_synchronous"] is False


def test_analyse_is_not_fooled_by_a_detuned_track():
    r = K.analyse_signal(progression(57, True, 64, cents=45.0))
    assert r["camelot"] == "8A" and r["tuning_cents"] == pytest.approx(45, abs=3)


def test_analyse_uses_the_beats():
    beat_times = np.arange(0.0, 64.0, 0.5)
    r = K.analyse_signal(progression(57, True, 64), beat_times, source="stems")
    assert r["camelot"] == "8A" and r["beat_synchronous"] is True and r["source"] == "stems"
    assert K.analyse_signal(progression(57, True, 64), beat_times[:4])["beat_synchronous"] is False  # too few to use


def test_silence_has_no_key():
    r = K.analyse_signal(np.zeros(SR * 10))
    assert r["key"] is None and r["camelot"] is None and r["best_guess"] is None and r["confidence"] == 0


def test_white_noise_has_low_confidence_and_shows_no_key():
    x = np.random.default_rng(1).standard_normal(SR * 30) * 0.1
    r = K.analyse_signal(x)
    assert r["confidence"] < K.MIN_CONFIDENCE
    assert r["camelot"] is None and r["key"] is None and r["open_key"] is None
    assert r["best_guess"] is not None  # what it would have been


def test_confidence_grows_with_correlation_and_margin():
    def conf(best, second):
        s = np.zeros(24)
        s[0], s[1] = best, second
        return K.confidence_from(s)[1]

    assert conf(0.9, 0.3) > conf(0.6, 0.3) > conf(0.3, 0.2)
    assert conf(0.8, 0.2) > conf(0.8, 0.7)


def test_a_key_change_is_reported_with_its_section():
    beat_s = 0.5
    a, b = progression(57, True, 128), progression(50, True, 64)  # A minor, then D minor for 32 bars
    x = np.concatenate([a, b])
    r = K.analyse_signal(x, np.arange(0.0, x.size / SR, beat_s))
    assert r["best_guess"]["camelot"] == "8A"
    assert len(r["changes"]) == 1
    change = r["changes"][0]
    assert change["camelot"] == "7A"
    assert change["start_s"] == pytest.approx(128, abs=16)
    assert change["end_s"] == pytest.approx(192, abs=1)


def test_a_short_excursion_is_not_a_key_change():
    x = np.concatenate([progression(57, True, 64), progression(50, True, 16), progression(57, True, 64)])
    r = K.analyse_signal(x, np.arange(0.0, x.size / SR, 0.5))
    assert r["camelot"] == "8A" and r["changes"] == []


def test_viterbi_needs_a_reason_to_change():
    s = np.zeros((6, 24))
    s[:, 0] = 0.6
    s[2, 5] = 0.8  # one block's whim
    assert K.viterbi(s, cost=0.6).tolist() == [0] * 6
    s[3:, 5] = 0.9
    s[3:, 0] = 0.1
    assert K.viterbi(s, cost=0.6).tolist() == [0, 0, 5, 5, 5, 5]


# --- the job -------------------------------------------------------------------------------------------------------


class Ctx:
    def __init__(self, library):
        self.library = library

    def progress(self, _f):
        pass


class Jobs:
    def __init__(self):
        self.submitted = []

    def submit(self, kind, payload, **kw):
        self.submitted.append((kind, payload["hash"], kw))
        return {"id": "x", "state": "queued"}

    def stop(self):
        pass


def write_wav(path, x):
    from scipy.io import wavfile

    wavfile.write(path, SR, (x * 30000).astype(np.int16))


def test_key_is_a_light_per_track_job():
    t = {j.name: j for j in REGISTRY.per_track()}["key"]
    assert t.pool == "light"


def test_the_job_stores_the_analysis_of_the_mix(lib, tmp_path):
    wav = tmp_path / "a.wav"
    write_wav(wav, progression(57, True, 32))
    lib.upsert_track("h", 32.0)
    with lib.db.write() as c:
        c.execute("INSERT INTO track_paths (path, hash, size, mtime_ns) VALUES (?, 'h', 0, 0)", (str(wav),))
    assert K.key_job({"hash": "h"}, Ctx(lib)) == {"hash": "h", "key": "8A", "confidence": pytest.approx(
        lib.analysis("h", "key", K.VERSION)["data"]["confidence"]), "source": "mix"}  # fmt: skip


def test_the_job_prefers_the_harmonic_stems(lib, tmp_path, monkeypatch):
    from seedbox2.analysis import stems

    folder = tmp_path / "stems"
    folder.mkdir()
    # the drums and the vocals say another key: they must not count
    write_wav(folder / "bass.wav", progression(45, True, 32) * 0.5)
    write_wav(folder / "other.wav", progression(57, True, 32) * 0.5)
    write_wav(folder / "drums.wav", progression(60, False, 32))
    monkeypatch.setattr(stems, "current", lambda _lib, _h: folder)
    lib.upsert_track("h", 32.0)
    assert K.key_job({"hash": "h"}, Ctx(lib))["source"] == "stems"
    assert lib.analysis("h", "key", K.VERSION)["data"]["camelot"] == "8A"


def test_the_job_says_when_the_file_is_gone(lib):
    lib.upsert_track("h", 32.0)
    with pytest.raises(FileNotFoundError):
        K.key_job({"hash": "h"}, Ctx(lib))
    with pytest.raises(ValueError):
        K.key_job({"hash": "nope"}, Ctx(lib))


@pytest.mark.parametrize(("kind", "data", "again"), [
    ("beats", {"beat_synchronous": False, "source": "mix"}, True),
    ("beats", {"beat_synchronous": True, "source": "mix"}, False),
    ("stems", {"beat_synchronous": True, "source": "mix"}, True),
    ("stems", {"beat_synchronous": True, "source": "stems"}, False),
    ("waveform", {"beat_synchronous": False, "source": "mix"}, False),
])  # fmt: skip
def test_the_key_is_redone_when_better_input_arrives(lib, kind, data, again):
    jobs = Jobs()
    lib.upsert_track("h", 32.0)
    lib.put_analysis("h", "key", K.VERSION, data)
    K.on_job_finished(jobs, lib, {"kind": kind, "state": "done", "payload": {"hash": "h"}})
    assert [j[:2] for j in jobs.submitted] == ([("key", "h")] if again else [])


def test_no_second_key_job_while_the_first_is_pending_or_after_a_failure(lib):
    jobs = Jobs()
    lib.upsert_track("h", 32.0)
    K.on_job_finished(jobs, lib, {"kind": "beats", "state": "done", "payload": {"hash": "h"}})  # no key yet
    lib.put_analysis("h", "key", K.VERSION, {"beat_synchronous": False, "source": "mix"})
    K.on_job_finished(jobs, lib, {"kind": "stems", "state": "failed", "payload": {"hash": "h"}})
    assert jobs.submitted == []


def test_queue_missing_is_for_tracks_without_this_version(lib):
    jobs = Jobs()
    for h in ("a", "b", "c"):
        lib.upsert_track(h, 32.0)
    lib.put_analysis("a", "key", K.VERSION, {})
    lib.put_analysis("b", "key", "0", {})
    assert K.queue_missing(jobs, lib) == 2
    assert sorted(j[1] for j in jobs.submitted) == ["b", "c"]
    assert all(j[2]["priority"] == "background" for j in jobs.submitted)


# --- overrides, the planner and hints ------------------------------------------------------------------------------


GUESS = {"key": "A minor", "camelot": "8A", "open_key": "1m"}
SURE = {**GUESS, "confidence": 0.9, "best_guess": GUESS}
UNSURE = {"key": None, "camelot": None, "open_key": None, "confidence": 0.2, "best_guess": GUESS}


def test_the_planner_sees_a_confident_key_and_nothing_else(lib):
    lib.upsert_track("h", 32.0, tags={"camelot": "3B"})
    assert K.planning_key(lib, "h") is None  # not analysed: a tag isn't a key to plan on
    lib.put_analysis("h", "key", K.VERSION, UNSURE)
    assert K.planning_key(lib, "h") is None
    assert K.effective(lib, "h")["from"] == "unknown"
    lib.put_analysis("h", "key", K.VERSION, SURE)
    assert K.planning_key(lib, "h") == "8A"


def test_the_planner_treats_an_unknown_key_as_no_move():
    from musicreco.mixing import camelot_move

    assert camelot_move(None, "8A") == camelot_move("8A", None) == camelot_move("", "8A") == "?"


def test_an_override_wins_and_survives_new_analysis(lib):
    lib.upsert_track("h", 32.0)
    lib.put_analysis("h", "key", K.VERSION, SURE)
    assert K.set_override(lib, "h", "F#m")["camelot"] == "11A"
    assert K.effective(lib, "h") == {"key": "Gb minor", "camelot": "11A", "open_key": "4m", "from": "user"}
    lib.put_analysis("h", "key", K.VERSION, {**SURE, "camelot": "9A"})
    assert K.planning_key(lib, "h") == "11A"
    K.clear_override(lib, "h")
    assert K.effective(lib, "h")["from"] == "analysis" and K.planning_key(lib, "h") == "9A"


def test_an_override_can_be_any_notation_but_must_be_a_key(lib):
    lib.upsert_track("h", 32.0)
    assert K.set_override(lib, "h", "1m")["camelot"] == "8A"
    assert K.set_override(lib, "h", "C major")["camelot"] == "8B"
    with pytest.raises(ValueError):
        K.set_override(lib, "h", "H minor nonsense")


def track(tags=None) -> dict:
    return {"hash": "h", "duration": 60.0, "title": "", "artist": "", "album": "", "source": "local", "added_at": "",
            "tags": tags or {}}  # fmt: skip


def test_view_tags_fill_in_until_the_analysis():
    row = describe(track({"camelot": "3B"}))
    assert (row["key"], row["key_from"], row["key_hint"]) == ("3B", "tags", None)
    row = describe(track())
    assert (row["key"], row["key_from"], row["key_hint"]) == (None, None, None)


def test_view_ours_with_the_tag_that_disagrees_next_to_it():
    a = {"key": {"camelot": "8A", "key": "A minor"}}
    row = describe(track({"camelot": "3B", "file": {"key": "Am"}}), analyses=a)
    assert (row["key"], row["key_from"]) == ("8A", "analysis")
    assert (row["key_hint"], row["key_hint_from"]) == ("3B", "tunebat")
    row = describe(track({"file": {"key": "Am"}}), analyses=a)
    assert (row["key"], row["key_hint"]) == ("8A", None)  # they agree


def test_view_low_confidence_is_unknown_even_with_a_tag():
    row = describe(track({"camelot": "3B"}), analyses={"key": {"camelot": None, "key": None}})
    assert (row["key"], row["key_from"]) == (None, "unknown")
    assert (row["key_hint"], row["key_hint_from"]) == ("3B", "tunebat")


def test_view_override_wins_and_is_not_listed_as_an_analysis():
    row = describe(track({"camelot": "3B"}), analyses={"key": {"camelot": "8A"}, "key_override": {"camelot": "11A"}})
    assert (row["key"], row["key_from"], row["key_hint"]) == ("11A", "user", "3B")
    assert row["status"]["analyses"] == ["key"]


def test_view_rows_read_the_override_from_the_library(lib):
    lib.upsert_track("h", 32.0, tags={"camelot": "3B"})
    lib.put_analysis("h", "key", K.VERSION, SURE)
    K.set_override(lib, "h", "11A")
    row = lib.view_rows()[0]
    assert (row["key"], row["key_from"], row["key_hint"]) == ("11A", "user", "3B")
    K.clear_override(lib, "h")
    assert lib.view_rows()[0]["key"] == "8A"


def test_key_hints_lists_each_key_once_most_trusted_first():
    tags = {"camelot": "3B", "rekordbox": {"key": "Am"}, "file": {"key": "8A"}, "itunes": {"key": "Dm"}}
    assert key_hints(tags) == [{"key": "8A", "from": "rekordbox"}, {"key": "3B", "from": "tunebat"},
                               {"key": "7A", "from": "itunes"}]  # fmt: skip


# --- the API -------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from seedbox2.server.app import create_app

    library = Library.open(tmp_path / "library.sqlite3")
    app = create_app()
    app.state.library = library
    jobs = Jobs()
    app.state.jobs = jobs
    with TestClient(app) as c:
        c.library, c.jobs = library, jobs  # type: ignore[attr-defined]
        yield c
    library.close()


def test_api_key_is_queued_first_then_served(client):
    assert client.get("/api/analysis/none/key").status_code == 404
    client.library.upsert_track("h", 32.0, tags={"camelot": "3B"})
    r = client.get("/api/analysis/h/key")
    assert r.status_code == 202
    assert client.jobs.submitted[0][:2] == ("key", "h") and client.jobs.submitted[0][2]["front"] is True
    client.library.put_analysis("h", "key", K.VERSION, SURE)
    body = client.get("/api/analysis/h/key").json()
    assert body["effective"] == {**GUESS, "from": "analysis"}
    assert body["hints"] == [{"key": "3B", "from": "tunebat"}]
    assert body["analysis"]["confidence"] == 0.9


def test_api_override_round_trip(client):
    assert client.put("/api/analysis/none/key/override", json={"key": "8A"}).status_code == 404
    client.library.upsert_track("h", 32.0)
    assert client.put("/api/analysis/h/key/override", json={"key": "nonsense"}).status_code == 422
    body = client.put("/api/analysis/h/key/override", json={"key": "Am"}).json()
    assert body["effective"] == {**GUESS, "from": "user"} and body["analysis"] is None
    assert client.delete("/api/analysis/h/key/override").json()["effective"]["from"] == "unknown"
