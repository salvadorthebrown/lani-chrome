#!/usr/bin/env python3
"""Draws Lani's toolbar icons as PNGs, stdlib only.

  python3 tools/make_icons.py                     # hibiscus (default) into extension/icons
  python3 tools/make_icons.py --flower lily       # stargazer lily instead
  python3 tools/make_icons.py --flower lily --out /tmp/preview
"""
import argparse
import math
import pathlib
import struct
import zlib

DEFAULT_OUT = pathlib.Path(__file__).resolve().parent.parent / "extension" / "icons"
SAMPLES = 4  # supersampling per axis, for smooth edges
CENTER = 0.5


def lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def in_rounded_square(x, y, radius=0.22):
    cx = min(max(x, radius), 1 - radius)
    cy = min(max(y, radius), 1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def background(x, y):
    return lerp((30, 27, 75), (15, 90, 100), (x + y) / 2)  # deep indigo to teal


def dist_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def hibiscus(x, y):
    """Five rounded petals in magenta-to-pink, a dark throat, and a long yellow stamen."""
    dx, dy = x - CENTER, y - CENTER
    r = math.hypot(dx, dy)
    # Stamen first: it sits on top of the petals.
    tip = (0.66, 0.27)
    if dist_to_segment(x, y, CENTER, CENTER, *tip) <= 0.017:
        return (254, 240, 138)
    if math.hypot(x - tip[0], y - tip[1]) <= 0.045:
        return (250, 204, 21)
    for k in range(5):
        angle = math.radians(-90 + 72 * k)
        u = dx * math.cos(angle) + dy * math.sin(angle)
        v = -dx * math.sin(angle) + dy * math.cos(angle)
        if ((u - 0.185) / 0.185) ** 2 + (v / 0.135) ** 2 <= 1:
            if r < 0.075:
                return lerp((127, 29, 60), (190, 24, 93), r / 0.075)
            return lerp((219, 39, 119), (251, 146, 170), (r - 0.075) / 0.26)
    return None


def lily(x, y):
    """Six pointed, white-to-pink petals with a dark streak down each, like a stargazer lily."""
    dx, dy = x - CENTER, y - CENTER
    r = math.hypot(dx, dy)
    for k in range(6):
        angle = math.radians(-90 + 60 * k)
        u = dx * math.cos(angle) + dy * math.sin(angle)
        v = -dx * math.sin(angle) + dy * math.cos(angle)
        length = 0.37
        if 0 < u < length:
            half_width = 0.105 * math.sin(math.pi * u / length) ** 0.75
            if abs(v) <= half_width:
                if abs(v) < 0.011 and u < 0.30:
                    return (190, 24, 93)  # the streak down the middle of the petal
                return lerp((255, 244, 248), (236, 72, 153), u / length)
    if r < 0.05:
        return (250, 204, 21)
    return None


FLOWERS = {"hibiscus": hibiscus, "lily": lily}


def pixel(px, py, size, flower):
    r = g = b = covered = 0
    for sy in range(SAMPLES):
        for sx in range(SAMPLES):
            x = (px + (sx + 0.5) / SAMPLES) / size
            y = (py + (sy + 0.5) / SAMPLES) / size
            if not in_rounded_square(x, y):
                continue
            color = flower(x, y) or background(x, y)
            r, g, b, covered = r + color[0], g + color[1], b + color[2], covered + 1
    if covered == 0:
        return (0, 0, 0, 0)
    return (round(r / covered), round(g / covered), round(b / covered), round(255 * covered / SAMPLES ** 2))


def write_png(path, size, flower):
    rows = bytearray()
    for py in range(size):
        rows.append(0)  # filter type: none
        for px in range(size):
            rows.extend(pixel(px, py, size, flower))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flower", choices=sorted(FLOWERS), default="hibiscus")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        write_png(args.out / f"icon{size}.png", size, FLOWERS[args.flower])
    print(f"wrote {args.flower} icons to {args.out}")
