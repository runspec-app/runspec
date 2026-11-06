"""The runspec gate mark, drawn as pixels for the app icon.

The mark is a run triangle behind a vertical gate bar ("G1" in the brand work):
the console can run tools fast, but only through a gate you control. As an app
icon it sits knocked out in white on a rounded deep-teal tile.

Pure stdlib: shapes are rasterised with a small supersampling coverage test (no
Pillow), then wrapped as an RGBA PNG and, for Windows, a multi-size ICO. Used by
``app._build_icon`` only when no white-label ``accent_color`` / ``icon`` is set —
a branded console keeps its own colour square or icon.
"""

from __future__ import annotations

import math
import struct
import zlib

# Brand colours (see docs/design/brand.md).
TILE_RGB = (0x0E, 0x8F, 0x80)  # deep teal: icon tile
MARK_RGB = (0xFF, 0xFF, 0xFF)  # the mark, knocked out in white

# Geometry in a 48×48 design space (the SVG viewBox the brand board uses).
_TILE_RADIUS = 10.0
_BAR = ((15.6, 14.2), (15.6, 33.8))  # gate bar centre line, round caps
_BAR_HALF_WIDTH = 2.8
_TRIANGLE = ((21.4, 15.0), (34.8, 24.0), (21.4, 33.0))  # run triangle
_TRIANGLE_ROUND = 1.3  # rounded joins: the triangle grown by this radius

_SUPERSAMPLE = 4


def _dist_to_segment(px: float, py: float, a: tuple, b: tuple) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _in_triangle(px: float, py: float) -> bool:
    (x1, y1), (x2, y2), (x3, y3) = _TRIANGLE
    d1 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)
    d2 = (px - x3) * (y2 - y3) - (x2 - x3) * (py - y3)
    d3 = (px - x1) * (y3 - y1) - (x3 - x1) * (py - y1)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def _in_mark(px: float, py: float) -> bool:
    if _dist_to_segment(px, py, *_BAR) <= _BAR_HALF_WIDTH:
        return True
    if _in_triangle(px, py):
        return True
    t = _TRIANGLE
    return any(
        _dist_to_segment(px, py, t[i], t[(i + 1) % 3]) <= _TRIANGLE_ROUND
        for i in range(3)
    )


def _in_tile(px: float, py: float) -> bool:
    r = _TILE_RADIUS
    cx = min(max(px, r), 48.0 - r)
    cy = min(max(py, r), 48.0 - r)
    return math.hypot(px - cx, py - cy) <= r and 0.0 <= px <= 48.0 and 0.0 <= py <= 48.0


def render_rgba(size: int) -> bytes:
    """The icon at ``size``×``size`` as raw RGBA rows (top to bottom), with an
    anti-aliased tile edge (transparent corners) and an anti-aliased mark."""
    n = _SUPERSAMPLE
    scale = 48.0 / size
    out = bytearray()
    for y in range(size):
        for x in range(size):
            tile = mark = 0
            for sy in range(n):
                for sx in range(n):
                    px = (x + (sx + 0.5) / n) * scale
                    py = (y + (sy + 0.5) / n) * scale
                    if _in_tile(px, py):
                        tile += 1
                        if _in_mark(px, py):
                            mark += 1
            if tile == 0:
                out += b"\x00\x00\x00\x00"
                continue
            m = mark / tile  # share of the tile's coverage that is mark
            rgb = (round(t + (k - t) * m) for t, k in zip(TILE_RGB, MARK_RGB))
            out += bytes(rgb) + bytes([round(255 * tile / (n * n))])
    return bytes(out)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def png_bytes(size: int) -> bytes:
    """The icon as a 32-bit RGBA PNG (colour type 6)."""
    rgba = render_rgba(size)
    row = size * 4
    scanlines = b"".join(b"\x00" + rgba[i * row : (i + 1) * row] for i in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def ico_bytes(sizes: tuple[int, ...] = (16, 32, 48, 64)) -> bytes:
    """A Windows ``.ico`` holding one PNG-compressed RGBA image per size, so the
    taskbar, title bar and Alt+Tab each pick a sharp one."""
    images = [png_bytes(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, img in zip(sizes, images):
        dim = 0 if s >= 256 else s  # 0 means 256 in an ICO directory entry
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    return header + entries + b"".join(images)
