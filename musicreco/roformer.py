"""RoFormer source separation (BS-RoFormer and Mel-Band RoFormer), inference only, for musicreco.separator.

An inference-only port of ZFTurbo's Music-Source-Separation-Training ("MSST") models/bs_roformer/bs_roformer.py and
mel_band_roformer.py (MIT License, Copyright (c) 2024 Roman Solovyev (ZFTurbo)), themselves derived from
lucidrains/BS-RoFormer (MIT License, Copyright (c) 2023 Phil Wang). MSST checkpoints load with
load_state_dict(strict=True), no key remapping. Changes vs MSST: training code removed; librosa's mel filterbank
replaced by an exact numpy port (only its > 0 band layout is used); the rotary embedding inlined (same math as
rotary-embedding-torch); STFT / iSTFT and the complex mask product run on real tensors, so they can stay on the CPU
while the network runs on the Apple GPU (no complex ops on MPS); optional attention chunking caps GPU memory; MSST's
overlap-add `demix` reimplemented with per-chunk edge windows.

Imports torch and einops: only musicreco.separator imports this module, and only when it splits a track.
"""

from __future__ import annotations

import os

# set before torch is imported: ops without an MPS kernel fall back to the CPU instead of raising
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

from collections.abc import Callable, Sequence  # noqa: E402

import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.nn as nn  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from einops import pack, rearrange, unpack  # noqa: E402

# ============================================================================= model building blocks


