"""Objective metrics for one rendered transition (#54): how well the decks line up and how much they fight.

Input is a render as any engine can describe it (`Render`): the master mix, each deck's own signal after its fader
(echo/reverb returns included, before the bus leveller and limiter), optional per-deck stems (`vocals`, `bass`, ...;
#44 adds them) and the plan, for the tempo and keys. Everything is measured on the audio, not read off the plan, so
Seedbox 1 and Seedbox 2 renders are compared like for like.

- overlap: where both decks are audible (each within OVERLAP_RANGE_DB of its own loudest moment).
- beat alignment: onset envelopes (spectral flux) of both decks cross-correlated in ALIGN_WIN_S windows over the
  overlap; the lag of the peak is the error (positive: B late), median and 95th percentile of |error| in ms.
- bass overlap: energy below 120 Hz (from the bass stems when there are any, else a 4th-order low-pass of the whole
  deck, kick included: the "never both bass EQs up" rule) on both decks at once, within BASS_RANGE_DB of the louder deck's typical bass level: seconds, and dB·s above that floor.
- vocal overlap: seconds both decks' vocals are active (from the vocal stems; without them a rough proxy, tonal energy
  in the voice band, reported as `vocal_source: "proxy"`).
- loudness: short-term LUFS (3 s) through the overlap against the line from A's level just before it to B's level
  just after it: the deepest dip and the biggest bump in LU; true peak (dBTP) and clipped samples of the mix.
- masking: per band, the share of the two decks' energy that is common to both, sum(2·min(Ea, Eb)) / sum(Ea + Eb)
  over the overlap's STFT frames: 0 when one deck owns the band, 1 when both are equally loud all the time.
- key and stretch: each deck's tempo ratio (master BPM / native BPM) over the bars it plays, the pitch shift that
  results (none with key lock, plus any `pitch_semitones` the plan asks for) and the Camelot distance of the keys as
  heard.

`checks()` turns the numbers into pass/warn/fail items for the transition editor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

from musicreco.mixing import _parse_camelot, camelot_move
from musicreco.mixplan import deck_bpm, lane_value
from musicreco.mixrender import _block_power, true_peak_db

LEVEL_WIN_S, LEVEL_HOP_S = 0.4, 0.05
OVERLAP_RANGE_DB = 30.0  # a deck counts as audible within this of its own loudest 400 ms
SILENCE_DBFS = -60.0
MIN_OVERLAP_S = 1.0  # shorter than this (level windows smearing over a cut): no overlap
ALIGN_WIN_S, ALIGN_HOP_S, ALIGN_MAX_LAG_S = 2.0, 1.0, 0.08
ONSET_FFT, ONSET_HOP = 1024, 128
BASS_HZ = 120.0
BASS_RANGE_DB = 20.0
VOCAL_RANGE_DB = 25.0
VOICE_BAND_HZ = (250.0, 4000.0)
VOICE_SHARE = 0.05
SHORT_TERM_S, ST_HOP_S = 3.0, 0.1
REF_S = 4.0  # each track's level: the 4 s of short-term blocks nearest the overlap
CLIP_LEVEL = 0.9999
MASK_FFT, MASK_HOP = 4096, 1024
BANDS = {"sub": (20, 60), "bass": (60, 250), "low_mid": (250, 2000), "high_mid": (2000, 6000), "high": (6000, 20000)}


@dataclass
class Render:
    """One rendered transition. Arrays are (channels, n) float at `sr`, all on the same timeline as `mix`."""

    mix: np.ndarray
    decks: dict[str, np.ndarray]  # "A", "B": each deck after its fader, its effect returns included
    sr: int
    plan: dict | None = None  # musicreco MixPlan (v4) or Seedbox 2's plan: tempo, a/b decks, key_lock
    stems: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)  # {"A": {"vocals": ..., "bass": ...}}
    keys: dict[str, str] = field(default_factory=dict)  # Camelot keys of the tracks: {"A": "8A", "B": "9A"}

    @classmethod
    def from_mixrender(cls, r: dict, plan: dict | None, keys: dict[str, str] | None = None) -> Render:
        """From `musicreco.mixrender.render_audio`'s result: each deck with its effect returns."""
        decks = {k: sum((s for n, s in r["stems"].items() if n.split()[0] == k), np.zeros_like(r["mix"])) for k in "AB"}
        return cls(mix=r["mix"], decks=decks, sr=r["sr"], plan=plan, keys=keys or {})


