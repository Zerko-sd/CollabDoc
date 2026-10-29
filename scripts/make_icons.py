"""Draw the app icons (web/icons/icon-192.png, icon-512.png) without Pillow.

A dark rounded square with a white "C" ring -- the brand mark -- rendered
with 4x4 supersampling and written as an RGBA PNG by hand (zlib + CRC).

    python scripts/make_icons.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "web" / "icons"
INK = (55, 53, 47)
PAPER = (255, 255, 255)


def coverage(x: float, y: float, size: int) -> tuple[float, float]:
    """(inside the rounded square, inside the C) for one sample point."""
    radius = size * 0.22
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    square = (x - cx) ** 2 + (y - cy) ** 2 <= radius**2
    dx, dy = x - size / 2, y - size / 2
    distance = math.hypot(dx, dy)
    angle = math.degrees(math.atan2(dy, dx))
    ring = size * 0.19 <= distance <= size * 0.31 and abs(angle) > 42
    return square, ring


def draw(size: int) -> bytes:
    rows = []
    samples = 4
    for py in range(size):
        row = bytearray([0])  # filter: none
        for px in range(size):
            inside = letter = 0
            for sy in range(samples):
                for sx in range(samples):
                    square, ring = coverage(px + (sx + 0.5) / samples, py + (sy + 0.5) / samples, size)
                    inside += square
                    letter += square and ring
            total = samples * samples
            alpha = inside / total
            mix = letter / inside if inside else 0
            color = [round(INK[i] * (1 - mix) + PAPER[i] * mix) for i in range(3)]
            row += bytes(color + [round(alpha * 255)])
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        (OUT / ("icon-%d.png" % size)).write_bytes(draw(size))
        print("wrote", OUT / ("icon-%d.png" % size))
