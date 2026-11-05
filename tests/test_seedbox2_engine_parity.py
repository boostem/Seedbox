"""The live engine (#65) against the offline renderer: the same plan, on the same audio, should null.

Every case renders a pair of synthetic tracks with `musicreco.mixrender` and with the live engine's core
(`frontend2/src/audio/engine/`, run by `frontend2/scripts/engine-render.ts` under deno), and subtracts them. The core is
the code the AudioWorklet runs. The master's leveller and limiter have to look ahead offline and can't live (#58), so the
fixtures stay under both; a test checks that they did.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from musicreco import mixrender
from musicreco.paths import deno_path
from seedbox2.mix import convert, plan as v3

ROOT = Path(__file__).resolve().parents[1]
SR = mixrender.SR
BPM = 124.0
SPB = 60.0 / BPM
LIMIT_DB = -40.0
STEMS = ("drums", "bass", "other", "vocals")

deno = deno_path()
pytestmark = pytest.mark.skipif(deno is None, reason="needs deno to run the engine's core")


def _kf(beat: float, value: float, curve: str = "linear") -> dict:
    return {"beat": beat, "value": value, "curve": curve}


def _track_stems(seed: int, seconds: float = 34.0, bpm: float = BPM, gap_s: float = 0.5) -> dict[str, np.ndarray]:
    """A track as four stems, (2, n) float32: kick and hats, a bassline, chords, a wandering voice, with noise in the
    highs so every band of the isolator has something to null."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    spb = 60.0 / bpm
    phase = ((t - gap_s) % spb) / spb
    beat = np.floor((t - gap_s) / spb)
    kick = np.sin(2 * np.pi * (50 + 60 * np.exp(-phase * 14)) * phase * spb) * np.exp(-phase * 7)
    off = ((t - gap_s + spb / 2) % spb) / spb
    hats = rng.normal(size=n) * np.exp(-off * 18) * 0.4
    drums = np.stack([kick * 0.7 + hats * 0.5, kick * 0.7 + hats * 0.4])
    root = 55.0 * 2 ** (np.array([0, 3, 5, 7])[(beat // 4 % 4).astype(int)] / 12)
    bass = np.sin(2 * np.pi * np.cumsum(root) / SR) * (0.45 + 0.1 * np.sin(phase * 6.28))
    chord = sum(np.sin(2 * np.pi * np.cumsum(root * 4 * r) / SR + i) for i, r in enumerate((1.0, 1.26, 1.5)))
    other = np.stack([chord, np.roll(chord, 40)]) * 0.09 + rng.normal(size=(2, n)) * 0.012
    voice = np.sin(2 * np.pi * np.cumsum(300 + 180 * np.sin(t * (1.1 + seed))) / SR)
    voice = voice * (0.5 + 0.5 * np.sin(t * 2.3)) * 0.14
    return {"drums": drums.astype(np.float32), "bass": np.stack([bass, bass]).astype(np.float32),
            "other": other.astype(np.float32), "vocals": np.stack([voice, voice * 0.8]).astype(np.float32)}  # fmt: skip


def _mix(stems: dict[str, np.ndarray]) -> np.ndarray:
    return sum(stems.values())  # type: ignore[return-value]


def _deck(track_id: str, frm: float, to: float, *, beat0_s: float = 0.5, bpm: float = BPM, **over: object) -> dict:
    lanes = {name: [_kf(0.0, v3.LANE_DEFAULTS[name], "hold")] for name in v3.LANES}
    lanes["volume"] = [_kf(0.0, 0.38, "hold")]  # low enough to stay under the leveller and limiter
    lanes.update(over.pop("lanes", {}))  # type: ignore[arg-type]
    return {"track_id": track_id, "beat0_s": beat0_s, "native_bpm": bpm, "play_from_beat": frm, "play_to_beat": to,
            "lanes": lanes, "loops": [], "fx": [], "echo_beats": 0.75, "echo_tap": "pre_eq", "reverb_size": 0.75,
            "reverb_tap": "pre_eq", **over}  # fmt: skip


def _plan(a: dict, b: dict, *, w0: float = -16.0, w1: float = 32.0, bpm: float = BPM, key_lock: bool = True) -> dict:
    return {"version": 2, "from_id": "track-a", "to_id": "track-b", "technique": "long blend", "bars": 8,
            "key_lock": key_lock, "window": {"start_beat": w0, "end_beat": w1}, "tempo": [_kf(0.0, bpm, "hold")],
            "a": a, "b": b, "markers": [], "notes": [], "edited": False}  # fmt: skip


@pytest.fixture(scope="module")
def tracks(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("engine-parity")
    out: dict = {"root": root}
    for name, seed in (("a", 1), ("b", 2)):
        stems = _track_stems(seed)
        mix = _mix(stems)
        out[name] = stems
        path = root / f"{name}.wav"
        wavfile.write(path, SR, mix.T.astype(np.float32))
        out[f"{name}_path"] = path
    return out


def _write_f32(path: Path, audio: np.ndarray) -> None:
    np.asarray(audio, np.float32).reshape(-1).astype("<f4").tofile(path)


def _engine(tracks: dict, plan3: dict, trims: dict[str, float], name: str, *, stems: dict[str, dict] | None = None,
            block: int = 128, swap: dict[str, np.ndarray] | None = None, fold: bool = False) -> tuple[np.ndarray, dict]:  # fmt: skip
    """The plan through the live engine's core: `stems` per deck plays those, else the full mixes (or `swap`'s)."""
    work = tracks["root"] / name
    work.mkdir(exist_ok=True)
    streams: dict[str, str] = {}
    audio: dict[str, dict] = {}
    for deck in "ab":
        if stems and deck in stems:
            sources = []
            for stem, data in stems[deck].items():
                key = f"{deck}.{stem}"
                _write_f32(work / f"{key}.f32", data)
                streams[key] = f"{key}.f32"
                sources.append({"key": key, "stem": f"gain_{stem}"})
        else:
            key = f"{deck}.mix"
            data = swap[deck] if swap and deck in swap else _mix(tracks[deck])
            _write_f32(work / f"{key}.f32", data)
            streams[key] = f"{key}.f32"
            sources = [{"key": key, "stem": None}]
        audio[deck] = {"sources": sources, "trimDb": trims[deck.upper()]}
    job = {"plan": plan3, "audio": audio, "streams": streams, "out": "out.f32", "block": block}
    if fold:
        job["fold"] = "fold.json"
    (work / "job.json").write_text(json.dumps(job))
    r = subprocess.run(
        [deno, "run", "--no-config", "--no-lock", "--sloppy-imports", "--allow-read", "--allow-write", "scripts/engine-render.ts", str(work / "job.json")],
        cwd=ROOT / "frontend2", capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr[-3000:]
    info = json.loads(r.stdout.strip().splitlines()[-1])
    raw = np.fromfile(work / "out.f32", "<f4")
    return raw.reshape(2, -1), info


def _residual_db(ref: np.ndarray, got: np.ndarray) -> float:
    """How far `got` is from `ref`, in dB relative to `ref`."""
    assert ref.shape == got.shape
    power = float(np.mean(ref.astype(np.float64) ** 2))
    return 10 * np.log10(float(np.mean((ref.astype(np.float64) - got) ** 2)) / power + 1e-20)


def _offline(tracks: dict, plan2: dict, a_path: Path | None = None, b_path: Path | None = None) -> dict:
    r = mixrender.render_audio(plan2, a_path or tracks["a_path"], b_path or tracks["b_path"])
    assert r["leveller_db"] == 0.0 and r["limiter_db"] == 0.0, "a fixture must stay under the leveller and limiter"
    return r


def _case_blend() -> dict:
    return _plan(
        _deck("a", -32.0, 32.0, beat0_s=8.0, lanes={
            "volume": [_kf(0.0, 0.55), _kf(24.0, 0.0, "ease")],
            "eq_low": [_kf(4.0, 0.0, "hold"), _kf(4.0, -30.0, "hold")],
            "eq_mid": [_kf(0.0, 0.0), _kf(16.0, -12.0, "ease")],
            "eq_high": [_kf(8.0, 0.0), _kf(16.0, -30.0)],
        }),
        _deck("b", 0.0, 64.0, lanes={
            "volume": [_kf(0.0, 0.0), _kf(8.0, 0.55, "ease")],
            "eq_low": [_kf(4.0, -30.0, "hold"), _kf(4.0, 0.0, "hold")],
            "eq_high": [_kf(0.0, 3.0), _kf(16.0, 0.0)],
        }),
    )  # fmt: skip


def _case_filter() -> dict:
    return _plan(
        _deck("a", -32.0, 32.0, beat0_s=8.0, lanes={
            "volume": [_kf(0.0, 0.3, "hold")],  # resonance peaks: stays under the limiter
            "filter": [_kf(-8.0, 0.0), _kf(8.0, -0.9, "ease"), _kf(16.0, 0.8), _kf(24.0, 0.0)],
            "filter_res": [_kf(-8.0, 0.0), _kf(8.0, 0.7)],
        }),
        _deck("b", -4.0, 64.0, lanes={"volume": [_kf(0.0, 0.3, "hold")], "filter": [_kf(0.0, 0.7), _kf(16.0, 0.0, "ease")]}),
    )  # fmt: skip


def _case_loops_fx() -> dict:
    return _plan(
        _deck("a", -32.0, 28.0, beat0_s=8.0, loops=[
            {"start_beat": -8.0, "end_beat": 0.0, "source_beat": -12.0, "length_beats": 2.0},
            {"start_beat": 0.0, "end_beat": 4.0, "source_beat": 0.0, "length_beats": 0.25},
        ], fx=[
            {"type": "stutter", "start_beat": 8.0, "end_beat": 12.0, "div": 0.25},
            {"type": "brake", "start_beat": 20.0, "end_beat": 24.0},
        ]),
        _deck("b", 0.0, 64.0, loops=[{"start_beat": 2.0, "end_beat": 10.0, "source_beat": 2.0, "length_beats": 4.0}],
              fx=[{"type": "spinback", "start_beat": 24.0, "end_beat": 28.0}]),
    )  # fmt: skip


def _case_sends() -> dict:
    return _plan(
        _deck("a", -32.0, 8.0, beat0_s=8.0, echo_beats=0.75, reverb_size=0.4, lanes={
            "echo_send": [_kf(-8.0, 0.0), _kf(0.0, 0.5, "ease"), _kf(8.0, 0.5, "hold")],
            "echo_feedback": [_kf(0.0, 0.3), _kf(6.0, 0.7)],
            "reverb_send": [_kf(-4.0, 0.0, "hold"), _kf(-4.0, 0.3, "hold")],
            "eq_low": [_kf(0.0, -30.0, "hold")],
        }),
        _deck("b", 4.0, 64.0, echo_tap="post_fader", reverb_tap="post_fader", echo_beats=1.5, lanes={
            "echo_send": [_kf(12.0, 0.0, "hold"), _kf(12.0, 0.4, "hold"), _kf(16.0, 0.0, "hold")],
            "reverb_send": [_kf(16.0, 0.0), _kf(20.0, 0.35)],
        }),
    )  # fmt: skip


def _case_grid() -> dict:
    """A deck whose grid changes tempo: its file time runs on each segment's own rate, at the master tempo all the same."""
    a = _deck("a", -32.0, 32.0, beat0_s=8.0)
    a["grid"] = [{"start_beat": -64.0, "start_s": 8.0 - 64 * SPB, "bpm": BPM}, {"start_beat": 0.0, "start_s": 8.0, "bpm": BPM}]
    return _plan(a, _deck("b", 0.0, 64.0))


CASES = {"blend": _case_blend, "filter": _case_filter, "loops_fx": _case_loops_fx, "sends": _case_sends, "grid": _case_grid}


@pytest.mark.parametrize("name", CASES)
def test_a_plan_without_stretch_nulls_against_the_renderer(tracks, name):
    plan2 = CASES[name]()
    ref = _offline(tracks, plan2)
    plan3 = convert.from_seedbox1(plan2).model_dump()
    got, info = _engine(tracks, plan3, ref["trim_db"], name)
    assert info["missing"] == 0
    residual = _residual_db(ref["mix"], got)
    print(f"{name}: {residual:.1f} dB")
    assert residual < LIMIT_DB


def test_the_block_size_changes_nothing(tracks):
    plan2 = _case_filter()
    ref = _offline(tracks, plan2)
    plan3 = convert.from_seedbox1(plan2).model_dump()
    a, _ = _engine(tracks, plan3, ref["trim_db"], "block-128")
    b, _ = _engine(tracks, plan3, ref["trim_db"], "block-37", block=37)
    assert np.max(np.abs(a - b)) < 1e-5


def test_varispeed_nulls_against_a_render_without_key_lock(tracks):
    plan2 = _plan(_deck("a", -32.0, 32.0, beat0_s=8.0), _deck("b", 0.0, 64.0), bpm=BPM * 1.03, key_lock=False)
    ref = _offline(tracks, plan2)
    plan3 = convert.from_seedbox1(plan2).model_dump()
    got, _ = _engine(tracks, plan3, ref["trim_db"], "varispeed")
    residual = _residual_db(ref["mix"], got)
    print(f"varispeed: {residual:.1f} dB")
    assert residual < -30.0


def test_stems_at_unity_null_against_the_full_track(tracks):
    plan2 = _case_blend()
    ref = _offline(tracks, plan2)
    plan3 = convert.from_seedbox1(plan2).model_dump()
    plan3["a"]["stem_lanes"] = {name: [_kf(0.0, 1.0, "hold")] for name in v3.STEM_LANES}
    plan3["b"]["stem_lanes"] = {name: [_kf(0.0, 1.0, "hold")] for name in v3.STEM_LANES}
    got, _ = _engine(tracks, plan3, ref["trim_db"], "stems-unity", stems={"a": tracks["a"], "b": tracks["b"]})
    assert _residual_db(ref["mix"], got) < -60.0


def test_a_silent_stem_is_true_silence(tracks):
    """vocals at 0 on deck B: exactly a render of B with no vocals in its file."""
    root = tracks["root"]
    no_vocals = sum(v for k, v in tracks["b"].items() if k != "vocals")
    path = root / "b-no-vocals.wav"
    wavfile.write(path, SR, no_vocals.T.astype(np.float32))
    plan2 = _case_blend()
    ref = _offline(tracks, plan2, b_path=path)
    plan3 = convert.from_seedbox1(plan2).model_dump()
    plan3["b"]["stem_lanes"] = {name: [_kf(0.0, 0.0 if name == "gain_vocals" else 1.0, "hold")] for name in v3.STEM_LANES}
    got, _ = _engine(tracks, plan3, ref["trim_db"], "stems-silent", stems={"b": tracks["b"]})
    assert _residual_db(ref["mix"], got) < -60.0


def test_stem_lanes_fold_into_eq_as_the_server_folds_them(tracks):
    """The graceful fallback: a deck without stems gets the same EQ lanes in the page as `validate(has_stems=...)` gives."""
    plan2 = _case_blend()
    ref = _offline(tracks, plan2)
    plan3 = convert.from_seedbox1(plan2).model_dump()
    plan3["a"]["stem_lanes"] = {
        "gain_drums": [_kf(0.0, 1.0), _kf(8.0, 0.25, "ease")],
        "gain_bass": [_kf(0.0, 1.0, "hold"), _kf(4.0, 1.0, "hold"), _kf(4.0, 0.0, "hold")],
        "gain_vocals": [_kf(-4.0, 1.0), _kf(12.0, 0.0)],
        "gain_other": [_kf(0.0, 1.0, "hold")],
    }
    plan3["b"]["stem_lanes"] = {name: [_kf(0.0, 1.0, "hold")] for name in v3.STEM_LANES}
    # a stored plan keeps stem lanes: the engine folds them when the track has no stems
    server = v3.validate(plan3, has_stems=lambda _: False).model_dump()
    _engine(tracks, plan3, ref["trim_db"], "fold", fold=True)
    page = json.loads((tracks["root"] / "fold" / "fold.json").read_text())
    for deck in "ab":
        assert page[deck]["stem_lanes"] is None
        for lane, keys in server[deck]["lanes"].items():
            assert len(page[deck]["lanes"][lane]) == len(keys), lane
            for x, y in zip(page[deck]["lanes"][lane], keys):
                assert x["beat"] == pytest.approx(y["beat"]) and x["value"] == pytest.approx(y["value"]) and x["curve"] == y["curve"]
    assert page["notes"] == server["notes"]
