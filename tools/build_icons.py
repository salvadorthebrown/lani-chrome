#!/usr/bin/env python3
"""Cuts Lani's source art into the four Chrome icon sizes, with rounded corners.

  python3 tools/build_icons.py                          # art/lani-lily.png -> extension/icons
  python3 tools/build_icons.py --src art/lani-hibiscus.png
The source art is made with tools/flux_icon.py (Flux schnell through ComfyUI).
"""
import argparse
import pathlib

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parent.parent
SIZES = (16, 32, 48, 128)
CORNER = 0.22  # corner radius as a fraction of the icon size


def rounded_mask(size):
    """A rounded-square alpha mask, drawn at 8x and shrunk so the edges are smooth."""
    big = size * 8
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * CORNER), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def build(src, out):
    art = Image.open(src).convert("RGB")
    side = min(art.size)
    art = art.crop(((art.width - side) // 2, (art.height - side) // 2, (art.width + side) // 2, (art.height + side) // 2))
    out.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        icon = art.resize((size, size), Image.LANCZOS).convert("RGBA")
        icon.putalpha(rounded_mask(size))
        icon.save(out / f"icon{size}.png", optimize=True)
        print(f"wrote icon{size}.png")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=pathlib.Path, default=ROOT / "art" / "lani-lily.png")
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "extension" / "icons")
    args = ap.parse_args()
    build(args.src, args.out)
