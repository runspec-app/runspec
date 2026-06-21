"""winlock.is_screen_locked — best-effort, never raises, False off-Windows."""

from __future__ import annotations

import sys

from runspec_console.winlock import is_screen_locked


def test_off_windows_is_false():
    if sys.platform != "win32":
        assert is_screen_locked() is False


def test_never_raises():
    # Whatever the platform, the probe must return a bool and not raise.
    assert isinstance(is_screen_locked(), bool)
