"""winlock probes — best-effort, never raise.

``is_screen_locked`` is fail-open (bool); ``screen_lock_state`` is the tri-state
the governance gate uses (``unknown`` ⇒ fail-closed in the caller)."""

from __future__ import annotations

import sys

from runspec_console.winlock import is_screen_locked, screen_lock_state


def test_off_windows_is_false():
    if sys.platform != "win32":
        assert is_screen_locked() is False


def test_never_raises():
    # Whatever the platform, the probe must return a bool and not raise.
    assert isinstance(is_screen_locked(), bool)


def test_state_off_windows_is_unlocked():
    # Non-Windows has no lock concept → 'unlocked', so a fail-closed gate is a
    # no-op there (never disables automation on Linux/macOS).
    if sys.platform != "win32":
        assert screen_lock_state() == "unlocked"


def test_state_is_one_of_three():
    assert screen_lock_state() in {"locked", "unlocked", "unknown"}


def test_is_locked_is_fail_open_wrapper(monkeypatch):
    import runspec_console.winlock as wl

    monkeypatch.setattr(wl, "screen_lock_state", lambda: "unknown")
    assert wl.is_screen_locked() is False  # unknown ⇒ not "definitely locked"
    monkeypatch.setattr(wl, "screen_lock_state", lambda: "locked")
    assert wl.is_screen_locked() is True
