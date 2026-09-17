"""
test_break.py — the Rota "Take a break" gate + start/cancel.

Binds the real Bridge methods onto a light stub (no Bridge.__init__). The rota
config, the lock probe, and Outlook are monkeypatched so the timed-suspend /
auto-resume / lock-hold logic is exercised on any OS.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import runspec_console.config as cfg
import runspec_console.sources.outlook as ol
import runspec_console.winlock as wl
from runspec_console.bridge import Bridge


class _Stub:
    _build_clock_ok = Bridge._build_clock_ok
    start_break = Bridge.start_break
    cancel_break = Bridge.cancel_break

    def __init__(self) -> None:
        self._break_until: datetime | None = None

    def restart_chat(self) -> None:  # gate/break refresh presence — no-op in tests
        pass

    def app_title(self) -> str:  # the white-label window title (branding.py in prod)
        return "Acme Console"


# ── gate behaviour ───────────────────────────────────────────────────────────


def test_break_suspends_then_auto_resumes(monkeypatch):
    # Rota disabled → normally always-on; an active break must still suspend, and
    # resume (self-clearing) once the window elapses and the screen is unlocked.
    monkeypatch.setattr(cfg, "read_rota", lambda: {})
    monkeypatch.setattr(wl, "is_screen_locked", lambda: False)
    b = _Stub()
    clock = b._build_clock_ok()

    assert clock() is True  # no break → always-on
    b._break_until = datetime.now() + timedelta(minutes=10)
    assert clock() is False  # inside the break window
    b._break_until = datetime.now() - timedelta(seconds=1)  # window elapsed
    assert clock() is True  # unlocked → resume
    assert b._break_until is None  # self-cleared


def test_break_held_while_screen_locked(monkeypatch):
    monkeypatch.setattr(cfg, "read_rota", lambda: {})
    monkeypatch.setattr(wl, "is_screen_locked", lambda: True)
    b = _Stub()
    clock = b._build_clock_ok()
    b._break_until = datetime.now() - timedelta(seconds=1)  # window already elapsed
    assert clock() is False  # still locked → held
    assert b._break_until is not None  # NOT cleared while locked


def test_break_overrides_enabled_rota(monkeypatch):
    # Even when within working hours, an active break suspends.
    monkeypatch.setattr(wl, "is_screen_locked", lambda: False)
    all_on = {
        d: {"on": True, "start": "00:00", "end": "23:59"}
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }
    monkeypatch.setattr(cfg, "read_rota", lambda: {"enabled": True, **all_on})
    b = _Stub()
    clock = b._build_clock_ok()
    assert clock() is True  # inside hours, no break
    b._break_until = datetime.now() + timedelta(minutes=5)
    assert clock() is False  # break wins


# ── start / cancel ───────────────────────────────────────────────────────────


def test_start_break_sets_until_and_reminder(monkeypatch):
    seen = {}
    monkeypatch.setattr(ol, "outlook_available", lambda: True)

    def fake_create(subject, when):
        seen["subject"] = subject
        seen["when"] = when

    monkeypatch.setattr(ol, "create_reminder", fake_create)
    b = _Stub()
    res = b.start_break(30)
    assert b._break_until is not None
    assert res["reminder_set"] is True
    assert res["break_until"]  # ISO string
    assert seen["subject"].startswith("Break over")
    # The reminder is branded to the window-title name, not the literal "runspec".
    assert "Acme Console" in seen["subject"]


def test_start_break_without_outlook(monkeypatch):
    monkeypatch.setattr(ol, "outlook_available", lambda: False)
    b = _Stub()
    res = b.start_break(15)
    assert b._break_until is not None
    assert res["reminder_set"] is False


def test_start_break_survives_reminder_failure(monkeypatch):
    monkeypatch.setattr(ol, "outlook_available", lambda: True)

    def boom(subject, when):
        raise RuntimeError("RPC server is not available")

    monkeypatch.setattr(ol, "create_reminder", boom)
    b = _Stub()
    res = b.start_break(60)
    assert b._break_until is not None  # break still set
    assert res["reminder_set"] is False  # reminder failed, reported


def test_start_break_floors_minutes(monkeypatch):
    monkeypatch.setattr(ol, "outlook_available", lambda: False)
    b = _Stub()
    before = datetime.now()
    b.start_break(0)  # floored to 1
    assert b._break_until is not None and b._break_until > before


def test_cancel_break(monkeypatch):
    b = _Stub()
    b._break_until = datetime.now() + timedelta(minutes=10)
    assert b.cancel_break() == {"ok": True}
    assert b._break_until is None
