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
        "outlook_get_message",
        "outlook_calendar",
        "outlook_reminders",
        "outlook_resolve_recipient",
        "outlook_draft",
        "outlook_send",
        "outlook_save_attachment",
        "outlook_create_event",
        "outlook_respond_invite",
        "outlook_mark_read",
        "outlook_categorize",
        "outlook_move",
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
        lambda eid, body, fm, reply_all=False: (
            seen.update(eid=eid, body=body, fm=fm, reply_all=reply_all)
            or {"to": "a@b.com", "subject": "RE: x", "entry_id": "D1"}
        ),
    )
    out = _StubBridge()._run_outlook_tool(
        "outlook_draft", {"reply_to": "E1", "body": "thanks"}
    )
    assert seen == {"eid": "E1", "body": "thanks", "fm": None, "reply_all": False}
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


# ── dispatch: new reads (get_message / resolve_recipient) ─────────────────────


def test_get_message_requires_entry_id():
    out = _StubBridge()._run_outlook_tool("outlook_get_message", {})
    assert out.startswith("Error:") and "entry_id" in out


def test_get_message_returns_full_json(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ol,
        "get_message",
        lambda eid: (
            seen.update(eid=eid)
            or {"subject": "Hi", "body": "full body", "attachments": []}
        ),
    )
    out = _StubBridge()._run_outlook_tool("outlook_get_message", {"entry_id": "E1"})
    assert seen["eid"] == "E1"
    assert json.loads(out)["body"] == "full body"


def test_resolve_recipient_requires_name():
    out = _StubBridge()._run_outlook_tool("outlook_resolve_recipient", {})
    assert out.startswith("Error:") and "name" in out


def test_resolve_recipient_returns_json(monkeypatch):
    monkeypatch.setattr(
        ol,
        "resolve_recipient",
        lambda who: {"resolved": True, "name": who, "address": "j@corp.com"},
    )
    out = _StubBridge()._run_outlook_tool("outlook_resolve_recipient", {"name": "Jane"})
    assert json.loads(out)["address"] == "j@corp.com"


# ── dispatch: new writes (save_attachment / create_event / respond_invite) ────


def test_save_attachment_requires_entry_id():
    out = _StubBridge()._run_outlook_tool("outlook_save_attachment", {})
    assert out.startswith("Error:") and "entry_id" in out


def test_save_attachment_plumbs_args(monkeypatch):
    seen = {}

    def fake(eid, name, index, dest):
        seen.update(eid=eid, name=name, index=index, dest=dest)
        return {"name": "app.log", "path": "/tmp/app.log", "size": 12}

    monkeypatch.setattr(ol, "save_attachment", fake)
    out = _StubBridge()._run_outlook_tool(
        "outlook_save_attachment",
        {"entry_id": "E1", "name": "app.log", "index": 0, "dest_dir": "/tmp"},
    )
    assert seen == {"eid": "E1", "name": "app.log", "index": 0, "dest": "/tmp"}
    assert out.startswith("✓ Saved attachment 'app.log'") and "/tmp/app.log" in out


def test_create_event_requires_subject():
    out = _StubBridge()._run_outlook_tool(
        "outlook_create_event", {"start": "2026-06-27T14:00"}
    )
    assert out.startswith("Error:") and "subject" in out


def test_create_event_rejects_bad_start():
    out = _StubBridge()._run_outlook_tool(
        "outlook_create_event", {"subject": "Sync", "start": "not-a-date"}
    )
    assert out.startswith("Error:") and "start" in out


def test_create_event_defaults_end_and_reports_invite(monkeypatch):
    from datetime import datetime, timedelta

    seen = {}

    def fake(subject, start, end, **kw):
        seen.update(subject=subject, start=start, end=end, **kw)
        return {
            "subject": subject,
            "start": str(start),
            "end": str(end),
            "entry_id": "A1",
            "sent": bool(kw.get("attendees")),
        }

    monkeypatch.setattr(ol, "create_event", fake)
    out = _StubBridge()._run_outlook_tool(
        "outlook_create_event",
        {"subject": "Sync", "start": "2026-06-27T14:00", "attendees": "a@b.com"},
    )
    # end defaults to start + 30 min when omitted
    assert seen["end"] - seen["start"] == timedelta(minutes=30)
    assert seen["start"] == datetime(2026, 6, 27, 14, 0)
    assert out.startswith("✓ Invite sent")


