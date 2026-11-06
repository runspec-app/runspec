"""The runspec gate mark rendered as the unbranded app icon (brandmark.py)."""

from __future__ import annotations

import struct
import sys

import runspec_console.config as cfg
from runspec_console import brandmark


def _pixel(rgba: bytes, size: int, x: int, y: int) -> tuple[int, int, int, int]:
    i = (y * size + x) * 4
    return tuple(rgba[i : i + 4])  # type: ignore[return-value]


def test_render_has_transparent_corners_teal_tile_and_white_mark():
    size = 48
    rgba = brandmark.render_rgba(size)
    assert len(rgba) == size * size * 4
    # Rounded tile: the very corner is outside it.
    assert _pixel(rgba, size, 0, 0)[3] == 0
    # Inside the tile, away from the mark: solid deep teal.
    assert _pixel(rgba, size, 6, 24) == (*brandmark.TILE_RGB, 255)
    # On the gate bar and inside the triangle: white.
    assert _pixel(rgba, size, 15, 24) == (255, 255, 255, 255)
    assert _pixel(rgba, size, 26, 24) == (255, 255, 255, 255)
    # The gap between bar and triangle stays teal (the mark is two shapes).
    assert _pixel(rgba, size, 19, 24)[:3] == brandmark.TILE_RGB


def test_png_is_rgba_at_requested_size():
    png = brandmark.png_bytes(32)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, colour = struct.unpack(">IIBB", png[16:26])
    assert (width, height, depth, colour) == (32, 32, 8, 6)


def test_ico_holds_one_png_per_size():
    sizes = (16, 32, 48)
    ico = brandmark.ico_bytes(sizes)
    reserved, kind, count = struct.unpack("<HHH", ico[:6])
    assert (reserved, kind, count) == (0, 1, 3)
    for n, size in enumerate(sizes):
        w, h, _, _, _, bpp, length, offset = struct.unpack(
            "<BBBBHHII", ico[6 + 16 * n : 22 + 16 * n]
        )
        assert (w, h, bpp) == (size, size, 32)
        assert ico[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + length <= len(ico)


def _cfg_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)


def test_unbranded_console_gets_the_gate_mark(monkeypatch, tmp_path):
    from runspec_console import app

    monkeypatch.setattr(sys, "platform", "linux")
    _cfg_to_tmp(monkeypatch, tmp_path)
    icon = app._build_icon()
    assert icon is not None and icon.name == "runspec_console-gate-v1.png"
    assert icon.read_bytes() == brandmark.png_bytes(64)


def test_unbranded_console_on_windows_gets_a_multi_size_ico(monkeypatch, tmp_path):
    from runspec_console import app

    monkeypatch.setattr(sys, "platform", "win32")
    _cfg_to_tmp(monkeypatch, tmp_path)
    icon = app._build_icon()
    assert icon is not None and icon.name == "runspec_console-gate-v1.ico"
    assert struct.unpack("<HHH", icon.read_bytes()[:6]) == (0, 1, 4)


def test_white_label_accent_keeps_its_colour_square(monkeypatch, tmp_path):
    from runspec_console import app

    _cfg_to_tmp(monkeypatch, tmp_path)
    cfg.write_config({"console": {"theme": {"accent_color": "#b3001b"}}})
    icon = app._build_icon()
    assert icon is not None and "b3001b" in icon.name
