"""Build the FoxDash boot-screen assets from the Ember Deck boot set.

The Ember Deck Plymouth theme (EmberEngine/boot/plymouth/emberdeck in the
Ember Deck repository) is drawn for a 4:3 landscape screen and scales its
images at boot. FoxDash runs on a HyperPixel 4.0: the panel scans out
480x800 portrait, and the compositor rotates the desktop to 800x480
landscape. Plymouth draws on the raw panel, so it needs images that are
already rotated, and the Pi Zero 2 W should not scale six images at boot.

This script writes, from the unchanged Ember Deck art:

* deploy/raspberry-pi/boot/plymouth/foxdash/*.png
    stars and twinkle layers at 800x480 and the crescent and mask at the
    logo size, each in a landscape version and a ``-rot`` version rotated
    90 degrees clockwise for the portrait panel.
* deploy/raspberry-pi/boot/foxdash-boot-wallpaper.png
    the last frame of the boot animation at 800x480 landscape, shown by
    swaybg behind FoxDash so the hand-over from Plymouth has no desktop
    or black gap.

Run it on a machine with Pillow, for example:

    python tools/build_boot_assets.py --ember-deck H:/TheEmberDeck/The-Ember-Deck
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

WIDTH, HEIGHT = 800, 480          # logical landscape dashboard
LOGO_FRACTION = 0.55              # same as emberdeck.script
NIGHT = (0x0B, 0x0B, 0x0F, 255)   # Ember Deck night background
WALLPAPER_TWINKLE_OPACITY = 0.55  # mid point of the twinkle pulse

BACKGROUND_LAYERS = ("stars", "twinkle_far", "twinkle_mid", "twinkle_near")
LOGO_LAYERS = ("crescent", "mask")

REPO_ROOT = Path(__file__).resolve().parents[1]
THEME_DIR = REPO_ROOT / "deploy" / "raspberry-pi" / "boot" / "plymouth" / "foxdash"
WALLPAPER = REPO_ROOT / "deploy" / "raspberry-pi" / "boot" / "foxdash-boot-wallpaper.png"


def cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Centre-crop to the target aspect ratio, then resize (no distortion)."""
    src_w, src_h = image.size
    target = width / height
    if src_w / src_h > target:
        crop_w = round(src_h * target)
        left = (src_w - crop_w) // 2
        image = image.crop((left, 0, left + crop_w, src_h))
    else:
        crop_h = round(src_w / target)
        top = (src_h - crop_h) // 2
        image = image.crop((0, top, src_w, top + crop_h))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def with_opacity(image: Image.Image, opacity: float) -> Image.Image:
    alpha = image.getchannel("A").point(lambda a: round(a * opacity))
    faded = image.copy()
    faded.putalpha(alpha)
    return faded


def save_pair(image: Image.Image, name: str) -> None:
    image.save(THEME_DIR / f"{name}.png", optimize=True)
    # Plymouth draws on the unrotated portrait panel. Logical top-left maps
    # to the panel's top-right corner, so the art turns 90 degrees clockwise.
    image.transpose(Image.Transpose.ROTATE_270).save(THEME_DIR / f"{name}-rot.png", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ember-deck", required=True, type=Path, help="Ember Deck repository root")
    args = parser.parse_args()

    source = args.ember_deck / "EmberEngine" / "boot" / "plymouth" / "emberdeck"
    if not source.is_dir():
        raise SystemExit(f"Ember Deck theme not found: {source}")
    THEME_DIR.mkdir(parents=True, exist_ok=True)

    logo_size = int(min(WIDTH, HEIGHT) * LOGO_FRACTION)
    layers: dict[str, Image.Image] = {}
    for name in BACKGROUND_LAYERS:
        layers[name] = cover(Image.open(source / f"{name}.png").convert("RGBA"), WIDTH, HEIGHT)
        save_pair(layers[name], name)
    for name in LOGO_LAYERS:
        layers[name] = Image.open(source / f"{name}.png").convert("RGBA").resize(
            (logo_size, logo_size), Image.Resampling.LANCZOS
        )
        save_pair(layers[name], name)

    frame = Image.new("RGBA", (WIDTH, HEIGHT), NIGHT)
    frame.alpha_composite(layers["stars"])
    for name in BACKGROUND_LAYERS[1:]:
        frame.alpha_composite(with_opacity(layers[name], WALLPAPER_TWINKLE_OPACITY))
    logo_pos = ((WIDTH - logo_size) // 2, (HEIGHT - logo_size) // 2)
    for name in LOGO_LAYERS:
        frame.alpha_composite(layers[name], logo_pos)
    frame.convert("RGB").save(WALLPAPER, optimize=True)

    print(f"logo {logo_size}px; wrote {THEME_DIR} and {WALLPAPER}")


if __name__ == "__main__":
    main()