class RMSNorm(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        return F.normalize(x, dim=-1) * self.scale * self.gamma


class RotaryEmbedding(nn.Module):
    """Equivalent of ``rotary_embedding_torch.RotaryEmbedding(dim)`` as used by the RoFormers
    ('lang' freqs, theta=10000, interleaved pairs). Keeps the ``freqs`` parameter so that
    checkpoints (which contain ``...rotary_embed.freqs``) load with strict=True."""

    def __init__(self, dim: int, theta: float = 10000.0):
        super().__init__()
        freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: dim // 2].float() / dim))
        self.freqs = nn.Parameter(freqs, requires_grad=False)
        self._cache: dict = {}

    def _load_from_state_dict(self, *args, **kwargs):
        self._cache.clear()
        super()._load_from_state_dict(*args, **kwargs)

    def _apply(self, fn, *args, **kwargs):  # .to()/.float() etc. invalidate cached tables
        self._cache.clear()
        return super()._apply(fn, *args, **kwargs)

    def rotate_queries_or_keys(self, t):  # t: (..., seq, dim)
        n = t.shape[-2]
        key = (n, t.device, t.dtype)
        cs = self._cache.get(key)
        if cs is None:
            pos = torch.arange(n, device=t.device, dtype=torch.float32)
            ang = pos[:, None] * self.freqs.to(device=t.device, dtype=torch.float32)[None, :]
            ang = ang.repeat_interleave(2, dim=-1)
            cs = (ang.cos().to(t.dtype), ang.sin().to(t.dtype))
            self._cache[key] = cs
        cos, sin = cs
        x1, x2 = t.unflatten(-1, (-1, 2)).unbind(-1)
        rot = torch.stack((-x2, x1), dim=-1).flatten(-2)
        return t * cos + rot * sin


def _sdpa(q, k, v, max_elems: int | None):
    """scaled_dot_product_attention, optionally split over the batch dim so that one call never
    holds more than ``max_elems`` attention scores (MPS may materialise the full B*H*N*N matrix)."""
    if max_elems:
        b, h, n, _ = q.shape
        step = max(1, int(max_elems // (h * n * k.shape[-2])))
        if step < b:
            parts = [F.scaled_dot_product_attention(q[i:i + step], k[i:i + step], v[i:i + step])
                     for i in range(0, b, step)]
            return torch.cat(parts, dim=0)
    return F.scaled_dot_product_attention(q, k, v)


class Attention(nn.Module):
    def __init__(self, dim, heads=8, dim_head=64, rotary_embed=None):
        super().__init__()
        self.heads = heads
        inner = heads * dim_head
        self.rotary_embed = rotary_embed
        self.norm = RMSNorm(dim)
        self.to_qkv = nn.Linear(dim, inner * 3, bias=False)
        self.to_gates = nn.Linear(dim, heads)
        self.to_out = nn.Sequential(nn.Linear(inner, dim, bias=False), nn.Dropout(0.0))
        self.max_attn_elems: int | None = None

    def forward(self, x):
        x = self.norm(x)
        q, k, v = rearrange(self.to_qkv(x), "b n (qkv h d) -> qkv b h n d", qkv=3, h=self.heads)
        if self.rotary_embed is not None:
            q = self.rotary_embed.rotate_queries_or_keys(q)
            k = self.rotary_embed.rotate_queries_or_keys(k)
        out = _sdpa(q, k, v, self.max_attn_elems)
        out = out * rearrange(self.to_gates(x), "b n h -> b h n 1").sigmoid()
        return self.to_out(rearrange(out, "b h n d -> b n (h d)"))


class FeedForward(nn.Module):
    def __init__(self, dim, mult=4):
        super().__init__()
        inner = int(dim * mult)
        # indices must match MSST (net.0 norm, net.1 / net.4 linears)
        self.net = nn.Sequential(
            RMSNorm(dim), nn.Linear(dim, inner), nn.GELU(), nn.Dropout(0.0), nn.Linear(inner, dim), nn.Dropout(0.0)
        )

    def forward(self, x):
        return self.net(x)


class Transformer(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, rotary_embed, norm_output: bool):
        super().__init__()
        self.layers = nn.ModuleList(
            [nn.ModuleList([Attention(dim, heads, dim_head, rotary_embed), FeedForward(dim)]) for _ in range(depth)]
        )
        self.norm = RMSNorm(dim) if norm_output else nn.Identity()

    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return self.norm(x)


class BandSplit(nn.Module):
    def __init__(self, dim, dim_inputs: Sequence[int]):
        super().__init__()
        self.dim_inputs = tuple(dim_inputs)
        self.to_features = nn.ModuleList([nn.Sequential(RMSNorm(d), nn.Linear(d, dim)) for d in self.dim_inputs])

    def forward(self, x):
        xs = x.split(self.dim_inputs, dim=-1)
        return torch.stack([f(s) for s, f in zip(xs, self.to_features)], dim=-2)


def _mlp(dim_in, dim_out, dim_hidden, n_hidden):
    dims = (dim_in, *((dim_hidden,) * n_hidden), dim_out)
    layers = []
    for i, (a, b) in enumerate(zip(dims[:-1], dims[1:])):
        layers.append(nn.Linear(a, b))
        if i < len(dims) - 2:
            layers.append(nn.Tanh())
    return nn.Sequential(*layers)


class MaskEstimator(nn.Module):
    def __init__(self, dim, dim_inputs: Sequence[int], n_hidden: int, mlp_expansion_factor=4):
        super().__init__()
        self.dim_inputs = tuple(dim_inputs)
        hidden = dim * mlp_expansion_factor
        self.to_freqs = nn.ModuleList(
            [nn.Sequential(_mlp(dim, d * 2, hidden, n_hidden), nn.GLU(dim=-1)) for d in self.dim_inputs]
        )

    def forward(self, x):
        return torch.cat([mlp(t) for t, mlp in zip(x.unbind(dim=-2), self.to_freqs)], dim=-1)


def _cmul(a, b):
    """Complex product of real-view tensors (..., 2)."""
    ar, ai = a.unbind(-1)
    br, bi = b.unbind(-1)
    return torch.stack((ar * br - ai * bi, ar * bi + ai * br), dim=-1)


class _RoformerBase(nn.Module):
    """Shared STFT / axial-transformer plumbing."""

    def _init_common(self, *, dim, depth, stereo, num_stems, time_transformer_depth, freq_transformer_depth,
                     linear_transformer_depth, dim_head, heads, stft_n_fft, stft_hop_length, stft_win_length,
                     stft_normalized, zero_dc, skip_connection, use_pope, norm_output):
        if linear_transformer_depth:
            raise NotImplementedError("linear_transformer_depth > 0 is not supported by this minimal port")
        if use_pope:
            raise NotImplementedError("use_pope (PolarFormer) is not supported by this minimal port")
        self.stereo = stereo
        self.audio_channels = 2 if stereo else 1
        self.num_stems = num_stems
        self.skip_connection = skip_connection
        self.zero_dc = zero_dc
        self.win_length = stft_win_length
        self.stft_kwargs = dict(n_fft=stft_n_fft, hop_length=stft_hop_length, win_length=stft_win_length,
                                normalized=stft_normalized)
        time_rot, freq_rot = RotaryEmbedding(dim_head), RotaryEmbedding(dim_head)  # shared across layers, as in MSST
        self.layers = nn.ModuleList([
            nn.ModuleList([
                Transformer(dim, time_transformer_depth, heads, dim_head, time_rot, norm_output),
                Transformer(dim, freq_transformer_depth, heads, dim_head, freq_rot, norm_output),
            ])
            for _ in range(depth)
        ])
        # Device for STFT/iSTFT and mask application; None = same as the network.
        self.spec_device: torch.device | None = None

    def _net_device(self):
        return self.band_split.to_features[0][1].weight.device

    def _stft(self, audio):  # (b, s, t) -> real (b, f*s, frames, 2)   [MSST layout 'b (f s) t c']
        b, s, t = audio.shape
        window = torch.hann_window(self.win_length, device=audio.device)
        spec = torch.stft(audio.reshape(b * s, t), **self.stft_kwargs, window=window, return_complex=True)
        spec = torch.view_as_real(spec)
        spec = spec.reshape(b, s, *spec.shape[1:])
        return rearrange(spec, "b s f t c -> b (f s) t c")

    def _istft(self, spec, length):  # real (b, n, f*s, frames, 2) -> (b, n, s, t)
        b, n = spec.shape[:2]
        s = self.audio_channels
        spec = rearrange(spec, "b n (f s) t c -> (b n s) f t c", s=s).contiguous()
        if self.zero_dc:
            spec[:, 0] = 0.0
        window = torch.hann_window(self.win_length, device=spec.device)
        audio = torch.istft(torch.view_as_complex(spec), **self.stft_kwargs, window=window, return_complex=False,
                            length=length)
        return rearrange(audio, "(b n s) t -> b n s t", b=b, n=n, s=s)

    def _axial(self, x):  # (b, frames, bands, dim)
        store = []
        for time_t, freq_t in self.layers:
            if self.skip_connection:
                for prev in store:
                    x = x + prev
            x = rearrange(x, "b t f d -> b f t d")
            x, ps = pack([x], "* t d")
            x = unpack(time_t(x), ps, "* t d")[0]
            x = rearrange(x, "b f t d -> b t f d")
            x, ps = pack([x], "* f d")
            x = unpack(freq_t(x), ps, "* f d")[0]
            if self.skip_connection:
                store.append(x)
        return x


DEFAULT_FREQS_PER_BANDS = (
    (2,) * 24 + (4,) * 12 + (12,) * 8 + (24,) * 8 + (48,) * 8 + (128, 129)
)


class BSRoformer(_RoformerBase):
    def __init__(self, dim, *, depth, stereo=False, num_stems=1, time_transformer_depth=2, freq_transformer_depth=2,
                 linear_transformer_depth=0, freqs_per_bands: tuple[int, ...] = DEFAULT_FREQS_PER_BANDS, dim_head=64,
                 heads=8, stft_n_fft=2048, stft_hop_length=512, stft_win_length=2048, stft_normalized=False,
                 zero_dc=True, mask_estimator_depth=2, mlp_expansion_factor=4, skip_connection=False, use_pope=False,
                 **_training_only_kwargs):
        super().__init__()
        self._init_common(dim=dim, depth=depth, stereo=stereo, num_stems=num_stems,
                          time_transformer_depth=time_transformer_depth, freq_transformer_depth=freq_transformer_depth,
                          linear_transformer_depth=linear_transformer_depth, dim_head=dim_head, heads=heads,
                          stft_n_fft=stft_n_fft, stft_hop_length=stft_hop_length, stft_win_length=stft_win_length,
                          stft_normalized=stft_normalized, zero_dc=zero_dc, skip_connection=skip_connection,
                          use_pope=use_pope, norm_output=False)
        self.final_norm = RMSNorm(dim)
        freqs_per_bands = tuple(int(f) for f in freqs_per_bands)
        assert sum(freqs_per_bands) == stft_n_fft // 2 + 1, "freqs_per_bands must sum to n_fft//2+1"
        dims = tuple(2 * f * self.audio_channels for f in freqs_per_bands)
        self.band_split = BandSplit(dim, dims)
        # NB: in MSST's bs_roformer.py the mask MLP has (depth - 1) hidden layers
        self.mask_estimators = nn.ModuleList(
            [MaskEstimator(dim, dims, mask_estimator_depth - 1, mlp_expansion_factor) for _ in range(num_stems)]
        )

    def forward(self, raw_audio):  # (b, s, t) -> (b, n_stems, s, t)
        net_dev = self._net_device()
        spec_dev = self.spec_device or net_dev
        spec = self._stft(raw_audio.to(spec_dev))
        x = rearrange(spec, "b f t c -> b t (f c)").to(net_dev)
        x = self.final_norm(self._axial(self.band_split(x)))
        mask = torch.stack([m(x) for m in self.mask_estimators], dim=1)
        mask = rearrange(mask, "b n t (f c) -> b n f t c", c=2).to(spec_dev, torch.float32)
        return self._istft(_cmul(spec.unsqueeze(1), mask), raw_audio.shape[-1])


def mel_band_membership(sr: int, n_fft: int, n_mels: int) -> np.ndarray:
    """Boolean (n_mels, n_fft//2+1) = ``librosa.filters.mel(sr=sr, n_fft=n_fft, n_mels=n_mels) > 0``
    (Slaney mel scale, fmin=0, fmax=sr/2, float32 weights), with MSST's forced [0,0] / [-1,-1].
    Exact numpy port of librosa's computation, so no librosa/numba dependency is needed."""
    f_min, f_sp = 0.0, 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = np.log(6.4) / 27.0

    def hz_to_mel(f: float) -> float:
        f = np.asanyarray(f)
        mels = (f - f_min) / f_sp
        if f >= min_log_hz:
            mels = min_log_mel + np.log(f / min_log_hz) / logstep
        return mels

    def mel_to_hz(m: np.ndarray) -> np.ndarray:
        hz = f_min + f_sp * m
        log_t = m >= min_log_mel
        hz[log_t] = min_log_hz * np.exp(logstep * (m[log_t] - min_log_mel))
        return hz

    fftfreqs = np.fft.rfftfreq(n=n_fft, d=1.0 / sr)
    mel_f = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(float(sr) / 2), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fftfreqs)
    weights = np.zeros((n_mels, len(fftfreqs)), dtype=np.float32)
    for i in range(n_mels):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_f[2:n_mels + 2] - mel_f[:n_mels]))[:, None]  # slaney norm (kept for float32 parity)
    weights[0, 0] = 1.0
    weights[-1, -1] = 1.0
    return weights > 0


