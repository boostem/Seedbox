"""Synthetic TrackAnalysis dicts (the shape of structure.analyze) and Tracks with a known grid and structure, so the
planner tests don't depend on the analyser.

A track is described in bars: intro / outro lengths, which bars are sung, where the drops are. The per-bar features
are what structure.py reports for such a track: drums-only edges have little low end and energy, the body has bass,
drops are at full power, the 8 bars before a drop are a breakdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from musicreco.models import Track


@dataclass
class Spec:
    """One synthetic track. Bars are 0-based; ranges are [start, end)."""

    id: str
    bpm: float = 124.0
    bars: int = 128
    first_downbeat_s: float = 0.0
    phrase_offset: int = 0
    intro_bars: int = 16
    intro_drums: bool = True
    outro_bars: int = 16
    outro_drums: bool = True
    vocals: list[tuple[int, int]] = field(default_factory=list)
    drops: list[int] = field(default_factory=list)  # drop starts; each drop lasts 16 bars after an 8-bar breakdown
    camelot: str = "8A"
    cold_end: bool = False  # full power up to the last bar (no wind-down)
    insert: int | None = None  # a 4-bar fill section starts here: the phrasing after it is 4 bars off the grid

    @property
    def bar_s(self) -> float:
        return 240.0 / self.bpm

    @property
    def duration_s(self) -> float:
        return round(self.first_downbeat_s + self.bars * self.bar_s + 0.5, 3)


def sections(s: Spec) -> list[tuple[str, int, int]]:
    """(kind, start bar, end bar) covering [0, bars): intro, body/breakdown/drop, outro."""
    o0 = s.bars - s.outro_bars
    cuts = [(d - 8, "breakdown") for d in s.drops] + [(d, "drop") for d in s.drops] + [(d + 16, "body") for d in s.drops]
    if s.insert is not None:
        cuts += [(s.insert, "body"), (s.insert + 4, "body")]
    out = [("intro", 0, s.intro_bars)] if s.intro_bars else []
    pos, kind = s.intro_bars, "body"
    for at, nxt in sorted(cuts):
        at = max(pos, min(at, o0))
        if at > pos:
            out.append((kind, pos, at))
        pos, kind = at, nxt
    if o0 > pos:
        out.append((kind, pos, o0))
    if s.outro_bars:
        out.append(("outro", o0, s.bars))
    return out


def analysis(s: Spec) -> dict:
    """TrackAnalysis dict for `s` (only the fields the planner reads, plus the identifying ones)."""
    secs = sections(s)
    kind_at = [next(k for k, a, b in secs if a <= i < b) for i in range(s.bars)]
    energy, low, vocal = [], [], []
    for i, kind in enumerate(kind_at):
        drums = (kind == "intro" and s.intro_drums) or (kind == "outro" and s.outro_drums)
        e = {"intro": 0.6, "outro": 0.6, "breakdown": 0.4, "drop": 1.0}.get(kind, 0.8)
        lo = {"breakdown": 0.15, "drop": 0.9}.get(kind, 0.7)
        if drums:
            e, lo = 0.55, 0.3
        if s.cold_end and i >= s.bars - 4:
            e = 1.0
        energy.append(e)
        low.append(lo)
        vocal.append(0.85 if any(a <= i < b for a, b in s.vocals) else 0.05)

    def t(bar: int) -> float:
        return round(s.first_downbeat_s + bar * s.bar_s, 3)

    def edge(a: int, b: int, drums: bool) -> dict:
        sung = any(a <= i < b for va, vb in s.vocals for i in range(va, vb))
        return {"bars": b - a, "start_s": t(a), "end_s": t(b), "drums_only": drums and not sung, "has_vocals": sung}

    return {
        "version": 4, "track_id": s.id, "duration_s": s.duration_s, "bpm": s.bpm, "bpm_source": "detected",
        "first_downbeat_s": s.first_downbeat_s, "bar_s": round(s.bar_s, 6), "bar_count": s.bars,
        "phrase_offset": s.phrase_offset, "energy_per_bar": energy, "low_per_bar": low, "vocal_per_bar": vocal,
        "sections": [{"kind": k, "start_bar": a, "end_bar": b, "start_s": t(a), "end_s": t(b)} for k, a, b in secs],
        "intro": edge(0, s.intro_bars, s.intro_drums),
        "outro": edge(s.bars - s.outro_bars, s.bars, s.outro_drums),
        "cues": [],
    }  # fmt: skip


def track(s: Spec) -> Track:
    return Track(
        id=s.id, name=s.id.replace("_", " "), artists=["Synth"], album="", release_date="2024",
        duration_ms=int(s.duration_s * 1000), popularity=0, key="", camelot=s.camelot, bpm=round(s.bpm),
        energy=0.7, danceability=0.7, cover_url=None,
    )  # fmt: skip


# --- the library of pairs the tests use -----------------------------------------------------------------------------

LONG_OUTRO = Spec("long_outro", outro_bars=32, intro_bars=32)
SHORT_OUTRO = Spec("short_outro", outro_bars=8, outro_drums=False)
COLD_END = Spec("cold_end", outro_bars=0, cold_end=True)
VOCAL_OUTRO = Spec("vocal_outro", outro_bars=16, outro_drums=False, vocals=[(32, 64), (112, 128)])
VOCAL_INTRO = Spec("vocal_intro", intro_bars=16, intro_drums=False, vocals=[(0, 16), (48, 80)])
DROP_HEAVY = Spec("drop_heavy", bars=160, outro_bars=8, outro_drums=False, drops=[40, 96, 136], vocals=[(16, 32), (120, 160)])
DROP_HEAVY_B = Spec("drop_heavy_b", bars=160, intro_bars=8, intro_drums=False, drops=[24, 72], vocals=[(0, 24)])
STANDARD = Spec("standard")
PICKUP = Spec("pickup", first_downbeat_s=0.37, phrase_offset=4, bars=132, intro_bars=20)
FAST = Spec("fast", bpm=140.0)  # 12.9 % faster than 124: no blend
HALF_TIME = Spec("half_time", bpm=64.0, bars=72, intro_bars=8, outro_bars=8)
CLASH_KEY = Spec("clash_key", camelot="3B")
# a 4-bar fill at bar 56: the breakdown, drop and 16-bar outro after it start 4 bars off the track's 8-bar grid
INSERT = Spec("insert", bars=132, insert=56, drops=[68])
OUTRO_24 = Spec("outro_24", outro_bars=24)  # a 16 + 8-bar outro: blends start on its 16-bar phrase
SHORT_INTRO_DROP = Spec("short_intro_drop", intro_bars=8, drops=[16])  # 8-bar intro, 8-bar build, drop at bar 16
BUILD_END = Spec("build_end", outro_bars=8, outro_drums=False, drops=[112])  # last build ends 16 bars from the end
# typical club tracks: vocal house with 16-bar drum edges (B's outro 32), peak-time techno with 32-bar drum edges,
# a house track whose outro keeps its bass and pads, and an edit with 8-bar edges on both sides
HOUSE = Spec("house", bars=144, vocals=[(32, 64), (80, 112)])
HOUSE_B = Spec("house_b", bpm=123.0, camelot="9A", outro_bars=32, vocals=[(24, 56)])
TECHNO = Spec("techno", bpm=130.0, bars=192, intro_bars=32, outro_bars=32)
TECHNO_B = Spec("techno_b", bpm=131.0, bars=192, intro_bars=32, outro_bars=32, camelot="8B")
MUSIC_OUTRO = Spec("music_outro", outro_drums=False)
SHORT_EDGES = Spec("short_edges", intro_bars=8, outro_bars=8)

# (name, A, B): every pair has an expected planner outcome in test_planner.py and a line in the snapshot
PAIRS = [
    ("long outro -> standard", LONG_OUTRO, STANDARD),
    ("standard -> long outro", STANDARD, LONG_OUTRO),
    ("short outro -> standard", SHORT_OUTRO, STANDARD),
    ("cold end -> standard", COLD_END, STANDARD),
    ("vocal outro -> standard", VOCAL_OUTRO, STANDARD),
    ("vocal outro -> vocal intro", VOCAL_OUTRO, VOCAL_INTRO),
    ("drop heavy -> drop heavy", DROP_HEAVY, DROP_HEAVY_B),
    ("standard -> pickup", STANDARD, PICKUP),
    ("standard -> fast", STANDARD, FAST),
    ("standard -> half time", STANDARD, HALF_TIME),
    ("standard -> clash key", STANDARD, CLASH_KEY),
    ("short outro -> clash key", SHORT_OUTRO, CLASH_KEY),
    ("insert -> standard", INSERT, STANDARD),
    ("outro 24 -> standard", OUTRO_24, STANDARD),
    ("standard -> short intro drop", STANDARD, SHORT_INTRO_DROP),
    ("build end -> short intro drop", BUILD_END, SHORT_INTRO_DROP),
    ("build end -> fast", BUILD_END, FAST),
    ("build end -> standard", BUILD_END, STANDARD),
    ("house -> house", HOUSE, HOUSE_B),
    ("techno -> techno", TECHNO, TECHNO_B),
    ("music outro -> house", MUSIC_OUTRO, HOUSE_B),
    ("short edges -> short edges", SHORT_EDGES, SHORT_EDGES),
]
# the club pairs whose blends should run 16 bars or more at the default mix length
CLUB_PAIRS = [p for p in PAIRS if p[0] in ("house -> house", "techno -> techno", "music outro -> house")]


# --- click-track audio ----------------------------------------------------------------------------------------------


def click_track(s: Spec, sr: int, pitch: float) -> "np.ndarray":
    """(2, n) float32 audio matching analysis(s): a 3 ms click of `pitch` Hz on every beat (downbeats louder and an
    octave up) over a quiet 55 Hz bass note, silent before the first downbeat."""
    import numpy as np

    n = int(round(s.duration_s * sr))
    x = np.zeros(n, np.float32)
    t = np.arange(n) / sr
    start = int(round(s.first_downbeat_s * sr))
    x[start:] += 0.08 * np.sin(2 * np.pi * 55.0 * t[start:])
    m = int(0.003 * sr)
    env = np.exp(-np.arange(m) / (0.0006 * sr)).astype(np.float32)
    for k in range(s.bars * 4):
        i = int(round((s.first_downbeat_s + k * 60.0 / s.bpm) * sr))
        f, amp = (2 * pitch, 0.8) if k % 4 == 0 else (pitch, 0.5)
        seg = x[i : i + m]
        seg += amp * env[: seg.size] * np.sin(2 * np.pi * f * np.arange(seg.size) / sr)
    return np.stack([x, x])


def pad_track(s: Spec, sr: int, pitch: float) -> "np.ndarray":
    """click_track plus a sustained two-note chord (pitch / 2 and 3/4 pitch) from the first downbeat: mid-range
    material for the echo and reverb sends to catch."""
    import numpy as np

    x = click_track(s, sr, pitch)
    t = np.arange(x.shape[1]) / sr
    pad = 0.1 * (np.sin(2 * np.pi * pitch / 2 * t) + np.sin(2 * np.pi * pitch * 0.75 * t))
    pad[: int(round(s.first_downbeat_s * sr))] = 0.0
    return (x + pad.astype(np.float32)).astype(np.float32)


def click_offset(x: "np.ndarray", sr: int, t: float, search_s: float = 0.06) -> float:
    """Seconds between `t` and the loudest click within +-search_s of it in a (2, n) signal (inf: nothing there).
    The envelope is high-passed above 500 Hz so the bass under the clicks doesn't count."""
    import numpy as np
    from scipy import signal

    i0, i1 = max(0, int((t - search_s) * sr)), min(x.shape[1], int((t + search_s) * sr))
    if i1 - i0 < 16:
        return float("inf")
    seg = signal.sosfiltfilt(signal.butter(4, 500, "highpass", fs=sr, output="sos"), x[:, i0:i1], axis=1)
    env = np.abs(seg).max(axis=0)
    if env.max() < 1e-3:
        return float("inf")
    return abs((i0 + int(np.argmax(env))) / sr - t)
