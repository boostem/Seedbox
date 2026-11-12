"""`python -m seedbox2.eval cues`: learn cue points and transition lengths from real DJ mixes (#55).

    python -m seedbox2.eval cues train transitions.jsonl [--out model.json | --install] [--holdout 0.2]
    python -m seedbox2.eval cues eval transitions.jsonl [--model model.json] [--json report.json]
    python -m seedbox2.eval cues synth out.jsonl [--mixes 60]      (synthetic mixes: checks the pipeline, not music)

transitions.jsonl has one aligned transition per line: where a DJ brought B in over A, in each track's own time.

    {"mix": "mix id", "genre": "house",                       # mix: held out together; genre: optional
     "a": {"hash": "<library hash>"}, "b": {"hash": "..."},     # or {"analysis": {...}} in musicreco's shape
     "a_in_s": 312.4,      # A's track time when B comes in
     "b_cue_s": 0.0,       # B's track time at that moment (its cue-in)
     "length_s": 30.1,     # how long both play
     "key_clash": false}   # optional

The tracks are analysed by our own analysers (the library, by hash): the DJ's choice only says where. Each transition
becomes a choice among the planner's candidates (`transitions.blend_windows`, every phrase-aligned overlap of 8, 16
and 32 bars); the DJ's is the candidate nearest to what they played. A conditional logit over the candidates
(`musicreco.cuescore.FEATURES`) learns which one a DJ picks; a Platt scaling puts its utility on the planner's
0-100 fit. Whole mixes are held out, and `eval` reports how often the planner's choice (hard limits included) lands
within 8 bars of the DJ's cue, against the hand-set penalties. Kim et al. 2020 measured 74 % between DJs.

Data: see "Learnt cue points" in seedbox2/README.md (no audio and no dataset ships with Seedbox).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from musicreco import cuescore, transitions
from musicreco.cuescore import FEATURES, NET_INPUTS, CueModel

PHRASE = transitions.PHRASE
AGREE_BARS = 8  # "within 8 bars" (Kim et al. 2020: 74 % between DJs, 86 % within 16)
HUMAN_AGREEMENT = 0.74


@dataclass
class Transition:
    mix: str
    genre: str
    ga: transitions.Grid
    gb: transitions.Grid
    a0: float  # the DJ's, in bars
    b0: float
    length: float
    key_clash: bool = False

    @property
    def energy(self) -> float:
        ga, gb = self.ga, self.gb
        return (ga.mean(ga.energy, ga.outro[0], ga.end) + gb.mean(gb.energy, gb.intro[0], gb.intro[1] or 1)) / 2


# --- reading ----------------------------------------------------------------------------------------------------------


def _analysis(side: dict, lib: Any) -> dict | None:
    if isinstance(side.get("analysis"), dict):
        return side["analysis"]
    h = side.get("hash")
    if not h or lib is None:
        return None
    from seedbox2.analysis import vocals
    from seedbox2.sets.timeline import seedbox1_analysis

    row = lib.track(h)
    if row is None:
        return None
    an = seedbox1_analysis(lib, h, float(row["duration"]))
    return vocals.plan_input_for(lib, h, an) if an is not None else None


def read(path: Path, lib: Any = None, log: Any = print) -> list[Transition]:
    """The aligned transitions in `path` whose two tracks have a beat grid (skipped ones are counted in the log)."""
    out, skipped = [], Counter()
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        an_a, an_b = _analysis(rec.get("a") or {}, lib), _analysis(rec.get("b") or {}, lib)
        ga, gb = transitions.make_grid(an_a), transitions.make_grid(an_b)
        if ga is None or gb is None:
            skipped["no analysis"] += 1
            continue
        ratio = gb.bar_s / ga.bar_s
        if not 0.8 < ratio < 1.25:  # half/double time: the planner rescales B's grid, the DJ's bars wouldn't match
            skipped["half/double time"] += 1
            continue
        out.append(Transition(
            mix=str(rec.get("mix") or f"line {n}"), genre=str(rec.get("genre") or "").lower(), ga=ga, gb=gb,
            a0=(float(rec["a_in_s"]) - ga.fd) / ga.bar_s, b0=(float(rec["b_cue_s"]) - gb.fd) / gb.bar_s,
            length=float(rec["length_s"]) / ga.bar_s, key_clash=bool(rec.get("key_clash")),
        ))  # fmt: skip
    if skipped:
        log(f"skipped {sum(skipped.values())} transitions: " + ", ".join(f"{v} {k}" for k, v in skipped.items()))
    return out


def held_out(mix: str, fraction: float) -> bool:
    """Deterministic split by mix: a mix's transitions are all in training or all held out."""
    return zlib.crc32(mix.encode()) % 1000 < fraction * 1000


