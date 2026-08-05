"""
test_icon_gtk_loadable.py — the faithful reproduction of the shipped Linux icon
crash, run against the *real* gdk-pixbuf loader.

pywebview's GTK backend hands the window icon straight to
`Gtk.Window.set_icon_from_file()`, which decodes it with gdk-pixbuf. The bug: the
generated icon was always a PNG-compressed `.ico`, and gdk-pixbuf's ICO loader
rejects that with "Compressed icons are not supported" — an exception that aborts
window creation and leaves a blank stub window.

These tests call `GdkPixbuf.Pixbuf.new_from_file()` — the exact decode
`set_icon_from_file()` performs — so they catch a regression at the real boundary,
not a mock. They require the GdkPixbuf typelib (python3-gi + gir1.2-gdkpixbuf-2.0)
and are skipped where it isn't installed (so the normal test job / dev machines
stay green); CI's `console-app-smoke` job installs it and runs them.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402


def _cfg_to_tmp(monkeypatch, tmp_path):
    import runspec_console.config as cfg

    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)
    cfg.write_config({"console": {"theme": {"accent_color": "#0958d9"}}})


def test_generated_icon_loads_through_gdkpixbuf(monkeypatch, tmp_path):
    """The icon this platform actually ships must decode with gdk-pixbuf — i.e.
    the exact loader GTK uses — with the expected dimensions."""
    from runspec_console import app

    _cfg_to_tmp(monkeypatch, tmp_path)
    icon = app._build_icon()
    assert icon is not None

    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(icon))
    assert pixbuf.get_width() == 32
    assert pixbuf.get_height() == 32


def test_windows_style_png_ico_is_rejected_by_gdkpixbuf(monkeypatch, tmp_path):
    """Document the root cause: the Windows-style PNG-compressed `.ico` — what
    every platform used to get — genuinely fails in gdk-pixbuf. This is why the
    non-Windows path must emit a bare `.png`. If gdk-pixbuf ever starts accepting
    it, this test flips and we can simplify; until then it proves the fix is
    load-bearing."""
    import sys

    from runspec_console import app

    _cfg_to_tmp(monkeypatch, tmp_path)
    # Force the Windows branch so _build_icon emits the PNG-compressed .ico that
    # broke GTK, even though we're running on Linux.
    monkeypatch.setattr(sys, "platform", "win32")
    ico = app._build_icon()
    assert ico is not None and ico.suffix == ".ico"

    # This is documentation of the root cause, not the guard — the guard is the
    # test above ("the icon we actually ship loads"). If some future gdk-pixbuf
    # gains PNG-in-ICO support the .ico stops being a problem *and* our .png fix
    # stays correct, so skip rather than false-red the build.
    try:
        GdkPixbuf.Pixbuf.new_from_file(str(ico))
    except GLib.Error:
        return  # expected: gdk-pixbuf rejects the PNG-compressed .ico
    pytest.skip(
        "gdk-pixbuf on this system accepts a PNG-compressed .ico; the "
        ".png fix remains correct regardless"
    )