# --- helpers ---------------------------------------------------------------------------------------------------------


def _mono(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return x.mean(axis=0) if x.ndim == 2 else x


def _frames_db(x: np.ndarray, sr: int, win_s: float = LEVEL_WIN_S, hop_s: float = LEVEL_HOP_S) -> np.ndarray:
    """RMS level (dBFS) of mono x in windows of win_s every hop_s, frame i centred at i * hop_s."""
    p = x * x
    win, hop = max(1, round(win_s * sr)), max(1, round(hop_s * sr))
    c = np.concatenate([[0.0], np.cumsum(p)])
    centres = np.arange(0, p.size, hop)
    lo, hi = np.clip(centres - win // 2, 0, p.size), np.clip(centres + win // 2, 0, p.size)
    ms = (c[hi] - c[lo]) / np.maximum(hi - lo, 1)
    return 10 * np.log10(ms + 1e-12)


def _audible(db: np.ndarray, range_db: float) -> np.ndarray:
    return db > max(SILENCE_DBFS, float(db.max()) - range_db) if db.size else db.astype(bool)


def _lowpass(x: np.ndarray, sr: int, hz: float) -> np.ndarray:
    return signal.sosfiltfilt(signal.butter(4, hz, "low", fs=sr, output="sos"), x)


def _round(x: float | None, nd: int = 2) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), nd)


# --- overlap ---------------------------------------------------------------------------------------------------------


def overlap(r: Render) -> dict:
    """{start_s, end_s, seconds, mask (per LEVEL_HOP_S frame), levels}: where both decks are audible."""
    la, lb = (_frames_db(_mono(r.decks[k]), r.sr) for k in ("A", "B"))
    both = _audible(la, OVERLAP_RANGE_DB) & _audible(lb, OVERLAP_RANGE_DB)
    idx = np.flatnonzero(both)
    if idx.size * LEVEL_HOP_S < MIN_OVERLAP_S:
        both = np.zeros_like(both)
        return {"start_s": None, "end_s": None, "seconds": 0.0, "mask": both, "levels": (la, lb)}
    return {"start_s": idx[0] * LEVEL_HOP_S, "end_s": (idx[-1] + 1) * LEVEL_HOP_S,
            "seconds": float(idx.size * LEVEL_HOP_S), "mask": both, "levels": (la, lb)}


# --- beat alignment --------------------------------------------------------------------------------------------------


def _onset_env(x: np.ndarray, sr: int) -> np.ndarray:
    """Spectral flux (half-wave rectified, log-compressed magnitude) every ONSET_HOP samples."""
    _, _, z = signal.stft(x, sr, nperseg=ONSET_FFT, noverlap=ONSET_FFT - ONSET_HOP, boundary=None, padded=False)
    mag = np.log1p(100 * np.abs(z))
    flux = np.maximum(np.diff(mag, axis=1), 0).sum(axis=0)
    return np.concatenate([[0.0], flux])


