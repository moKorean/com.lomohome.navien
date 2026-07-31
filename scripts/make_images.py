"""Regenerate every app-store image and icon from the source art in docs/.

    python3 scripts/make_images.py

Two jobs:

1. Resize the reference photos (docs/app-image.png, docs/device-image-*.png) into the
   exact sizes the Homey App Store requires. App-store images are 10:7 landscape
   (250x175 / 500x350 / 1000x700); driver images are square (75 / 500 / 1000) on a
   white background, per guideline 1.4.

2. Draw the icons as monochrome SVGs. Homey renders icon.svg as a mask (it recolours the
   shapes), so these are transparent-background monochrome art that works on any tint.
     - App        — the Navien wordmark, vector-traced from docs/Navien_CI.png
     - AirOne     — ceiling ERV box with duct ports
     - AirMonitor — upright fabric unit with a dot-matrix display and side tab
     - Mate       — quilted sleep mat with a crescent moon
   The device icons are drawn from the real Navien hardware shown in the docs/ photos.

Needs `sips` (macOS) for the raster resize. The app icon also needs `rsvg-convert` and
`potrace` (brew install potrace) to vectorise the logo; the device icons are pure SVG.
The committed files are the SVGs and PNGs it writes.
"""

import base64
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# --- 1. Raster store images -----------------------------------------------------

APP_SIZES = {"small": (250, 175), "large": (500, 350), "xlarge": (1000, 700)}
DEVICE_SIZES = {"small": 75, "large": 500, "xlarge": 1000}
# driver id -> source photo
DEVICE_SOURCES = {
    "airone": "device-image-airone.png",
    "mate": "device-image-mat.png",
    "airmonitor": "device-image-monitor.png",
}


def _sips(src: Path, w: int, h: int, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["sips", "-s", "format", "png", "-z", str(h), str(w), str(src), "--out", str(dst)],
        check=True, stdout=subprocess.DEVNULL,
    )
    print(f"  {dst.relative_to(ROOT)}  {w}x{h}")


def resize_images() -> None:
    print("app store images:")
    for name, (w, h) in APP_SIZES.items():
        _sips(DOCS / "app-image.png", w, h, ROOT / "assets/images" / f"{name}.png")
    print("device images:")
    for driver, source in DEVICE_SOURCES.items():
        for name, size in DEVICE_SIZES.items():
            _sips(DOCS / source, size, size,
                  ROOT / "drivers" / driver / "assets/images" / f"{name}.png")


# --- 2. Icons -------------------------------------------------------------------

HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 960" '
        'width="960" height="960">\n')
INK = "#111111"


def _write(path: Path, body: str) -> None:
    path.write_text(HEAD + body + "</svg>\n")
    print(f"  {path.relative_to(ROOT)}")


def _app_icon() -> str:
    # The Navien wordmark, vector-traced from the CI logo so it stays crisp at any size
    # and, being monochrome, reads on any background Homey tints it with.
    logo = DOCS / "Navien_CI.png"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 1. Flatten the transparent logo onto white at 4x for a clean trace.
        b64 = base64.b64encode(logo.read_bytes()).decode()
        wrap = tmp / "wrap.svg"
        wrap.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="528" height="136" '
            'viewBox="0 0 528 136"><rect width="528" height="136" fill="#fff"/>'
            f'<image width="528" height="136" href="data:image/png;base64,{b64}"/></svg>'
        )
        white = tmp / "white.png"
        subprocess.run(["rsvg-convert", "-w", "2112", "-h", "544", str(wrap),
                        "-o", str(white)], check=True)
        bmp = tmp / "white.bmp"
        subprocess.run(["sips", "-s", "format", "bmp", str(white), "--out", str(bmp)],
                       check=True, stdout=subprocess.DEVNULL)
        traced = tmp / "trace.svg"
        subprocess.run(["potrace", str(bmp), "-s", "-o", str(traced),
                        "-k", "0.55", "--tight"], check=True)
        svg = traced.read_text()
    vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    lw, lh = float(vb.group(1)), float(vb.group(2))
    group = re.search(r"(<g\b.*?</g>)", svg, re.S).group(1)
    group = re.sub(r'fill="#[0-9a-fA-F]+"', f'fill="{INK}"', group, count=1)
    target_w = 950.0
    s = target_w / lw
    tx, ty = (960 - target_w) / 2, (960 - lh * s) / 2
    return (f'  <g transform="translate({tx:.2f} {ty:.2f}) scale({s:.6f})">\n'
            f'    {group}\n  </g>\n')


