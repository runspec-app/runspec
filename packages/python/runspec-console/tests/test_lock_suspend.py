"""
test_lock_suspend.py — the bridge lock auto-suspend gate (`_build_lock_ok`).

Default-on (absent `lock_suspend` ⇒ enabled), and fail-closed when on (an
`unknown` probe counts as locked). Binds the real method onto a light stub.
"""

from __future__ import annotations

import runspec_console.config as cfg
import runspec_console.winlock as wl
from runspec_console.bridge import Bridge


class _Stub:
    _build_lock_ok = Bridge._build_lock_ok
    screen_state = Bridge.screen_state


def test_default_on_suspends_when_locked(monkeypatch):
    # No lock_suspend key → defaults ON.
    monkeypatch.setattr(cfg, "read_rota", lambda: {})
    monkeypatch.setattr(wl, "screen_lock_state", lambda: "locked")
    assert _Stub()._build_lock_ok()() is False  # not ok to fire


def test_default_on_allows_when_unlocked(monkeypatch):
    monkeypatch.setattr(cfg, "read_rota", lambda: {})
    monkeypatch.setattr(wl, "screen_lock_state", lambda: "unlocked")
    assert _Stub()._build_lock_ok()() is True


def test_fail_closed_on_unknown(monkeypatch):
    # When enabled, an uncertain probe must NOT let automation run.
    monkeypatch.setattr(cfg, "read_rota", lambda: {"lock_suspend": True})
    monkeypatch.setattr(wl, "screen_lock_state", lambda: "unknown")
    assert _Stub()._build_lock_ok()() is False


def test_disabled_is_noop_even_when_locked(monkeypatch):
    monkeypatch.setattr(cfg, "read_rota", lambda: {"lock_suspend": False})
    monkeypatch.setattr(wl, "screen_lock_state", lambda: "locked")
    assert _Stub()._build_lock_ok()() is True  # gate off → always ok


def test_enabled_flag_captured_at_build(monkeypatch):
    # The toggle is captured at build (changing it goes through save_rota →
    # restart); the lock *state* is read live.
    monkeypatch.setattr(cfg, "read_rota", lambda: {"lock_suspend": True})
    states = iter(["unlocked", "locked"])
    monkeypatch.setattr(wl, "screen_lock_state", lambda: next(states))
    gate = _Stub()._build_lock_ok()
    assert gate() is True  # unlocked
    assert gate() is False  # locked — re-read live


def test_screen_state_passthrough(monkeypatch):
    monkeypatch.setattr(wl, "screen_lock_state", lambda: "locked")
    assert _Stub().screen_state() == "locked"