# --- candidates -------------------------------------------------------------------------------------------------------


def candidates(t: Transition) -> list[transitions.Plan]:
    with cuescore.using(None):
        return [p for n in transitions._LENGTHS for p in transitions.blend_windows(t.ga, t.gb, n, t.key_clash)]


def nearest(t: Transition, plans: list[transitions.Plan]) -> int | None:
    """Index of the candidate closest to the DJ's choice, None when no candidate is within a phrase of their cues."""
    best, best_d = None, math.inf
    for i, p in enumerate(plans):
        if abs(p.a0 - t.a0) > PHRASE or abs(p.b0 - t.b0) > PHRASE:
            continue
        d = abs(p.a0 - t.a0) + abs(p.b0 - t.b0) + abs(math.log2(p.length / max(t.length, 1.0))) * PHRASE
        if d < best_d:
            best, best_d = i, d
    return best


# --- fitting ----------------------------------------------------------------------------------------------------------


def length_prior(ts: Iterable[Transition], strength: float = 8.0) -> dict[str, dict[int, float]]:
    """P(length | genre, energy) over 8/16/32/64 bars: counts, smoothed towards the coarser level (genre/energy ->
    */energy -> */* -> the default prior) with `strength` pseudo-transitions."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for t in ts:
        n = cuescore.snap_length(t.length)
        b = cuescore.energy_bucket(t.energy)
        counts["*/*"][n] += 1
        counts[f"*/{b}"][n] += 1
        if t.genre:
            counts[f"{t.genre}/{b}"][n] += 1

    def smooth(c: Counter, base: dict[int, float]) -> dict[int, float]:
        total = sum(c.values()) + strength
        return {n: (c[n] + strength * base[n]) / total for n in cuescore.LENGTHS}

    out = {"*/*": smooth(counts["*/*"], cuescore.DEFAULT_LENGTH_PRIOR)}
    for key in sorted(k for k in counts if k.startswith("*/") and k != "*/*"):
        out[key] = smooth(counts[key], out["*/*"])
    for key in sorted(k for k in counts if not k.startswith("*/")):
        out[key] = smooth(counts[key], out.get("*/" + key.split("/")[1], out["*/*"]))
    return out


def design(ts: list[Transition], prior: CueModel, names: tuple[str, ...] = FEATURES,
           mixes: list[str] | None = None) -> list[tuple[np.ndarray, int]]:  # fmt: skip
    """Per usable transition: the candidates' feature matrix (columns `names`) and the DJ's row. `mixes` gets each
    row's mix."""
    rows = []
    for t in ts:
        plans = candidates(t)
        y = nearest(t, plans)
        if y is None or len(plans) < 2:
            continue
        x = [cuescore.features(t.ga, t.gb, p.a0, p.b0, p.length, t.key_clash, p.a_loop, p.b_loop, prior) for p in plans]
        rows.append((np.array([[f[k] for k in names] for f in x]), y))
        if mixes is not None:
            mixes.append(t.mix)
    return rows


def fit_logit(rows: list[tuple[np.ndarray, int]], l2: float = 0.05, steps: int = 1500, lr: float = 0.05) -> np.ndarray:
    """Conditional logit (a softmax over each transition's candidates), L2-regularised, by Adam."""
    w = np.zeros(len(FEATURES))
    m, v = np.zeros_like(w), np.zeros_like(w)
    for step in range(1, steps + 1):
        g = l2 * w
        for x, y in rows:
            u = x @ w
            p = np.exp(u - u.max())
            p /= p.sum()
            g += (p @ x - x[y]) / len(rows)
        m = 0.9 * m + 0.1 * g
        v = 0.999 * v + 0.001 * g * g
        w -= lr * (m / (1 - 0.9**step)) / (np.sqrt(v / (1 - 0.999**step)) + 1e-8)
    return w