class MelBandRoformer(_RoformerBase):
    def __init__(self, dim, *, depth, stereo=False, num_stems=1, time_transformer_depth=2, freq_transformer_depth=2,
                 linear_transformer_depth=0, num_bands=60, dim_head=64, heads=8, sample_rate=44100, stft_n_fft=2048,
                 stft_hop_length=512, stft_win_length=2048, stft_normalized=False, zero_dc=True,
                 mask_estimator_depth=1, mlp_expansion_factor=4, skip_connection=False, use_pope=False,
                 **_training_only_kwargs):
        super().__init__()
        self._init_common(dim=dim, depth=depth, stereo=stereo, num_stems=num_stems,
                          time_transformer_depth=time_transformer_depth, freq_transformer_depth=freq_transformer_depth,
                          linear_transformer_depth=linear_transformer_depth, dim_head=dim_head, heads=heads,
                          stft_n_fft=stft_n_fft, stft_hop_length=stft_hop_length, stft_win_length=stft_win_length,
                          stft_normalized=stft_normalized, zero_dc=zero_dc, skip_connection=skip_connection,
                          use_pope=use_pope, norm_output=True)
        n_freqs = stft_n_fft // 2 + 1
        fb = torch.from_numpy(mel_band_membership(int(sample_rate), stft_n_fft, int(num_bands)))
        assert fb.shape[1] == n_freqs and bool(fb.any(dim=0).all()), "all frequencies must be covered"
        freq_indices = torch.arange(n_freqs).repeat(int(num_bands), 1)[fb]  # band-major, ascending freqs
        if stereo:
            freq_indices = (freq_indices[:, None] * 2 + torch.arange(2)).reshape(-1)
        self.register_buffer("freq_indices", freq_indices, persistent=False)
        self.register_buffer("num_bands_per_freq", fb.sum(dim=0), persistent=False)
        dims = tuple(2 * int(f) * self.audio_channels for f in fb.sum(dim=1).tolist())
        self.band_split = BandSplit(dim, dims)
        # NB: in MSST's mel_band_roformer.py the mask MLP has `depth` hidden layers (one more than BS-RoFormer)
        self.mask_estimators = nn.ModuleList(
            [MaskEstimator(dim, dims, mask_estimator_depth, mlp_expansion_factor) for _ in range(num_stems)]
        )

    def forward(self, raw_audio):  # (b, s, t) -> (b, n_stems, s, t)
        net_dev = self._net_device()
        spec_dev = self.spec_device or net_dev
        spec = self._stft(raw_audio.to(spec_dev))  # (b, F, frames, 2), F = freqs * channels
        idx = self.freq_indices.to(spec_dev)
        x = rearrange(spec[:, idx], "b f t c -> b t (f c)").to(net_dev)
        x = self._axial(self.band_split(x))
        masks = torch.stack([m(x) for m in self.mask_estimators], dim=1)
        masks = rearrange(masks, "b n t (f c) -> b n f t c", c=2).to(spec_dev, torch.float32)
        # average the masks of overlapping mel bands (real-valued scatter_add, no complex ops)
        b, n = masks.shape[:2]
        summed = torch.zeros((b, n) + tuple(spec.shape[1:]), device=spec_dev, dtype=torch.float32)
        summed.scatter_add_(2, idx.view(1, 1, -1, 1, 1).expand_as(masks), masks)
        denom = self.num_bands_per_freq.to(spec_dev).repeat_interleave(self.audio_channels).clamp(min=1)
        masks = summed / denom.to(torch.float32).view(1, 1, -1, 1, 1)
        return self._istft(_cmul(spec.unsqueeze(1), masks), raw_audio.shape[-1])


