"""
Tests for the notification-listener source (sources/notifications.py).

Payload extraction and the new-id diff are pure, tested with fakes. The WinRT
poll loop can't run off-Windows, so we test the platform/availability guards and
that start()/stop() are safe no-ops here.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from runspec_console.events import EventSource, available_sources, create_source
from runspec_console.sources.notifications import (
    NotificationSource,
    new_ids,
    notification_payload,
)


def _toast(app, *texts, id="N1"):
    """Build a fake matching the WinRT UserNotification → Notification → visual →
    bindings → text-elements object graph."""
    elements = [SimpleNamespace(text=t) for t in texts]
    binding = SimpleNamespace(get_text_elements=lambda: elements)
    visual = SimpleNamespace(bindings=[binding])
    return SimpleNamespace(
        id=id,
        app_info=SimpleNamespace(display_info=SimpleNamespace(display_name=app)),
        notification=SimpleNamespace(visual=visual),
    )


# ── payload extraction ────────────────────────────────────────────────────────


def test_notification_payload_title_and_body():
    un = _toast("Microsoft Teams", "Alice Smith", "Can you review the PR?", id="abc")
    p = notification_payload(un)
    assert p == {
        "id": "abc",
        "app": "Microsoft Teams",
        "title": "Alice Smith",
        "body": "Can you review the PR?",
        "text": "Alice Smith\nCan you review the PR?",
    }


def test_notification_payload_multiline_body():
    un = _toast("Slack", "general", "line1", "line2")
    p = notification_payload(un)
    assert p["title"] == "general"
    assert p["body"] == "line1\nline2"


def test_notification_payload_tolerates_missing():
    p = notification_payload(object())
    assert p["title"] == "" and p["body"] == "" and p["app"] == ""


def test_new_ids():
    assert new_ids({"a", "b"}, ["a", "b", "c", "d"]) == ["c", "d"]
    assert new_ids(set(), ["x"]) == ["x"]
    assert new_ids({"a"}, ["a"]) == []
    assert new_ids(set(), ["", "y"]) == ["y"]  # blanks ignored


# ── availability / platform guards ───────────────────────────────────────────


def test_available_off_windows():
    if sys.platform == "win32":
        return
    ok, reason = NotificationSource().available()
    assert ok is False and "Windows" in reason


def test_available_reports_missing_winsdk(monkeypatch):
    # Force the Windows branch, then make the winsdk import fail deterministically
    # (it may genuinely be installed on a Windows runner) so we exercise the
    # missing-dependency path on any platform.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winsdk", None)
    ok, reason = NotificationSource().available()
    assert ok is False
    assert "winsdk" in reason


def test_event_types():
    assert NotificationSource().event_types() == ["toast"]


def test_start_noop_when_unavailable(monkeypatch):
    monkeypatch.setattr(NotificationSource, "available", lambda self: (False, "nope"))
    src = NotificationSource()
    emitted = []
    src.start(emitted.append)
    src.stop()
    assert emitted == []
    assert src._thread is None


# ── protocol + registration ──────────────────────────────────────────────────


def test_satisfies_protocol():
    assert isinstance(NotificationSource(), EventSource)


def test_registered_on_import():
    assert "notifications" in available_sources()
    assert isinstance(create_source("notifications"), NotificationSource)