def platt(rows: list[tuple[np.ndarray, int]], w: Any, steps: int = 500, ridge: float = 1e-3) -> tuple[float, float]:
    """scale, bias so that sigmoid(scale * u + bias) is the chance a candidate is the DJ's, classes weighted equally
    (the DJ's pick reads above 50, the rest below). On the utility as the planner sees it (not relative to the
    transition's best candidate, which the planner doesn't know when it scores one); `ridge` keeps it finite when
    the training transitions are separable."""
    us, ys = [], []
    for x, y in rows:
        u = x @ w if isinstance(w, np.ndarray) else w(x)
        us.append(u)
        ys.append(np.eye(len(u))[y])
    u, yv = np.concatenate(us), np.concatenate(ys)
    wt = np.where(yv == 1, 0.5 / max(yv.sum(), 1), 0.5 / max((1 - yv).sum(), 1))
    a, c = 1.0, 0.0
    for _ in range(steps):  # Newton on two parameters
        p = 1 / (1 + np.exp(-(a * u + c)))
        r = wt * (p - yv)
        s = wt * p * (1 - p) + 1e-9
        H = np.array([[(s * u * u).sum(), (s * u).sum()], [(s * u).sum(), s.sum()]]) + (ridge + 1e-9) * np.eye(2)
        da, dc = np.linalg.solve(H, [(r * u).sum() + ridge * a, r.sum() + ridge * c])
        a, c = a - da, c - dc
        if abs(da) + abs(dc) < 1e-9:
            break
    return float(a), float(c)


# --- the network (ONNX) -----------------------------------------------------------------------------------------------


@dataclass
class Net:
    mu: np.ndarray
    sd: np.ndarray
    w1: np.ndarray  # inputs x hidden
    b1: np.ndarray
    w2: np.ndarray  # hidden

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return np.tanh(((x - self.mu) / self.sd) @ self.w1 + self.b1) @ self.w2

    def onnx(self) -> bytes:
        """The network as an ONNX graph: float32 [n, inputs] -> [n, 1]. Needs the `onnx` package (dev only)."""
        import onnx
        from onnx import TensorProto, helper, numpy_helper

        def const(name: str, a: np.ndarray) -> onnx.TensorProto:
            return numpy_helper.from_array(np.asarray(a, dtype=np.float32), name)

        nodes = [
            helper.make_node("Sub", ["x", "mu"], ["xc"]),
            helper.make_node("Div", ["xc", "sd"], ["xs"]),
            helper.make_node("MatMul", ["xs", "w1"], ["z"]),
            helper.make_node("Add", ["z", "b1"], ["zb"]),
            helper.make_node("Tanh", ["zb"], ["h"]),
            helper.make_node("MatMul", ["h", "w2"], ["u"]),
        ]
        graph = helper.make_graph(
            nodes, "seedbox_cue_net",
            [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["n", len(self.mu)])],
            [helper.make_tensor_value_info("u", TensorProto.FLOAT, ["n", 1])],
            [const("mu", self.mu), const("sd", self.sd), const("w1", self.w1), const("b1", self.b1),
             const("w2", self.w2.reshape(-1, 1))],
        )  # fmt: skip
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], producer_name="seedbox2.eval.cues")
        model.ir_version = 8  # readable by older onnxruntime builds (Intel Macs stay on 1.23)
        onnx.checker.check_model(model)
        return model.SerializeToString()


