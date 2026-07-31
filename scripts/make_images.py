"""Regenerate every app-store image and icon from the source art in docs/.

    python3 scripts/make_images.py

Two jobs:

1. Resize the reference photos (docs/app-image.png, docs/device-image-*.png) into the
   exact sizes the Homey App Store requires. App-store images are 10:7 landscape
   (250x175 / 500x350 / 1000x700); driver images are square (75 / 500 / 1000) on a
   white background, per guideline 1.4.

2. Draw the app + device icons as monochrome line-art SVGs. Homey renders icon.svg as
   a mask (it recolours the shapes), so these are transparent-background line drawings,
   the same convention as com.lomohome.localthings. Each device icon is traced from the
   real Navien hardware shown in the docs/ photos:
     - AirOne     — ceiling ERV box with duct ports
     - AirMonitor — upright fabric unit with a dot-matrix display and side tab
     - Mate       — quilted sleep mat with a crescent moon

Needs `sips` (macOS) for the raster resize and `rsvg-convert` only if you want to
preview the SVGs; the committed files are the SVGs and PNGs it writes.
"""

import subprocess
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
    # Three flowing air lines with a curl — "air" for the whole app (환기/제습/청정).
    return f'''  <g fill="none" stroke="{INK}" stroke-width="54"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M232 372 h366 a108 108 0 1 0 -108 -108"/>
    <path d="M198 496 h456 a110 110 0 1 1 -110 110"/>
    <path d="M232 620 h276 a92 92 0 1 1 -92 92"/>
  </g>
'''


def _airone_icon() -> str:
    return f'''  <g fill="none" stroke="{INK}" stroke-width="44"
     stroke-linecap="round" stroke-linejoin="round">
    <rect x="196" y="316" width="568" height="372" rx="46"/>
    <path d="M300 316 v-80 a34 34 0 0 1 34-34 h22 a34 34 0 0 1 34 34 v80"/>
    <path d="M572 316 v-80 a34 34 0 0 1 34-34 h22 a34 34 0 0 1 34 34 v80"/>
    <path d="M764 432 h80 a34 34 0 0 1 34 34 v22 a34 34 0 0 1 -34 34 h-80"/>
    <line x1="196" y1="474" x2="764" y2="474"/>
  </g>
  <g fill="{INK}">
    <circle cx="326" cy="586" r="30"/>
    <circle cx="634" cy="586" r="30"/>
  </g>
'''


def _airmonitor_icon() -> str:
    cx = 480
    cols = [cx + (c - 1.5) * 60 for c in range(4)]
    rows = [402 + r * 60 for r in range(3)]
    dots = "\n".join(
        f'    <circle cx="{x:.0f}" cy="{y:.0f}" r="17"/>' for y in rows for x in cols
    )
    return f'''  <g fill="none" stroke="{INK}" stroke-width="44"
     stroke-linecap="round" stroke-linejoin="round">
    <rect x="330" y="150" width="300" height="660" rx="118"/>
    <path d="M330 372 h-74 a26 26 0 0 0 -26 26 v92 a26 26 0 0 0 26 26 h74"/>
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