def test_create_event_plumbs_online_meeting(monkeypatch):
    seen = {}

    def fake(subject, start, end, **kw):
        seen.update(kw)
        return {
            "subject": subject,
            "start": str(start),
            "end": str(end),
            "entry_id": "A3",
            "sent": bool(kw.get("attendees")),
            "online_meeting": bool(kw.get("online_meeting")),
        }

    def with_link(subject, start, end, **kw):
        d = fake(subject, start, end, **kw)
        d["join_url"] = "https://teams.microsoft.com/l/meetup-join/xyz"
        return d

    monkeypatch.setattr(ol, "create_event", with_link)
    out = _StubBridge()._run_outlook_tool(
        "outlook_create_event",
        {
            "subject": "Design review",
            "start": "2026-06-27T15:00",
            "attendees": "a@b.com",
            "online_meeting": True,
        },
    )
    assert seen["online_meeting"] is True
    assert "Teams online meeting enabled" in out
    assert "Teams join link: https://teams.microsoft.com/l/meetup-join/xyz" in out


def test_create_event_online_meeting_link_pending(monkeypatch):
    # online_meeting requested but no link yet (add-in/restart pending) → the tool
    # says so plainly rather than dropping a bare success line.
    monkeypatch.setattr(
        ol,
        "create_event",
        lambda *a, **k: {
            "subject": "Design review",
            "start": "x",
            "end": "y",
            "entry_id": "A4",
            "sent": True,
            "online_meeting": True,
            "join_url": "",
        },
    )
    out = _StubBridge()._run_outlook_tool(
        "outlook_create_event",
        {
            "subject": "Design review",
            "start": "2026-06-27T15:00",
            "online_meeting": True,
        },
    )
    assert "not available yet" in out and "restart" in out


def test_create_event_personal_block_when_no_attendees(monkeypatch):
    monkeypatch.setattr(
        ol,
        "create_event",
        lambda *a, **k: {
            "subject": "Focus",
            "start": "x",
            "end": "y",
            "entry_id": "A2",
            "sent": False,
        },
    )
    out = _StubBridge()._run_outlook_tool(
        "outlook_create_event", {"subject": "Focus", "start": "2026-06-27T09:00"}
    )
    assert "saved to your calendar" in out.lower()


def test_respond_invite_requires_entry_id():
    out = _StubBridge()._run_outlook_tool(
        "outlook_respond_invite", {"response": "accept"}
    )
    assert out.startswith("Error:") and "entry_id" in out


def test_respond_invite_bad_response_surfaced(monkeypatch):
    def fake(eid, resp):
        raise ValueError(
            "response must be one of accept/tentative/decline (got 'maybe')"
        )

    monkeypatch.setattr(ol, "respond_invite", fake)
    out = _StubBridge()._run_outlook_tool(
        "outlook_respond_invite", {"entry_id": "E1", "response": "maybe"}
    )
    assert out.startswith("Error:") and "accept/tentative/decline" in out


def test_respond_invite_ok(monkeypatch):
    monkeypatch.setattr(
        ol, "respond_invite", lambda eid, resp: {"subject": "Standup", "response": resp}
    )
    out = _StubBridge()._run_outlook_tool(
        "outlook_respond_invite", {"entry_id": "E1", "response": "accept"}
    )
    assert out.startswith("✓ Responded 'accept'") and "Standup" in out


# ── dispatch: triage actions (mark / categorize / move) + reply_all ───────────


def test_draft_reply_all_plumbs_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ol,
        "save_reply_draft",
        lambda eid, body, fm, reply_all: (
            seen.update(reply_all=reply_all)
            or {"to": "a@b.com", "subject": "RE: x", "entry_id": "D1"}
        ),
    )
    _StubBridge()._run_outlook_tool(
        "outlook_draft", {"reply_to": "E1", "body": "hi", "reply_all": True}
    )
    assert seen["reply_all"] is True


def test_mark_read_requires_entry_id():
    out = _StubBridge()._run_outlook_tool("outlook_mark_read", {})
    assert out.startswith("Error:") and "entry_id" in out


def test_mark_read_reports_state(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ol,
        "mark_message",
        lambda eid, read: (
            seen.update(eid=eid, read=read) or {"subject": "Ticket", "unread": not read}
        ),
    )
    out = _StubBridge()._run_outlook_tool(
        "outlook_mark_read", {"entry_id": "E1", "read": True}
    )
    assert seen == {"eid": "E1", "read": True}
    assert out.startswith("✓ Marked read") and "Ticket" in out


