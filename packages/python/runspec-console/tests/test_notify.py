"""
Tests for desktop notifications on confirmation events.

The message-building helpers are pure and tested directly. The winrt send can't
run off-Windows, so we test the platform/availability guards and that the bridge
gate (`_register_confirm` → `_notify_confirm`) is wired, gated on availability,
and honours the opt-out — all with `notify`'s functions monkeypatched so nothing
touches a real toast or the OS.
"""

from __future__ import annotations

import sys
import threading

from runspec_console import notify
from runspec_console.bridge import Bridge


# ── pure message building ─────────────────────────────────────────────────────


def test_build_confirm_message_strips_prefix():
    title, _ = notify.build_confirm_message("local__restart-service", {})
    assert title == "Approval needed: restart-service"


def test_build_confirm_message_matches_gate_display():
    # Mirrors the gate's own `tool_name.split("__", 1)[-1]` (drops the host
    # prefix), so the toast title reads the same as the in-app card.
    title, _ = notify.build_confirm_message("local__flush-dns", {"x": 1})
    assert title == "Approval needed: flush-dns"


def test_summarize_args_renders_key_values():
    assert notify.summarize_args({"host": "web01", "force": True}) == (
        "host=web01, force=True"
    )


def test_summarize_args_empty_has_a_call_to_action():
    body = notify.summarize_args({})
    assert "approve" in body.lower()


def test_summarize_args_truncates_long_summary():
    body = notify.summarize_args({"blob": "x" * 500})
    assert len(body) <= notify._MAX_BODY
    assert body.endswith("…")


def test_short_val_truncates_per_value():
    assert notify._short_val("y" * 100).endswith("…")
    assert len(notify._short_val("y" * 100)) <= notify._MAX_VAL


def test_toast_xml_escapes_special_chars():
    xml = notify._toast_xml("a & b", '<tag> "q"')
    assert "&amp;" in xml and "&lt;tag&gt;" in xml and "&quot;" in xml
    assert "<tag>" not in xml


# ── platform guards ───────────────────────────────────────────────────────────


def test_available_false_off_windows():
    if sys.platform != "win32":
        ok, reason = notify.available()
        assert ok is False
        assert "Windows" in reason


def test_notify_is_noop_off_windows():
    if sys.platform != "win32":
        assert notify.notify("t", "b") is False


def test_set_app_id_safe_off_windows():
    if sys.platform != "win32":
        notify.set_app_id()  # must not raise


# ── bridge gate wiring ────────────────────────────────────────────────────────


class _StubBridge:
    """Binds the real notify path onto a light stub (no Bridge.__init__)."""

    _WINDOW_TITLE = "runspec console"

    def __init__(self, enabled=True):
        self._lock = threading.Lock()
        self._pending_confirms = {}
        self._dispatched = []
        self._enabled = enabled

    def _dispatch(self, event, detail):
        self._dispatched.append((event, detail))

    def _confirm_notifications_enabled(self):
        return self._enabled

    def _flash_window(self):  # no win32 off-Windows
        pass

    def _focus_window(self):
        pass

    _register_confirm = Bridge._register_confirm
    _notify_confirm = Bridge._notify_confirm


def test_register_confirm_builds_a_notification_when_available(monkeypatch):
    built = []
    monkeypatch.setattr(notify, "available", lambda: (True, ""))
    monkeypatch.setattr(
        notify,
        "build_confirm_message",
        lambda name, inp: built.append((name, inp)) or ("T", "B"),
    )
    monkeypatch.setattr(notify, "notify", lambda *a, **k: True)

    b = _StubBridge(enabled=True)
    b._register_confirm("chat1", "local__flush-dns", {"force": True}, "confirm")

    # The confirm event still dispatches to the in-app card …
    assert b._dispatched[0][0] == "runspec:tool_confirm"
    # … and a desktop message was built from the same tool call.
    assert built == [("local__flush-dns", {"force": True})]


def test_notify_confirm_respects_opt_out(monkeypatch):
    built = []
    monkeypatch.setattr(notify, "available", lambda: (True, ""))
    monkeypatch.setattr(
        notify, "build_confirm_message", lambda *a: built.append(a) or ("T", "B")
    )

    b = _StubBridge(enabled=False)
    b._notify_confirm("local__flush-dns", {}, "confirm")

    assert built == []  # disabled → never even builds the message


def test_notify_confirm_skips_when_unavailable(monkeypatch):
    built = []
    monkeypatch.setattr(notify, "available", lambda: (False, "nope"))
    monkeypatch.setattr(
        notify, "build_confirm_message", lambda *a: built.append(a) or ("T", "B")
    )

    b = _StubBridge(enabled=True)
    b._notify_confirm("local__flush-dns", {}, "confirm")

    assert built == []  # unavailable → no work, no thread


def test_notify_confirm_never_raises(monkeypatch):
    monkeypatch.setattr(
        notify, "available", lambda: (_ for _ in ()).throw(RuntimeError)
    )
    b = _StubBridge()
    b._notify_confirm("local__x", {}, "confirm")  # swallowed