def _batch(rows: list[tuple[np.ndarray, int]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Candidates padded to [transitions, max candidates, inputs], the mask of real ones, the DJ's index."""
    c = max(len(x) for x, _ in rows)
    d = rows[0][0].shape[1]
    xb, mask = np.zeros((len(rows), c, d)), np.zeros((len(rows), c), dtype=bool)
    for i, (x, _) in enumerate(rows):
        xb[i, : len(x)], mask[i, : len(x)] = x, True
    return xb, mask, np.array([y for _, y in rows])


def _nll(net: Net, rows: list[tuple[np.ndarray, int]]) -> float:
    total = 0.0
    for x, y in rows:
        u = net(x)
        total += float(np.log(np.exp(u - u.max()).sum()) + u.max() - u[y])
    return total / max(len(rows), 1)


def fit_net(rows: list[tuple[np.ndarray, int]], hidden: int = 8, l2: float = 1e-2, steps: int = 1500,
            lr: float = 0.005, seed: int = 55, val: list[tuple[np.ndarray, int]] | None = None,
            log: Any = None) -> tuple[Net, int]:  # fmt: skip
    """A one-hidden-layer network trained like the logit (a softmax over each transition's candidates), by Adam.
    With `val`, returns the step count with the lowest validation loss (checked every 50 steps) and that network."""
    allx = np.concatenate([x for x, _ in rows])
    mu, sd = allx.mean(axis=0), allx.std(axis=0)
    sd = np.where(sd < 1e-6, 1.0, sd)
    xb, mask, y = _batch(rows)
    xs = (xb - mu) / sd
    rng = np.random.default_rng(seed)
    d = xs.shape[2]
    params = [rng.normal(0, 1 / np.sqrt(d), (d, hidden)), np.zeros(hidden), rng.normal(0, 1 / np.sqrt(hidden), hidden)]
    m = [np.zeros_like(p) for p in params]
    v = [np.zeros_like(p) for p in params]
    t_idx = np.arange(len(rows))
    best: tuple[float, int, Net] | None = None
    for step in range(1, steps + 1):
        w1, b1, w2 = params
        h = np.tanh(xs @ w1 + b1)  # T x C x H
        u = np.where(mask, h @ w2, -np.inf)
        p = np.exp(u - u.max(axis=1, keepdims=True))
        p /= p.sum(axis=1, keepdims=True)
        du = p.copy()
        du[t_idx, y] -= 1.0
        du /= len(rows)
        g2 = np.einsum("tch,tc->h", h, du) + l2 * w2
        dz = du[:, :, None] * w2[None, None, :] * (1 - h * h)
        g1 = np.einsum("tcd,tch->dh", xs, dz) + l2 * w1
        gb = dz.sum(axis=(0, 1))
        for i, g in enumerate((g1, gb, g2)):
            m[i] = 0.9 * m[i] + 0.1 * g
            v[i] = 0.999 * v[i] + 0.001 * g * g
            params[i] = params[i] - lr * (m[i] / (1 - 0.9**step)) / (np.sqrt(v[i] / (1 - 0.999**step)) + 1e-8)
        if val is not None and step % 50 == 0:
            net = Net(mu, sd, *[p.copy() for p in params])
            loss = _nll(net, val)
            if best is None or loss < best[0]:
                best = (loss, step, net)
    if best is not None:
        if log:
            log(f"network: best validation loss {best[0]:.3f} at step {best[1]}")
        return best[2], best[1]
    return Net(mu, sd, *params), steps


def _logit_nll(w: np.ndarray, rows: list[tuple[np.ndarray, int]]) -> float:
    total = 0.0
    for x, y in rows:
        u = x @ w
        total += float(np.log(np.exp(u - u.max()).sum()) + u.max() - u[y])
    return total / max(len(rows), 1)


def train(ts: list[Transition], holdout: float = 0.2, log: Any = print, kind: str = "auto") -> CueModel:
    """kind: "logit" (linear weights), "net" (the ONNX network) or "auto": the one with the lower loss on a fifth of
    the training mixes, each fitted on the other four fifths."""
    train_ts = [t for t in ts if not held_out(t.mix, holdout)]
    prior = CueModel(weights={}, length_prior=length_prior(train_ts))
    row_mixes: list[str] = []
    rows = design(train_ts, prior, FEATURES, row_mixes)
    if not rows:
        raise SystemExit("no usable transitions to train on (none has a candidate within a phrase of the DJ's cues)")
    w = fit_logit(rows)
    info: dict[str, Any] = {"transitions": len(rows), "mixes": len({t.mix for t in train_ts}), "holdout": holdout,
                            "unusable": len(train_ts) - len(rows)}  # fmt: skip
    net = None
    if kind in ("net", "auto"):
        net_rows = design(train_ts, prior, NET_INPUTS)
        val = [held_out("val:" + mix, 0.2) for mix in row_mixes]
        tr = [r for r, v in zip(net_rows, val, strict=True) if not v]
        va = [r for r, v in zip(net_rows, val, strict=True) if v]
        steps = 1500
        if tr and va:
            net_in, steps = fit_net(tr, val=va, steps=3000, log=log)
            info["val_loss_net"] = round(_nll(net_in, va), 4)
            lin_tr = [r for r, v in zip(rows, val, strict=True) if not v]
            lin_va = [r for r, v in zip(rows, val, strict=True) if v]
            info["val_loss_logit"] = round(_logit_nll(fit_logit(lin_tr), lin_va), 4)
            log(f"validation loss: network {info['val_loss_net']}, logit {info['val_loss_logit']} "
                f"({len(va)} transitions)")  # fmt: skip
        if kind == "net" or info.get("val_loss_net", math.inf) < info.get("val_loss_logit", -math.inf):
            net, _ = fit_net(net_rows, steps=steps)
            info["net_steps"] = steps
            scale, bias = platt(net_rows, net)
            info["logit_scale"], info["logit_bias"] = platt(rows, w)  # for the comparison in the report
    if net is None:
        scale, bias = platt(rows, w)
    info["kind"] = "net" if net is not None else "logit"
    model = CueModel(weights=dict(zip(FEATURES, map(float, w), strict=True)), scale=scale, bias=bias,
                     length_prior=prior.length_prior, info=info, net=net.onnx() if net is not None else None)  # fmt: skip
    log(f"trained on {len(rows)} transitions from {model.info['mixes']} mixes "
        f"({model.info['unusable']} had no candidate within a phrase of the DJ's cues)")  # fmt: skip
    return model


# --- evaluation -------------------------------------------------------------------------------------------------------


def agreement(ts: list[Transition], model: CueModel | None) -> dict[str, Any]:
    """How often the planner's own choice (transitions._plan, hard limits and all) is within 8 bars of the DJ's."""
    n = cue_b = cue_a = both = within16 = length_ok = 0
    with cuescore.using(model):
        for t in ts:
            p = transitions._plan(t.ga, t.gb, t.key_clash)
            n += 1
            db, da = abs(p.b0 - t.b0), abs(p.a0 - t.a0)
            cue_b += db <= AGREE_BARS
            cue_a += da <= AGREE_BARS
            both += db <= AGREE_BARS and da <= AGREE_BARS
            within16 += db <= 2 * AGREE_BARS and da <= 2 * AGREE_BARS
            length_ok += p.length == cuescore.snap_length(t.length)
    def r(k: int) -> float | None:
        return round(k / n, 4) if n else None

    return {"transitions": n, "b_cue_within_8": r(cue_b), "a_in_within_8": r(cue_a), "both_within_8": r(both),
            "both_within_16": r(within16), "length_match": r(length_ok)}  # fmt: skip


def report(ts: list[Transition], model: CueModel, holdout: float) -> dict[str, Any]:
    test = [t for t in ts if held_out(t.mix, holdout)]
    learnt, hand = agreement(test, model), agreement(test, None)
    out = {"held_out_mixes": len({t.mix for t in test}), "learnt": learnt, "hand_set": hand,
           "target": HUMAN_AGREEMENT,
           "meets_target": learnt["both_within_8"] is not None and learnt["both_within_8"] >= HUMAN_AGREEMENT}  # fmt: skip
    if model.net is not None:  # the same model's linear weights, for comparison
        out["logit"] = agreement(test, CueModel(weights=model.weights, length_prior=model.length_prior,
                                                scale=float(model.info.get("logit_scale", model.scale)),
                                                bias=float(model.info.get("logit_bias", model.bias))))  # fmt: skip
    return out


def _print_report(r: dict[str, Any]) -> None:
    print(f"held out: {r['learnt']['transitions']} transitions from {r['held_out_mixes']} mixes")
    cols = [("learnt", "learnt"), *([("logit", "logit")] if "logit" in r else []), ("hand_set", "hand-set")]
    print(f"{'':16}" + "".join(f"{label:>10}" for _, label in cols))
    for k in ("b_cue_within_8", "a_in_within_8", "both_within_8", "both_within_16", "length_match"):
        print(f"{k:16}" + "".join(f"{(r[c][k] or 0) * 100:9.1f}%" for c, _ in cols))
    print(f"target {r['target'] * 100:.0f}% (both cues within 8 bars): {'met' if r['meets_target'] else 'not met'}")


# --- synthetic mixes --------------------------------------------------------------------------------------------------


def synth_track(rng: np.random.Generator, name: str) -> dict:
    """A random club track in musicreco's analysis shape: drum or musical edges, vocals, drops, a phrase grid."""
    bpm = float(rng.choice([122.0, 124.0, 126.0, 128.0, 130.0]))
    bar_s = 240.0 / bpm
    intro, outro = int(rng.choice([8, 16, 32])), int(rng.choice([8, 16, 32]))
    bars = int(rng.choice([128, 144, 160, 192]))
    intro_drums, outro_drums = bool(rng.random() < 0.7), bool(rng.random() < 0.7)
    drops = sorted({int(d) for d in rng.choice(range(intro + 16, bars - outro - 16, 8), size=int(rng.integers(1, 3)))})
    vocal_spans = [(int(s), int(s) + int(rng.choice([16, 32]))) for s in rng.choice(range(intro, bars - outro, 8), 2)]
    if not intro_drums and rng.random() < 0.5:
        vocal_spans.append((0, intro))
    if not outro_drums and rng.random() < 0.5:
        vocal_spans.append((bars - outro, bars))
    energy, low, vocal, kinds = [], [], [], []
    for i in range(bars):
        kind = "intro" if i < intro else "outro" if i >= bars - outro else "body"
        if any(d <= i < d + 16 for d in drops):
            kind = "drop"
        elif any(d - 8 <= i < d for d in drops):
            kind = "breakdown"
        drums = (kind == "intro" and intro_drums) or (kind == "outro" and outro_drums)
        e = 0.55 if drums else {"breakdown": 0.4, "drop": 1.0, "intro": 0.6, "outro": 0.6}.get(kind, 0.8)
        lo = 0.3 if drums else {"breakdown": 0.15, "drop": 0.9}.get(kind, 0.7)
        energy.append(round(float(np.clip(e + rng.normal(0, 0.03), 0, 1)), 3))
        low.append(round(float(np.clip(lo + rng.normal(0, 0.03), 0, 1)), 3))
        vocal.append(0.85 if any(a <= i < b for a, b in vocal_spans) else 0.05)
        kinds.append(kind)
    secs, start = [], 0
    for i in range(1, bars + 1):
        if i == bars or kinds[i] != kinds[start]:
            secs.append({"kind": kinds[start], "start_s": round(start * bar_s, 3), "end_s": round(i * bar_s, 3)})
            start = i

    def edge(a: int, b: int, drums: bool) -> dict:
        sung = any(vocal[i] > 0.5 for i in range(a, b))
        return {"bars": b - a, "start_s": round(a * bar_s, 3), "end_s": round(b * bar_s, 3),
                "drums_only": drums and not sung, "has_vocals": sung}  # fmt: skip

    return {"track_id": name, "bpm": bpm, "first_downbeat_s": 0.0, "bar_s": round(bar_s, 6), "bar_count": bars,
            "phrase_offset": 0, "energy_per_bar": energy, "low_per_bar": low, "vocal_per_bar": vocal,
            "sections": secs, "intro": edge(0, intro, intro_drums),
            "outro": edge(bars - outro, bars, outro_drums)}  # fmt: skip


# The synthetic DJ: cues on 16/32-bar phrases, rides drums-only edges, keeps vocals apart, prefers 32 bars.
# Its taste is not the hand-set penalties', so the check shows the model learns whatever the mixes show.
SYNTH_DJ = {"a_level16": 1.5, "b_first_cue": 1.0, "a_in_outro": 1.2, "a_body_cut": -1.5, "a_outro_skip": -1.0,
            "b_body_under": -1.0, "vocal_soft": -6.0, "len32": 1.2, "len8": -0.8, "a_perc": 0.8,
            "loops": -0.8}  # fmt: skip


def synth(n_mixes: int, per_mix: int = 8, seed: int = 55) -> list[dict]:
    rng = np.random.default_rng(seed)
    dj = CueModel(weights=SYNTH_DJ)
    out = []
    for m in range(n_mixes):
        genre = ["house", "techno"][m % 2]
        prev = synth_track(rng, f"m{m}t0")
        for k in range(1, per_mix + 1):
            nxt = synth_track(rng, f"m{m}t{k}")
            ga, gb = transitions.make_grid(prev), transitions.make_grid(nxt)
            t = Transition(f"mix {m}", genre, ga, gb, 0, 0, 16)
            plans = candidates(t)
            if plans:
                x = [cuescore.features(ga, gb, p.a0, p.b0, p.length, False, p.a_loop, p.b_loop, dj) for p in plans]
                u = np.array([dj.utility(f) for f in x]) + rng.gumbel(size=len(plans)) * 0.7
                p = plans[int(u.argmax())]
                da, db, dl = rng.normal(0, 0.15, 3)  # alignment noise, in bars
                out.append({"mix": f"mix {m}", "genre": genre, "a": {"analysis": prev}, "b": {"analysis": nxt},
                            "a_in_s": round((p.a0 + da) * ga.bar_s, 3),
                            "b_cue_s": round(max(0.0, p.b0 + db) * gb.bar_s, 3),
                            "length_s": round((p.length + dl) * ga.bar_s, 3)})  # fmt: skip
            prev = nxt
    return out


# --- CLI --------------------------------------------------------------------------------------------------------------


def _library(path: Path | None, needed: bool) -> Any:
    """The library the transitions' hashes are looked up in, opened only when a line refers to one."""
    if not needed:
        return None
    from seedbox2.library.repo import Library
    from seedbox2.server.deps import default_path

    return Library.open(path or default_path())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m seedbox2.eval cues", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train", help="fit the cue model on aligned transitions and report on the held-out mixes")
    t.add_argument("transitions", type=Path)
    t.add_argument("--out", type=Path, help="where to write the model (default: print the report only)")
    t.add_argument("--install", action="store_true", help=f"write it to the settings folder as {cuescore.MODEL_FILE}")
    t.add_argument("--holdout", type=float, default=0.2, help="share of mixes held out (default 0.2)")
    t.add_argument("--json", type=Path, help="write the report as JSON")
    t.add_argument("--library", type=Path, help="library database for the tracks' hashes (default: Seedbox 2's)")
    t.add_argument("--kind", choices=("auto", "net", "logit"), default="auto",
                   help="net: a small ONNX network over the features and the pair's tempo and energy; logit: linear "
                        "weights; auto (default): the one that does better on a fifth of the training mixes")  # fmt: skip
    e = sub.add_parser("eval", help="the planner against the DJs on the held-out mixes")
    e.add_argument("transitions", type=Path)
    e.add_argument("--model", type=Path, help="a model file (default: the installed one)")
    e.add_argument("--holdout", type=float, default=0.2)
    e.add_argument("--json", type=Path)
    e.add_argument("--library", type=Path)
    s = sub.add_parser("synth", help="write synthetic mixes (a known DJ taste) to check the pipeline")
    s.add_argument("out", type=Path)
    s.add_argument("--mixes", type=int, default=60)
    args = ap.parse_args(argv)

    if args.cmd == "synth":
        recs = synth(args.mixes)
        args.out.write_text("".join(json.dumps(r) + "\n" for r in recs))
        print(f"wrote {len(recs)} transitions from {args.mixes} synthetic mixes to {args.out}")
        return 0

    needs_lib = '"hash"' in args.transitions.read_text()
    ts = read(args.transitions, _library(args.library, needs_lib))
    if args.cmd == "train":
        model = train(ts, args.holdout, kind=args.kind)
        out = args.out
        if args.install:
            from seedbox2.paths import folders

            out = folders()["settings"] / cuescore.MODEL_FILE
        if out is not None:
            model.save(out)
            print(f"wrote {out} ({out.stat().st_size} bytes)")
    else:
        model = CueModel.load(args.model) if args.model else cuescore.active()
        if model is None:
            raise SystemExit(f"no cue model: pass --model or train one with --install (or set ${cuescore.MODEL_ENV})")
    r = report(ts, model, args.holdout)
    _print_report(r)
    if args.json:
        args.json.write_text(json.dumps(r, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