def beat_alignment(r: Render, ov: dict) -> dict:
    """Per-window onset cross-correlation lag between the decks over the overlap (ms; positive = B late)."""
    if not ov["seconds"]:
        return {"median_ms": None, "p95_ms": None, "mean_signed_ms": None, "windows": 0}
    ea, eb = (_onset_env(_mono(r.decks[k]), r.sr) for k in ("A", "B"))
    fps = r.sr / ONSET_HOP
    win, hop, maxlag = int(ALIGN_WIN_S * fps), int(ALIGN_HOP_S * fps), int(ALIGN_MAX_LAG_S * fps)
    mask = ov["mask"]
    lags = []
    for s in range(int(ov["start_s"] * fps), max(int(ov["end_s"] * fps) - win, int(ov["start_s"] * fps)) + 1, hop):
        f0, f1 = int(s / fps / LEVEL_HOP_S), int((s + win) / fps / LEVEL_HOP_S)
        if mask[f0:f1].mean() < 0.8:  # both decks have to play through the window
            continue
        a, b = ea[s: s + win], eb[s: s + win]
        if a.size < win or b.size < win or a.std() == 0 or b.std() == 0:
            continue
        a, b = (a - a.mean()) / a.std(), (b - b.mean()) / b.std()
        cc = signal.correlate(b, a, mode="full", method="fft") / win
        mid = win - 1
        seg = cc[mid - maxlag: mid + maxlag + 1]
        k = int(np.argmax(seg))
        if seg[k] < 0.2:  # no common pulse in this window (a breakdown on one deck)
            continue
        off = 0.0
        if 0 < k < seg.size - 1:  # parabolic interpolation: sub-hop precision
            y0, y1, y2 = seg[k - 1], seg[k], seg[k + 1]
            den = y0 - 2 * y1 + y2
            off = 0.5 * (y0 - y2) / den if den else 0.0
        lags.append((k - maxlag + off) / fps * 1000)
    if not lags:
        return {"median_ms": None, "p95_ms": None, "mean_signed_ms": None, "windows": 0}
    a = np.abs(lags)
    return {"median_ms": _round(np.median(a)), "p95_ms": _round(np.percentile(a, 95)),
            "mean_signed_ms": _round(np.mean(lags)), "windows": len(lags)}


# --- bass and vocals -------------------------------------------------------------------------------------------------


def bass_overlap(r: Render) -> dict:
    """Seconds and dB·s with both decks' bass (< BASS_HZ) within BASS_RANGE_DB of the louder deck's bass level."""
    lv = []
    for k in ("A", "B"):
        stem = r.stems.get(k, {}).get("bass")
        src = _mono(stem) if stem is not None else _mono(r.decks[k])
        lv.append(_frames_db(_lowpass(src, r.sr, BASS_HZ), r.sr, 0.1, LEVEL_HOP_S))
    la, lb = lv
    ref = max(np.percentile(la, 95), np.percentile(lb, 95))
    floor = max(SILENCE_DBFS, ref - BASS_RANGE_DB)
    lo = np.minimum(la, lb)
    both = lo > floor
    return {"seconds": _round(both.sum() * LEVEL_HOP_S), "db_s": _round(((lo - floor) * both).sum() * LEVEL_HOP_S),
            "floor_dbfs": _round(floor, 1), "source": "stems" if all("bass" in r.stems.get(k, {}) for k in "AB")
            else "low-pass"}  # fmt: skip


def _vocal_proxy(x: np.ndarray, sr: int) -> np.ndarray:
    """Per-LEVEL_HOP_S frame: voice-band level (dB), set to -inf where that band is noisy (spectral flatness > 0.3)
    or carries less than VOICE_SHARE of the frame's energy: drums and hats don't count as singing (pads and leads
    do, which is why it's only a proxy until the vocal stems)."""
    nfft, hop = 2048, round(LEVEL_HOP_S * sr)
    f, _, z = signal.stft(x, sr, nperseg=nfft, noverlap=nfft - hop, boundary="even")
    p = np.abs(z) ** 2
    band = (f >= VOICE_BAND_HZ[0]) & (f < VOICE_BAND_HZ[1])
    pb = p[band] + 1e-15
    flat = np.exp(np.log(pb).mean(axis=0)) / pb.mean(axis=0)
    share = pb.sum(axis=0) / (p.sum(axis=0) + 1e-15)
    db = 10 * np.log10(2 * pb.sum(axis=0) + 1e-12)
    db[(flat > 0.3) | (share < VOICE_SHARE)] = -np.inf
    return db