def build_model(cfg: dict, model_type: str | None = None) -> nn.Module:
    m = dict(cfg["model"])
    if model_type is None:
        model_type = "mel_band_roformer" if ("num_bands" in m and "freqs_per_bands" not in m) else "bs_roformer"
    cls = {"bs_roformer": BSRoformer, "mel_band_roformer": MelBandRoformer}[model_type]
    return cls(**m)


def load_checkpoint(model: nn.Module, path: str) -> None:
    sd = torch.load(path, map_location="cpu", weights_only=True)  # plain tensors only, no pickle code
    for key in ("state_dict", "model_state_dict", "state"):  # some trainers wrap the weights
        if isinstance(sd, dict) and isinstance(sd.get(key), dict):
            sd = sd[key]
    model.load_state_dict(sd, strict=True)  # fp16 checkpoints are upcast into the fp32 params


def default_device() -> torch.device:
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


# ============================================================================= overlap-add demix


def _window(chunk_size: int, fade: int) -> torch.Tensor:
    w = torch.ones(chunk_size)
    w[:fade] = torch.linspace(0, 1, fade)
    w[-fade:] = torch.linspace(1, 0, fade)
    return w


@torch.inference_mode()
def demix(model: nn.Module, mix: np.ndarray, chunk_size: int, num_overlap: int = 2, batch_size: int = 1,
          progress: Callable[[int, int], None] | None = None) -> np.ndarray:
    """MSST-style chunked inference: chunks of ``chunk_size`` every ``chunk_size // num_overlap``
    samples, linear fade-in/out of ``chunk_size // 10`` (none at the track edges), overlap-add with
    weight normalisation, reflect padding of ``chunk_size - step`` at both ends.
    mix: (channels, n) float32. Returns (n_stems, channels, n) float32."""
    device = model._net_device()
    mix_t = torch.as_tensor(np.ascontiguousarray(mix), dtype=torch.float32)
    channels, length = mix_t.shape
    step = chunk_size // num_overlap
    fade = chunk_size // 10
    border = chunk_size - step
    padded = length > 2 * border and border > 0
    if padded:
        mix_t = F.pad(mix_t[None], (border, border), mode="reflect")[0]
    total = mix_t.shape[-1]
    base_window = _window(chunk_size, fade)
    starts = list(range(0, total, step))
    out = torch.zeros((model.num_stems, channels, total), dtype=torch.float32)
    weight = torch.zeros(total, dtype=torch.float32)
    for b0 in range(0, len(starts), batch_size):
        batch_starts = starts[b0:b0 + batch_size]
        parts = []
        for s in batch_starts:
            part = mix_t[:, s:s + chunk_size]
            n = part.shape[-1]
            if n < chunk_size:
                mode = "reflect" if n > chunk_size // 2 else "constant"
                part = F.pad(part[None], (0, chunk_size - n), mode=mode)[0]
            parts.append(part)
        y = model(torch.stack(parts).to(device)).to("cpu", torch.float32)  # (B, n_stems, C, chunk)
        for j, s in enumerate(batch_starts):
            n = min(chunk_size, total - s)
            w = base_window.clone()
            if s == 0:
                w[:fade] = 1.0  # first chunk: no fade-in
            if s + step >= total:
                w[-fade:] = 1.0  # last chunk: no fade-out
            out[..., s:s + n] += y[j, ..., :n] * w[:n]
            weight[s:s + n] += w[:n]
        if progress is not None:
            progress(min(b0 + batch_size, len(starts)), len(starts))
    out /= weight.clamp(min=1e-8)
    if padded:
        out = out[..., border:-border]
    return torch.nan_to_num(out).numpy()
