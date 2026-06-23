"""Tests for winkeys — the Voice access hotkey injector.

The SendInput path only runs on Windows; off-Windows everything must be a safe,
silent no-op. We assert the chord constant and the non-Windows behaviour (which
is what CI exercises).
"""

from __future__ import annotations

import sys

import pytest

from runspec_console import winkeys


def test_voice_access_chord_is_ctrl_win_s():
    # Ctrl, Win, S in that order — modifiers first so they're held over S.
    assert winkeys.VOICE_ACCESS_HOTKEY == (0x11, 0x5B, 0x53)


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows no-op path")
def test_send_is_noop_off_windows():
    # Must never raise and must report it did nothing.
    assert winkeys.send_voice_access_hotkey() is False


def test_empty_chord_is_noop():
    assert winkeys._send_chord(()) is False