def vocal_overlap(r: Render) -> dict:
    """Seconds both decks' vocals are active at once."""
    act, srcs = [], []
    for k in ("A", "B"):
        stem = r.stems.get(k, {}).get("vocals")
        if stem is not None:
            db, srcs = _frames_db(_mono(stem), r.sr), srcs + ["stems"]
        else:
            db, srcs = _vocal_proxy(_mono(r.decks[k]), r.sr), srcs + ["proxy"]
        fin = db[np.isfinite(db)]
        act.append(db > max(SILENCE_DBFS, float(fin.max()) - VOCAL_RANGE_DB) if fin.size else np.zeros(db.size, bool))
    n = min(a.size for a in act)
    both = act[0][:n] & act[1][:n]
    return {"seconds": _round(both.sum() * LEVEL_HOP_S),
            "a_s": _round(act[0].sum() * LEVEL_HOP_S), "b_s": _round(act[1].sum() * LEVEL_HOP_S),
            "source": "stems" if srcs == ["stems", "stems"] else "proxy"}  # fmt: skip


# --- loudness --------------------------------------------------------------------------------------------------------


def loudness_deviation(r: Render, ov: dict) -> tuple[np.ndarray, np.ndarray, float, float] | None:
    """Short-term LUFS against the line between the tracks' levels through the overlap: (block centres in s, deviation
    in LU, A's level, B's level). None without an overlap or a level to go by."""
    st = -0.691 + 10 * np.log10(_block_power(r.mix, r.sr, SHORT_TERM_S, ST_HOP_S) + 1e-12)
    t = np.arange(st.size) * ST_HOP_S + SHORT_TERM_S / 2  # block centres
    if not ov["seconds"] or st.size < 3:
        return None
    o0, o1 = ov["start_s"], ov["end_s"]
    la, lb = ov["levels"]

    def only(deck_db: np.ndarray, other_db: np.ndarray, sel: np.ndarray, last: bool) -> float | None:
        """Median short-term loudness of the REF_S of blocks nearest the overlap where only this deck plays."""
        fi = np.clip((t / LEVEL_HOP_S).astype(int), 0, deck_db.size - 1)
        keep = sel & _audible(deck_db, OVERLAP_RANGE_DB)[fi] & ~_audible(other_db, OVERLAP_RANGE_DB)[fi] & (st > -70)
        idx = np.flatnonzero(keep)
        if idx.size * ST_HOP_S < 1.0:
            return None
        n = int(REF_S / ST_HOP_S)
        return float(np.median(st[idx[-n:] if last else idx[:n]]))

    # whole 3 s blocks on each side of the overlap, so A's level isn't measured with B already in it
    ref_a = only(la, lb, t + SHORT_TERM_S / 2 <= o0, True)
    ref_b = only(lb, la, t - SHORT_TERM_S / 2 >= o1, False)
    inside = (t >= o0) & (t <= o1)
    if not inside.any():
        inside = np.abs(t - (o0 + o1) / 2) <= ST_HOP_S
    if ref_a is None and ref_b is None:
        ref_a = ref_b = float(np.median(st[st > -70])) if (st > -70).any() else None
        if ref_a is None:
            return None
    ref_a, ref_b = (ref_a if ref_a is not None else ref_b), (ref_b if ref_b is not None else ref_a)
    frac = np.clip((t[inside] - o0) / max(o1 - o0, 1e-9), 0, 1)
    return t[inside], st[inside] - (ref_a + (ref_b - ref_a) * frac), ref_a, ref_b


def loudness(r: Render, ov: dict) -> dict:
    """Short-term LUFS dip/bump through the overlap vs the line between the tracks' levels; true peak; clipping."""
    out = {"true_peak_dbtp": _round(true_peak_db(r.mix)), "clipped_samples": int((np.abs(r.mix) >= CLIP_LEVEL).sum()),
           "dip_lu": None, "bump_lu": None, "a_lufs": None, "b_lufs": None}
    found = loudness_deviation(r, ov)
    if found is not None:
        _, dev, ref_a, ref_b = found
        out.update(dip_lu=_round(min(0.0, float(dev.min()))), bump_lu=_round(max(0.0, float(dev.max()))),
                   a_lufs=_round(ref_a, 1), b_lufs=_round(ref_b, 1))
    return out