def _airone_icon() -> str:
    # ERV box in an oblique (cabinet) projection — a slight side view, per the Homey
    # icon guideline. Front + top + right faces, two duct ports rising from the top.
    x0, y0, w, h = 150, 400, 500, 360
    x1, y1 = x0 + w, y0 + h
    dx, dy = 150, 110
    ducts = ""
    for cx in (x0 + 160, x0 + 360):
        by = y0 - dy + 60
        ducts += (f'\n    <ellipse cx="{cx+70}" cy="{by-90}" rx="46" ry="17"/>'
                  f'\n    <path d="M{cx+24} {by-90} V{by-20}"/>'
                  f'\n    <path d="M{cx+116} {by-90} V{by-20}"/>')
    return f'''  <g fill="none" stroke="{INK}" stroke-width="34"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M{x0} {y0} H{x1} V{y1} H{x0} Z"/>
    <path d="M{x0} {y0} L{x0+dx} {y0-dy} H{x1+dx} L{x1} {y0}"/>
    <path d="M{x1} {y0} L{x1+dx} {y0-dy} V{y1-dy} L{x1} {y1}"/>
    <line x1="{x0}" y1="{y0+150}" x2="{x1}" y2="{y0+150}"/>
  </g>
  <g fill="{INK}">
    <circle cx="{x0+120}" cy="{y0+255}" r="24"/>
    <circle cx="{x1-120}" cy="{y0+255}" r="24"/>
  </g>
  <g fill="none" stroke="{INK}" stroke-width="30"
     stroke-linecap="round" stroke-linejoin="round">{ducts}
  </g>
'''


def _airmonitor_icon() -> str:
    # Upright fabric unit with a slight side view (oblique top + right faces), a
    # dot-matrix display, and a small tag on the left edge.
    x, y, w, h, rx = 330, 210, 250, 560, 92
    dx, dy = 64, 46
    cx = x + w / 2
    dots = "\n".join(
        f'    <circle cx="{cx + (c-1.5)*50:.0f}" cy="{y+220 + r*50}" r="14"/>'
        for r in range(3) for c in range(4)
    )
    return f'''  <g fill="none" stroke="{INK}" stroke-width="34"
     stroke-linecap="round" stroke-linejoin="round">
    <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}"/>
    <path d="M{x+rx} {y} L{x+rx+dx} {y-dy} H{x+w-rx+dx} L{x+w-rx} {y}"/>
    <path d="M{x+w} {y+rx} L{x+w+dx} {y+rx-dy} V{y+h-rx-dy} L{x+w} {y+h-rx}"/>
    <path d="M{x} {y+248} h-30 a16 16 0 0 0 -16 16 v60 a16 16 0 0 0 16 16 h30"/>
  </g>
  <g fill="{INK}">
{dots}
  </g>
'''


def _mate_icon() -> str:
    px0, py0, pw, ph, rx = 168, 512, 624, 226, 56
    px1, py1 = px0 + pw, py0 + ph
    cx0, cy0, cx1, cy1 = px0 + 14, py0 + 14, px1 - 14, py1 - 14

    def clip(x, y):
        return max(cx0, min(cx1, x)), max(cy0, min(cy1, y))

    lines = []
    for k in range(cx0 - cy1, cx1 - cy0 + 1, 118):          # "\" diagonals
        a, b = clip(k + cy0, cy0), clip(k + cy1, cy1)
        if a != b:
            lines.append(f'    <line x1="{a[0]:.0f}" y1="{a[1]:.0f}" '
                         f'x2="{b[0]:.0f}" y2="{b[1]:.0f}"/>')
    for k in range(cx0 + cy0, cx1 + cy1 + 1, 118):          # "/" diagonals
        a, b = clip(k - cy0, cy0), clip(k - cy1, cy1)
        if a != b:
            lines.append(f'    <line x1="{a[0]:.0f}" y1="{a[1]:.0f}" '
                         f'x2="{b[0]:.0f}" y2="{b[1]:.0f}"/>')
    quilt = "\n".join(lines)
    return f'''  <defs>
    <clipPath id="pad">
      <rect x="{px0}" y="{py0}" width="{pw}" height="{ph}" rx="{rx}"/>
    </clipPath>
  </defs>
  <path d="M516 250 a96 96 0 1 0 0 168 a74 74 0 1 1 0 -168 z" fill="{INK}"/>
  <g fill="none" stroke="{INK}" stroke-linecap="round" stroke-linejoin="round">
    <rect x="{px0}" y="{py0}" width="{pw}" height="{ph}" rx="{rx}" stroke-width="42"/>
    <g clip-path="url(#pad)" stroke-width="18">
{quilt}
    </g>
  </g>
'''


def make_icons() -> None:
    print("icons:")
    _write(ROOT / "assets/icon.svg", _app_icon())
    _write(ROOT / "drivers/airone/assets/icon.svg", _airone_icon())
    _write(ROOT / "drivers/airmonitor/assets/icon.svg", _airmonitor_icon())
    _write(ROOT / "drivers/mate/assets/icon.svg", _mate_icon())


if __name__ == "__main__":
    resize_images()
    make_icons()
    print("done")