def test_categorize_requires_categories():
    out = _StubBridge()._run_outlook_tool("outlook_categorize", {"entry_id": "E1"})
    assert out.startswith("Error:") and "categories" in out


def test_categorize_plumbs_and_reports(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ol,
        "categorize_message",
        lambda eid, cats, replace: (
            seen.update(eid=eid, cats=cats, replace=replace)
            or {"subject": "Outage", "categories": ["Triaged", "Outage"]}
        ),
    )
    out = _StubBridge()._run_outlook_tool(
        "outlook_categorize",
        {"entry_id": "E1", "categories": "Triaged, Outage", "replace": False},
    )
    assert seen == {"eid": "E1", "cats": "Triaged, Outage", "replace": False}
    assert "Triaged, Outage" in out


def test_move_requires_folder():
    out = _StubBridge()._run_outlook_tool("outlook_move", {"entry_id": "E1"})
    assert out.startswith("Error:") and "folder" in out


def test_move_plumbs_and_reports(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ol,
        "move_message",
        lambda eid, mb, fo: (
            seen.update(eid=eid, mb=mb, fo=fo)
            or {"subject": "Ticket", "folder": fo, "entry_id": "E2"}
        ),
    )
    out = _StubBridge()._run_outlook_tool(
        "outlook_move", {"entry_id": "E1", "folder": "Tickets/Closed"}
    )
    assert seen == {"eid": "E1", "mb": "", "fo": "Tickets/Closed"}
    assert out.startswith("✓ Moved to Tickets/Closed")


# ── pure helpers (no COM; run on any OS) ──────────────────────────────────────


def test_merge_categories():
    # add (union, case-insensitive dedupe, order preserved)
    assert ol._merge_categories("Red, Blue", "Blue, Green", False) == "Red, Blue, Green"
    # replace
    assert ol._merge_categories("Red, Blue", "Green", True) == "Green"
    # accepts ; separators; no existing
    assert ol._merge_categories("", "A; B", False) == "A, B"


class _FakeAttachment:
    def __init__(self, name, size, index):
        self.FileName, self.Size, self.Index = name, size, index


def test_attachment_rows_shape():
    rows = ol._attachment_rows(
        [_FakeAttachment("a.log", 10, 1), _FakeAttachment("b.csv", 20, 2)]
    )
    assert rows == [
        {"name": "a.log", "size": 10, "index": 1},
        {"name": "b.csv", "size": 20, "index": 2},
    ]


def test_full_message_caps_body_and_lists_attachments():
    class _Msg:
        Subject, SenderName, SenderEmailAddress = "S", "Jane", "j@c.com"
        To, CC, ReceivedTime, UnRead = "me@c.com", "cc@c.com", "now", False
        Categories, ConversationID, EntryID = "Red, Blue", "C1", "E1"
        Body = "x" * (ol._FULL_BODY_CAP + 100)
        Attachments = [_FakeAttachment("a.log", 5, 1)]

    msg = ol._full_message(_Msg())
    assert msg["from"] == "Jane" and msg["cc"] == "cc@c.com"
    assert msg["categories"] == ["Red", "Blue"]
    assert msg["attachments"] == [{"name": "a.log", "size": 5, "index": 1}]
    assert msg["body"].endswith("…") and len(msg["body"]) == ol._FULL_BODY_CAP + 1


def test_safe_attachment_name_strips_paths():
    assert ol._safe_attachment_name("../../etc/passwd") == "passwd"
    assert ol._safe_attachment_name(r"C:\tmp\evil.exe") == "evil.exe"
    assert ol._safe_attachment_name("") == "attachment"


def test_match_attachment_index():
    rows = [
        {"name": "report.csv", "index": 1},
        {"name": "trace.log", "index": 2},
    ]
    assert ol._match_attachment_index(rows, "", 2) == 2  # explicit index
    assert ol._match_attachment_index(rows, "", 9) is None  # bad index
    assert ol._match_attachment_index(rows, "trace.log", 0) == 2  # exact name
    assert ol._match_attachment_index(rows, "REPORT", 0) == 1  # substring, ci
    assert ol._match_attachment_index(rows, "", 0) is None  # ambiguous (2 items)
    assert ol._match_attachment_index([{"name": "x", "index": 1}], "", 0) == 1  # sole