# --- masking ---------------------------------------------------------------------------------------------------------


def masking(r: Render, ov: dict) -> dict:
    """Per band: shared energy share of the two decks over the overlap (0 = one deck owns it, 1 = equal fight)."""
    if not ov["seconds"]:
        return {k: None for k in BANDS}
    i0, i1 = int(ov["start_s"] * r.sr), int(ov["end_s"] * r.sr)
    out = {}
    specs = []
    for k in ("A", "B"):
        x = _mono(r.decks[k])[i0:i1]
        if x.size < MASK_FFT:
            return {b: None for b in BANDS}
        f, _, z = signal.stft(x, r.sr, nperseg=MASK_FFT, noverlap=MASK_FFT - MASK_HOP, boundary=None, padded=False)
        specs.append(np.abs(z) ** 2)
    pa, pb = specs
    for name, (lo, hi) in BANDS.items():
        sel = (f >= lo) & (f < hi)
        ea, eb = pa[sel].sum(axis=0), pb[sel].sum(axis=0)
        tot = (ea + eb).sum()
        out[name] = _round(2 * np.minimum(ea, eb).sum() / tot, 3) if tot > 1e-12 else None
    return out


# --- key and stretch -------------------------------------------------------------------------------------------------


def _camelot_shift(code: str, semitones: int) -> str | None:
    """The Camelot code `semitones` up (each semitone is 7 steps round the wheel)."""
    p = _parse_camelot(code)
    if p is None:
        return None
    n, letter = p
    return f"{(n - 1 + 7 * semitones) % 12 + 1}{letter}"


def _wheel_steps(a: str, b: str) -> int | None:
    """Steps round the Camelot wheel (0-6), the letter ignored (relative keys are 0)."""
    pa, pb = _parse_camelot(a), _parse_camelot(b)
    if pa is None or pb is None:
        return None
    d = (pb[0] - pa[0]) % 12
    return min(d, 12 - d)


def key_and_stretch(r: Render) -> dict:
    """Tempo ratio per deck over its playing bars, the resulting pitch shift and the Camelot distance as heard."""
    out: dict = {"a_stretch": None, "b_stretch": None, "max_stretch_pct": None, "a_semitones": 0.0,
                 "b_semitones": 0.0, "key_a": r.keys.get("A") or None, "key_b": r.keys.get("B") or None,
                 "key_b_heard": None, "key_steps": None, "key_move": None}  # fmt: skip
    plan = r.plan
    if plan and plan.get("tempo") and plan.get("a") and plan.get("b"):
        w0, w1 = plan["window"]["start_beat"], plan["window"]["end_beat"]
        lock = bool(plan.get("key_lock", True))
        devs = []
        for k, d in (("a", plan["a"]), ("b", plan["b"])):
            p0, p1 = max(d["play_from_beat"], w0), min(d["play_to_beat"], w1)
            if p1 <= p0:
                continue
            beats = np.linspace(p0, p1, 128, endpoint=False)
            ratio = np.asarray(lane_value(plan["tempo"], beats), dtype=float) / np.asarray(deck_bpm(d, beats), float)
            worst = float(ratio[np.argmax(np.abs(ratio - 1))])
            out[f"{k}_stretch"] = round(worst, 4)
            devs.append(abs(worst - 1) * 100)
            semis = float(d.get("pitch_semitones", 0) or 0) + (0.0 if lock else 12 * math.log2(worst))
            out[f"{k}_semitones"] = round(semis, 2)
        out["max_stretch_pct"] = _round(max(devs)) if devs else None
    ka, kb = out["key_a"], out["key_b"]
    if ka and kb:
        ha = _camelot_shift(ka, round(out["a_semitones"]))
        hb = _camelot_shift(kb, round(out["b_semitones"]))
        if ha and hb:
            out.update(key_b_heard=hb, key_steps=_wheel_steps(ha, hb), key_move=camelot_move(ha, hb))
    return out


# --- everything ------------------------------------------------------------------------------------------------------


