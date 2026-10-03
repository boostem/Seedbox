"""Generate the Seedbox app icon: SVG → PNGs (headless Chromium) → Seedbox.icns (iconutil).

Motif: a dark vinyl record whose grooves carry a circular waveform (lime → aqua → violet) around a glowing
sprouting seed on the label. Sizes <= 64 px use a simplified drawing (fewer, thicker bars, no grooves).

    .venv/bin/python packaging/icon/make_icon.py            # writes packaging/icon/{seedbox.svg,
                                                            #   seedbox-small.svg, seedbox-*.png, Seedbox.icns}
Env CHROME overrides the headless Chromium binary.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHROME = os.environ.get("CHROME") or str(
    Path.home() / "Library/Caches/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-mac-arm64"
    / "chrome-headless-shell")
ICONSET = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]
HUE_TOP, HUE_BOTTOM = 80.0, 268.0  # lime at 12 o'clock → aqua at 3/9 → violet at 6 (no seam, no grey)
C = 512  # canvas centre


def _lerp_color(t: float) -> str:
    import colorsys

    hue = HUE_TOP + (HUE_BOTTOM - HUE_TOP) * (1 - math.cos(2 * math.pi * t)) / 2
    r, g, b = colorsys.hsv_to_rgb(hue / 360, 0.76, 1.0)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _wave(t: float) -> float:
    """Smooth pseudo-waveform 0..1 around the circle (a few phrase-like swells)."""
    v = (0.55 + 0.25 * math.sin(2 * math.pi * 3 * t + 0.6) + 0.15 * math.sin(2 * math.pi * 7 * t + 1.9)
         + 0.12 * math.sin(2 * math.pi * 13 * t + 0.3) + 0.08 * math.sin(2 * math.pi * 29 * t))
    return max(0.08, min(1.0, v))


def svg(small: bool = False) -> str:
    bars = 56 if small else 112
    r_in, r_span = (176, 118) if small else (176, 122)
    width = 15 if small else 7.5
    parts = []
    for i in range(bars):
        t = i / bars
        a = 2 * math.pi * t - math.pi / 2
        r0, r1 = r_in, r_in + 14 + r_span * _wave(t)
        x0, y0, x1, y1 = C + r0 * math.cos(a), C + r0 * math.sin(a), C + r1 * math.cos(a), C + r1 * math.sin(a)
        parts.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke="{_lerp_color(t)}"/>')
    wave = "\n      ".join(parts)
    grooves = "" if small else "\n    ".join(
        f'<circle cx="{C}" cy="{C}" r="{r}" fill="none" stroke="#ffffff" stroke-opacity="{0.035 + 0.02 * (i % 3 == 0):.3f}" '
        f'stroke-width="1.2"/>' for i, r in enumerate(range(150, 336, 7)))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs>
    <radialGradient id="bg" cx="38%" cy="28%" r="85%">
      <stop offset="0" stop-color="#232833"/><stop offset="0.55" stop-color="#12151b"/>
      <stop offset="1" stop-color="#07080b"/>
    </radialGradient>
    <radialGradient id="glow" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#7cf0c8" stop-opacity="0.13"/><stop offset="1" stop-color="#7cf0c8" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="disc" cx="50%" cy="50%" r="50%">
      <stop offset="0.45" stop-color="#15181e"/><stop offset="0.93" stop-color="#0b0c10"/>
      <stop offset="1" stop-color="#050608"/>
    </radialGradient>
    <linearGradient id="sheen" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.16"/><stop offset="0.45" stop-color="#ffffff" stop-opacity="0"/>
      <stop offset="0.55" stop-color="#ffffff" stop-opacity="0"/><stop offset="1" stop-color="#ffffff" stop-opacity="0.10"/>
    </linearGradient>
    <radialGradient id="label" cx="42%" cy="35%" r="70%">
      <stop offset="0" stop-color="#1b2016"/><stop offset="1" stop-color="#0a0c08"/>
    </radialGradient>
    <linearGradient id="seed" x1="0" y1="0" x2="0.4" y2="1">
      <stop offset="0" stop-color="#e9ff8a"/><stop offset="0.5" stop-color="#c6ff3d"/><stop offset="1" stop-color="#4fe39a"/>
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="14" stdDeviation="16" flood-color="#000" flood-opacity="0.45"/>
    </filter>
    <filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="10"/></filter>
    <clipPath id="body"><rect x="100" y="100" width="824" height="824" rx="186"/></clipPath>
  </defs>
  <rect x="100" y="100" width="824" height="824" rx="186" fill="url(#bg)" filter="url(#shadow)"/>
  <g clip-path="url(#body)">
    <circle cx="300" cy="250" r="420" fill="url(#glow)"/>
    <circle cx="{C}" cy="{C}" r="344" fill="url(#disc)"/>
    {grooves}
    <circle cx="{C}" cy="{C}" r="344" fill="url(#sheen)"/>
    <circle cx="{C}" cy="{C}" r="343" fill="none" stroke="#ffffff" stroke-opacity="0.10" stroke-width="2"/>
    <g stroke-linecap="round" stroke-width="{width}" opacity="0.55" filter="url(#soft)">
      {wave}
    </g>
    <g stroke-linecap="round" stroke-width="{width}">
      {wave}
    </g>
    <circle cx="{C}" cy="{C}" r="150" fill="url(#label)"/>
    <circle cx="{C}" cy="{C}" r="150" fill="none" stroke="#c6ff3d" stroke-opacity="0.85" stroke-width="{8 if small else 5}"/>
    <g transform="translate({C} {C + 22}) rotate(-18)">
      <path d="M0,-62 C46,-34 54,34 0,76 C-54,34 -46,-34 0,-62 Z" fill="url(#seed)"/>
      <path d="M0,-50 C22,-20 24,30 0,62" fill="none" stroke="#0b0d10" stroke-opacity="0.35" stroke-width="5"
            stroke-linecap="round"/>
      <path d="M0,-60 C-2,-82 6,-100 22,-112" fill="none" stroke="#c6ff3d" stroke-width="9" stroke-linecap="round"/>
      <path d="M20,-110 C40,-136 78,-132 92,-118 C70,-100 40,-98 20,-110 Z" fill="#4fe39a"/>
      <path d="M14,-104 C-6,-128 -42,-126 -54,-110 C-32,-94 -6,-94 14,-104 Z" fill="#c6ff3d"/>
    </g>
  </g>
</svg>
"""


