"""
test_outlook_tools.py — the built-in Outlook Classic (COM) agent tools.

Binds the real Bridge methods onto a light stub (no Bridge.__init__). The COM
helpers in sources/outlook are monkeypatched so we assert on tool gating,
autonomy, argument plumbing, and the per-turn awareness block — all on any OS.
"""

from __future__ import annotations

import json
import types

import runspec_console.config as cfg
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import _OUTLOOK_READ_TOOLS, _OUTLOOK_WRITE_TOOLS
from runspec_console.sources import outlook as ol


class _StubBridge:
    _outlook_tools = Bridge._outlook_tools
    _run_outlook_tool = Bridge._run_outlook_tool
    _effective_tool_autonomy = Bridge._effective_tool_autonomy


# ── autonomy ─────────────────────────────────────────────────────────────────


def test_reads_autonomous_writes_confirm():
    b = _StubBridge()
    for name in _OUTLOOK_READ_TOOLS:
        assert b._effective_tool_autonomy(name, {}) == "autonomous"
    for name in _OUTLOOK_WRITE_TOOLS:
        assert b._effective_tool_autonomy(name, {}) == "confirm"


# ── schema gating ────────────────────────────────────────────────────────────


def test_no_tools_when_outlook_unavailable(monkeypatch):
    monkeypatch.setattr(ol, "outlook_available", lambda: False)
    assert _StubBridge()._outlook_tools() == []


def test_tools_offered_when_available(monkeypatch):
    monkeypatch.setattr(ol, "outlook_available", lambda: True)
    monkeypatch.setattr(
        cfg,
        "read_mailbox",
        lambda: {"address": "help@corp.com", "folder": "Tickets/Open"},
    )
    names = [t["name"] for t in _StubBridge()._outlook_tools()]
    assert names == [
        "outlook_inbox",
        "outlook_search",
        "outlook_calendar",
        "outlook_reminders",
        "outlook_draft",
        "outlook_send",
    ]
    # The monitored mailbox is surfaced in the read tools' descriptions.
    inbox = next(
        t for t in _StubBridge()._outlook_tools() if t["name"] == "outlook_inbox"
    )
    assert "help@corp.com" in inbox["description"]


# ── dispatch / argument plumbing ──────────────────────────────────────────────


def test_inbox_returns_json_rows(monkeypatch):
    calls = {}

    def fake(mb, fo, unread, count):
        calls["args"] = (mb, fo, unread, count)
        return [{"subject": "Hi", "entry_id": "E1"}]

    monkeypatch.setattr(ol, "list_messages", fake)
    out = _StubBridge()._run_outlook_tool(
        "outlook_inbox",
        {"unread_only": False, "count": 5, "mailbox": "m", "folder": "f"},
    )
    assert calls["args"] == ("m", "f", False, 5)
    data = json.loads(out)
    assert data["count"] == 1 and data["messages"][0]["entry_id"] == "E1"


def test_search_requires_query():
    out = _StubBridge()._run_outlook_tool("outlook_search", {})
    assert out.startswith("Error:") and "query" in out


def test_search_passes_query(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ol, "search_messages", lambda q, mb, fo, count: seen.setdefault("q", q) or []
    )
    _StubBridge()._run_outlook_tool("outlook_search", {"query": "invoice"})
    assert seen["q"] == "invoice"


def test_calendar_handles_unreadable(monkeypatch):
    monkeypatch.setattr(ol, "upcoming_events", lambda days, count: None)
    out = _StubBridge()._run_outlook_tool("outlook_calendar", {})
    assert "Could not read" in out


def test_draft_reply_path(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ol,
        "save_reply_draft",
        lambda eid, body, fm: (
            seen.update(eid=eid, body=body, fm=fm)
            or {"to": "a@b.com", "subject": "RE: x", "entry_id": "D1"}
        ),
    )
    out = _StubBridge()._run_outlook_tool(
        "outlook_draft", {"reply_to": "E1", "body": "thanks"}
    )
    assert seen == {"eid": "E1", "body": "thanks", "fm": None}
    assert "D1" in out and out.startswith("✓ Draft saved")


def test_draft_new_requires_recipient(monkeypatch):
    monkeypatch.setattr(ol, "save_new_draft", lambda *a, **k: {"entry_id": "x"})
    out = _StubBridge()._run_outlook_tool("outlook_draft", {"body": "hi"})
    assert out.startswith("Error:") and "to" in out


def test_send_existing_draft(monkeypatch):
    seen = {}

    def fake(eid):
        seen["eid"] = eid
        return {"to": "a@b.com", "subject": "RE: x"}

    monkeypatch.setattr(ol, "send_draft", fake)
    out = _StubBridge()._run_outlook_tool("outlook_send", {"entry_id": "D1"})
    assert seen["eid"] == "D1" and out.startswith("✓ Sent")


def test_send_compose_requires_recipient():
    out = _StubBridge()._run_outlook_tool("outlook_send", {"subject": "s"})
    assert out.startswith("Error:") and "entry_id" in out


def test_com_failure_surfaced(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("RPC server is not available")

    monkeypatch.setattr(ol, "list_messages", boom)
    out = _StubBridge()._run_outlook_tool("outlook_inbox", {})
    assert out.startswith("✗ Outlook outlook_inbox failed:") and "RPC" in out


# ── per-turn awareness (_console_context) ─────────────────────────────────────


def _ctx_stub(monkeypatch, *, mailbox, available=True):
    monkeypatch.setattr(cfg, "read_rota", lambda: {})
    monkeypatch.setattr(cfg, "read_triggers", lambda: [])
    monkeypatch.setattr(cfg, "read_mailbox", lambda: mailbox)
    monkeypatch.setattr(ol, "outlook_available", lambda: available)
    ns = types.SimpleNamespace()
    ns._read_schedules = lambda: []
    ns._groups_map = lambda: {}
    ns._break_until = None
    ns._console_context = types.MethodType(Bridge._console_context, ns)
    return ns


def test_context_mentions_mailbox_and_tools(monkeypatch):
    ns = _ctx_stub(
        monkeypatch,
        mailbox={
            "address": "help@corp.com",
            "folder": "Tickets/Open",
            "label": "Helpdesk",
        },
    )
    out = ns._console_context()
    assert "Outlook (Classic, desktop)" in out
    assert "help@corp.com / Tickets/Open" in out
    assert "Helpdesk" in out
    assert "outlook_send" in out


def test_context_empty_when_no_mailbox_and_nothing_else(monkeypatch):
    ns = _ctx_stub(monkeypatch, mailbox=None)
    assert ns._console_context() == ""


def test_context_skips_outlook_when_unavailable(monkeypatch):
    # Even with a saved mailbox, a non-Windows console shouldn't claim the tools.
    ns = _ctx_stub(monkeypatch, mailbox={"address": "help@corp.com"}, available=False)
    assert ns._console_context() == ""