def measure(r: Render) -> dict:
    """All metrics for one render, JSON-ready, plus `checks`."""
    ov = overlap(r)
    m = {
        "duration_s": _round(r.mix.shape[-1] / r.sr),
        "overlap": {"start_s": _round(ov["start_s"]), "end_s": _round(ov["end_s"]), "seconds": _round(ov["seconds"])},
        "beat": beat_alignment(r, ov),
        "bass": bass_overlap(r),
        "vocals": vocal_overlap(r),
        "loudness": loudness(r, ov),
        "masking": masking(r, ov),
        "key": key_and_stretch(r),
    }
    m["checks"] = checks(m)
    return m


def _grade(value: float | None, warn: float, fail: float) -> str:
    if value is None:
        return "na"
    return "pass" if value <= warn else "warn" if value <= fail else "fail"


def checks(m: dict) -> list[dict]:
    """Pass/warn/fail items for the transition editor (#79): id, label, status, value, detail."""
    worst = {"pass": 0, "na": 0, "warn": 1, "fail": 2}

    def item(id_: str, label: str, status: str, value: str, detail: str) -> dict:
        return {"id": id_, "label": label, "status": status, "value": value, "detail": detail}

    out = []
    b = m["beat"]
    out.append(item("beat", "Beats in sync", _grade(b["median_ms"], 5, 15),
                    "–" if b["median_ms"] is None else f"{b['median_ms']:.1f} ms",
                    "Median beat offset between the decks over the overlap"
                    + ("" if b["p95_ms"] is None else f" (95th percentile {b['p95_ms']:.1f} ms)")))
    s = m["bass"]["seconds"]
    out.append(item("bass", "One bassline at a time", _grade(s, 0.5, 2.0), f"{s:.1f} s",
                    f"Both decks' bass below {BASS_HZ:.0f} Hz at once ({m['bass']['db_s']:.0f} dB·s)"))
    v = m["vocals"]
    out.append(item("vocals", "One vocal at a time", _grade(v["seconds"], 0.5, 2.0), f"{v['seconds']:.1f} s",
                    "Both decks' vocals at once" + (" (estimated without stems)" if v["source"] == "proxy" else "")))
    lo = m["loudness"]
    swing = None if lo["dip_lu"] is None else max(-lo["dip_lu"], lo["bump_lu"])
    st = _grade(swing, 1.5, 3.0)
    if lo["clipped_samples"]:
        st = "fail"
    elif lo["true_peak_dbtp"] is not None and lo["true_peak_dbtp"] > -1.0 and st == "pass":
        st = "warn"
    out.append(item("loudness", "Even loudness", st,
                    "–" if swing is None else f"{lo['dip_lu']:+.1f} / {lo['bump_lu']:+.1f} LU",
                    f"Dip / bump against the tracks; true peak {lo['true_peak_dbtp']:.1f} dBTP, "
                    f"{lo['clipped_samples']} clipped samples"))
    k = m["key"]
    sk = _grade(k["key_steps"], 1, 2) if k["key_move"] != "relative" else "pass"
    if not m["overlap"]["seconds"]:  # a cut: the keys never sound together
        sk = "pass" if sk != "na" else sk
    ss = _grade(k["max_stretch_pct"], 4.0, 8.0)
    stretch = "–" if k["max_stretch_pct"] is None else f"{k['max_stretch_pct']:.1f}%"
    keys = f"{k['key_a']} → {k['key_b_heard']}" if k["key_steps"] is not None else "keys unknown"
    out.append(item("key", "Key and tempo", max(sk, ss, key=worst.get), f"{keys}, stretch {stretch}",
                    f"Camelot move {k['key_move'] or '?'}; A x{k['a_stretch'] or 1:.3f}, B x{k['b_stretch'] or 1:.3f}"
                    + (f", pitch {k['b_semitones']:+.1f} st" if k["b_semitones"] else "")))
    mb = m["masking"]["bass"]
    out.append(item("masking", "Low end clear", _grade(mb, 0.3, 0.5), "–" if mb is None else f"{mb:.2f}",
                    "Shared energy of the decks in 60–250 Hz over the overlap (0 = one deck owns it)"))
    return out
