"""
Tests for the console-UI build hook's staleness check (hatch_build.ui_is_stale).

The hook itself drives npm and needs the hatchling build context, but the
decision of *whether* to rebuild is a pure mtime comparison we can pin down: a
missing bundle is stale, a bundle older than the UI source is stale, and a
freshly built one is not.
"""

from __future__ import annotations

import os

from hatch_build import ui_is_stale


def _touch(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_missing_bundle_is_stale(tmp_path):
    ui = tmp_path / "console-ui"
    (ui / "src").mkdir(parents=True)
    index = tmp_path / "pkg" / "dist" / "index.html"  # never built
    assert ui_is_stale(ui, index) is True


def test_bundle_older_than_source_is_stale(tmp_path):
    ui = tmp_path / "console-ui"
    index = tmp_path / "pkg" / "dist" / "index.html"
    _touch(index, 1000)  # built first…
    _touch(ui / "src" / "App.tsx", 2000)  # …then a source edit
    _touch(ui / "package.json", 1500)
    assert ui_is_stale(ui, index) is True


def test_fresh_bundle_is_not_stale(tmp_path):
    ui = tmp_path / "console-ui"
    _touch(ui / "src" / "App.tsx", 1000)
    _touch(ui / "package.json", 1000)
    _touch(ui / "index.html", 1000)
    index = tmp_path / "pkg" / "dist" / "index.html"
    _touch(index, 2000)  # built after all source
    assert ui_is_stale(ui, index) is False


def test_watched_root_file_triggers_stale(tmp_path):
    # A change to vite.config.ts (not under src/) must still count.
    ui = tmp_path / "console-ui"
    index = tmp_path / "pkg" / "dist" / "index.html"
    _touch(index, 1000)
    _touch(ui / "vite.config.ts", 3000)
    assert ui_is_stale(ui, index) is True
