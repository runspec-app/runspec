"""
Tests for the Outlook event source (sources/outlook.py).

The payload extractors are pure, so they're tested with plain fakes on any
platform. The COM pump can't run off-Windows, so we test the platform/availability
guards and that start()/stop() are safe no-ops here.
"""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

from runspec_console.events import Event, EventSource, available_sources, create_source
import pytest

from runspec_console.sources.outlook import (
    OutlookSource,
    _mail_payload,
    _reminder_payload,
    _split_categories,
    save_reply_draft,
)


# ── payload extraction ────────────────────────────────────────────────────────


def test_mail_payload_basic():
    mail = SimpleNamespace(
        SenderEmailAddress="alice@big.com",
        SenderName="Alice",
        To="ops@us.com",
        Subject="Outage on web01",
        Body="the site is down",
        ReceivedTime="2026-06-09 10:00",
        Categories="Red Category, Blue Category",
        EntryID="ENT123",
    )
    p = _mail_payload(mail)
    assert p == {
        "from": "alice@big.com",
        "to": "ops@us.com",
        "subject": "Outage on web01",
        "body": "the site is down",
        "received": "2026-06-09 10:00",
        "categories": ["Red Category", "Blue Category"],
        "entry_id": "ENT123",
    }


def test_mail_payload_sender_fallback_to_name():
    mail = SimpleNamespace(SenderEmailAddress="", SenderName="Jane Doe", Subject="hi")
    assert _mail_payload(mail)["from"] == "Jane Doe"


def test_mail_payload_truncates_body():
    mail = SimpleNamespace(Body="x" * 50_000, Subject="big")
    body = _mail_payload(mail)["body"]
    assert len(body) == 20_000 + 1  # cap + ellipsis
    assert body.endswith("…")


def test_mail_payload_tolerates_missing_fields():
    # A bare object with no Outlook attributes must not raise.
    p = _mail_payload(object())
    assert p["subject"] == "" and p["categories"] == [] and p["from"] == ""


def test_split_categories():
    assert _split_categories("A, B ,C") == ["A", "B", "C"]
    assert _split_categories("") == []
    assert _split_categories(None) == []


def test_reminder_payload_uses_inner_item():
    item = SimpleNamespace(
        Subject="Standup",
        Start="2026-06-09 09:30",
        Location="Room 1",
        MeetingStatus=1,
        Organizer="Boss",
    )
    p = _reminder_payload(SimpleNamespace(Item=item))
    assert p == {
        "subject": "Standup",
        "start": "2026-06-09 09:30",
        "location": "Room 1",
        "is_meeting": True,
        "organizer": "Boss",
    }


def test_reminder_payload_without_inner_item():
    p = _reminder_payload(SimpleNamespace(Subject="Pay invoice"))
    assert p["subject"] == "Pay invoice"
    assert p["is_meeting"] is False  # no MeetingStatus → non-meeting


# ── availability / platform guards ───────────────────────────────────────────


def test_available_off_windows():
    if sys.platform == "win32":
        return  # this assertion is about the non-Windows branch
    ok, reason = OutlookSource().available()
    assert ok is False and "Windows" in reason


def test_available_reports_missing_pywin32(monkeypatch):
    # Force the Windows branch on a box without pywin32 → the import guard fires.
    monkeypatch.setattr(sys, "platform", "win32")
    ok, reason = OutlookSource().available()
    if not ok:
        assert "pywin32" in reason
    # (On a real Windows box with pywin32 this would be (True, "") — fine.)


def test_event_types():
    assert OutlookSource().event_types() == ["new_mail", "reminder"]


def test_start_is_noop_when_unavailable(monkeypatch):
    # Guarantee the unavailable path regardless of host platform.
    monkeypatch.setattr(OutlookSource, "available", lambda self: (False, "nope"))
    src = OutlookSource()
    emitted = []
    src.start(emitted.append)
    src.stop()  # must not raise
    assert emitted == []
    assert src._thread is None


def test_dispatch_loop_marshals_events_and_stops():
    """Events enqueued (as the COM pump thread does) are delivered to the real
    emit by the dispatcher thread — never inline — and the None sentinel that
    stop() enqueues makes the loop exit. This is the off-pump marshalling that
    keeps the rota's calendar COM read from running re-entrantly in a callback."""
    src = OutlookSource()
    received: list[Event] = []
    got = threading.Event()

    def emit(ev: Event) -> None:
        received.append(ev)
        got.set()

    src._emit = emit
    t = threading.Thread(target=src._dispatch_loop, daemon=True)
    t.start()
    try:
        ev = Event.new("outlook", "reminder", {"subject": "standup"})
        src._enqueue(ev)  # what app.emit does on the pump thread
        assert got.wait(2), "dispatcher did not deliver the event"
        assert received == [ev]
    finally:
        src._queue.put(None)  # sentinel, as stop() enqueues
        t.join(timeout=2)
    assert not t.is_alive()


# ── protocol + registration ──────────────────────────────────────────────────


def test_satisfies_protocol():
    assert isinstance(OutlookSource(), EventSource)


def test_registered_on_import():
    assert "outlook" in available_sources()
    assert isinstance(create_source("outlook"), OutlookSource)


# ── draft reply writer guards ─────────────────────────────────────────────────


def test_save_reply_draft_requires_entry_id(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")  # past the platform guard
    with pytest.raises(RuntimeError, match="no source message"):
        save_reply_draft("", "hello")


def test_save_reply_draft_off_windows():
    if sys.platform == "win32":
        return
    with pytest.raises(RuntimeError, match="Windows"):
        save_reply_draft("ENT1", "hello")


# ── calendar rota decision ────────────────────────────────────────────────────


def _ev(subject, start, end, busy=False):
    from datetime import datetime

    return {
        "subject": subject,
        "start": datetime(2026, 6, 10, *start),
        "end": datetime(2026, 6, 10, *end),
        "busy": busy,
    }


def test_decide_calendar_on_and_off_shift():
    from datetime import datetime

    from runspec_console.sources.outlook import decide_calendar

    now = datetime(2026, 6, 10, 10, 0)
    shift = [_ev("Shift", (9, 0), (17, 0))]
    assert decide_calendar(shift, now, "shift", False) is True  # case-insensitive
    # No marker event covering now → off shift.
    assert decide_calendar([], now, "shift", False) is False
    assert (
        decide_calendar([_ev("Shift", (12, 0), (17, 0))], now, "shift", False) is False
    )  # marker but not covering 10:00


def test_decide_calendar_hold_during_meetings():
    from datetime import datetime

    from runspec_console.sources.outlook import decide_calendar

    now = datetime(2026, 6, 10, 10, 0)
    events = [
        _ev("Shift", (9, 0), (17, 0)),
        _ev("Standup", (9, 30), (10, 30), busy=True),
    ]
    # hold off → fires despite the meeting; hold on → suppressed.
    assert decide_calendar(events, now, "shift", False) is True
    assert decide_calendar(events, now, "shift", True) is False


def test_calendar_clock_ok_none_off_windows():
    if sys.platform == "win32":
        return
    from datetime import datetime

    from runspec_console.sources.outlook import calendar_clock_ok

    assert calendar_clock_ok(datetime.now(), "shift", False) is None