def test_split_recipients():
    assert ol._split_recipients("a@b.com; c@d.com , e@f.com") == [
        "a@b.com",
        "c@d.com",
        "e@f.com",
    ]
    assert ol._split_recipients("") == []


def test_respond_code_mapping():
    assert ol._respond_code("Accept") == 3
    assert ol._respond_code("tentative") == 2
    assert ol._respond_code("decline") == 4
    try:
        ol._respond_code("maybe")
    except ValueError as exc:
        assert "accept/tentative/decline" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


class _FakeAppt:
    def __init__(self):
        self.recipients_added = []
        self.MeetingStatus = 0
        self.sent = self.saved = False
        self.EntryID = "A1"
        self.Subject = self.Start = self.End = None

        class _Recips:
            def __init__(self, outer):
                self._outer = outer

            def Add(self, addr):
                self._outer.recipients_added.append(addr)

            def ResolveAll(self):
                pass

        self.Recipients = _Recips(self)

    def Send(self):
        self.sent = True

    def Save(self):
        self.saved = True


class _FakeApp:
    def __init__(self, appt):
        self._appt = appt

    def CreateItem(self, kind):
        assert kind == 1  # olAppointmentItem
        return self._appt


def test_build_event_sends_invite_with_attendees():
    from datetime import datetime

    appt = _FakeAppt()
    res = ol._build_event(
        _FakeApp(appt),
        subject="Sync",
        start=datetime(2026, 6, 27, 14, 0),
        end=datetime(2026, 6, 27, 14, 30),
        attendees="a@b.com; c@d.com",
        location="Room 1",
        body="agenda",
        reminder_minutes=10,
    )
    assert appt.recipients_added == ["a@b.com", "c@d.com"]
    assert appt.MeetingStatus == 1 and appt.sent and not appt.saved
    assert res["sent"] is True and res["attendees"] == ["a@b.com", "c@d.com"]
    assert res["online_meeting"] is False  # not requested → reported off


def test_build_event_reports_online_meeting_flag():
    from datetime import datetime

    appt = _FakeAppt()
    res = ol._build_event(
        _FakeApp(appt),
        subject="Sync",
        start=datetime(2026, 6, 27, 14, 0),
        end=datetime(2026, 6, 27, 14, 30),
        attendees="a@b.com",
        location="",
        body="",
        reminder_minutes=10,
        online_meeting=True,
    )
    # _build_event only reflects the request; the registry side effect lives in
    # create_event (the Windows-only wrapper), not the pure core.
    assert res["online_meeting"] is True
    # no Teams body on the fake appt → first-chance join_url read is empty
    assert res["join_url"] == ""


def test_build_event_first_chance_join_url_from_body():
    from datetime import datetime

    appt = _FakeAppt()
    appt.Body = "Join https://teams.microsoft.com/l/meetup-join/xyz now"
    res = ol._build_event(
        _FakeApp(appt),
        subject="Sync",
        start=datetime(2026, 6, 27, 14, 0),
        end=datetime(2026, 6, 27, 14, 30),
        attendees="a@b.com",
        location="",
        body="",
        reminder_minutes=10,
        online_meeting=True,
    )
    assert res["join_url"] == "https://teams.microsoft.com/l/meetup-join/xyz"


def test_build_event_saves_personal_block_without_attendees():
    from datetime import datetime

    appt = _FakeAppt()
    res = ol._build_event(
        _FakeApp(appt),
        subject="Focus",
        start=datetime(2026, 6, 27, 9, 0),
        end=datetime(2026, 6, 27, 10, 0),
        attendees="",
        location="",
        body="",
        reminder_minutes=-1,  # negative ⇒ no reminder set
    )
    assert appt.saved and not appt.sent and appt.MeetingStatus == 0
    assert res["sent"] is False


def test_parse_dt():
    from datetime import datetime

    from runspec_console.bridge_agent_tools import _parse_dt

    assert _parse_dt("2026-06-27T14:00") == datetime(2026, 6, 27, 14, 0)
    assert _parse_dt("2026-06-27T14:00:00Z") == datetime(
        2026, 6, 27, 14, 0
    )  # tz dropped
    assert _parse_dt("") is None
    assert _parse_dt("garbage") is None


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
