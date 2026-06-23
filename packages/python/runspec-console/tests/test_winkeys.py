"""Tests for winkeys — the Voice access hotkey injector.

The SendInput path only runs on Windows; off-Windows everything must be a safe,
silent no-op. We assert the chord constant and the non-Windows behaviour (which
is what CI exercises).
"""

from __future__ import annotations

import sys

import pytest

from runspec_console import winkeys


def test_voice_access_chord_is_alt_shift_b():
    # Alt, Shift, B in that order — modifiers first so they're held over B.
    assert winkeys.VOICE_ACCESS_TOGGLE == (0x12, 0x10, 0x42)


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows no-op path")
def test_send_is_noop_off_windows():
    # Must never raise and must report it did nothing.
    assert winkeys.send_voice_access_toggle() is False


def test_empty_chord_is_noop():
    assert winkeys._send_chord(()) is False