def render(svg_path: Path, png_path: Path, size: int) -> None:
    html = png_path.with_suffix(".html")
    html.write_text(f"<!doctype html><html><body style='margin:0;background:transparent'>"
                    f"<img src='{svg_path.as_uri()}' width='{size}' height='{size}' style='display:block'>"
                    f"</body></html>", encoding="utf-8")
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    f"--window-size={size},{size}", "--default-background-color=00000000",
                    "--allow-file-access-from-files", f"--screenshot={png_path}", html.as_uri()],
                   check=True, capture_output=True, timeout=60)
    html.unlink()


def main() -> int:
    if not Path(CHROME).exists():
        print(f"headless Chromium not found at {CHROME} (set CHROME=...)", file=sys.stderr)
        return 1
    big, small = HERE / "seedbox.svg", HERE / "seedbox-small.svg"
    big.write_text(svg(), encoding="utf-8")
    small.write_text(svg(small=True), encoding="utf-8")
    master, master_small = HERE / "seedbox-1024.png", HERE / "seedbox-small-256.png"
    render(big, master, 1024)
    render(small, master_small, 256)
    shutil.copy(master, HERE / "seedbox-512.png")
    subprocess.run(["sips", "-z", "512", "512", str(HERE / "seedbox-512.png")], check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Seedbox.iconset"
        iconset.mkdir()
        for base, scale in ICONSET:
            px = base * scale
            out = iconset / (f"icon_{base}x{base}" + ("@2x" if scale == 2 else "") + ".png")
            shutil.copy(master_small if px <= 64 else master, out)
            subprocess.run(["sips", "-z", str(px), str(px), str(out)], check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "Seedbox.icns")], check=True)
    master_small.unlink()
    print(f"wrote {HERE / 'Seedbox.icns'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
