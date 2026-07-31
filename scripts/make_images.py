"""Regenerate the app-store / driver images from the source photos in docs/.

    python3 scripts/make_images.py

Resizes the reference photos (docs/app-image.png, docs/device-image-*.png) into the
exact sizes the Homey App Store requires: app-store images are 10:7 landscape
(250x175 / 500x350 / 1000x700); driver images are square (75 / 500 / 1000) on a white
background, per guideline 1.4. Needs `sips` (macOS).

Icons (assets/icon.svg and drivers/*/assets/icon.svg) are maintained by hand — this
script deliberately does NOT touch them, so it can't overwrite hand-drawn artwork.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

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


if __name__ == "__main__":
    resize_images()
    print("done")
