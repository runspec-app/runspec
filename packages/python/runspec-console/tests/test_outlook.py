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
    _build_reminder_item,
    _calendar_options_key,
    _claim_category,
    _compose_reply_draft,
    _connection_alive,
    _descend,
    _event_row,
    _owner_from_categories,
    _same_folder,
    _extract_join_url,
    _folder_segments,
    _office_version_root,
    _read_join_url,
    _mail_payload,
    _mail_summary,
    _reminder_payload,
    _search_dasl,
    _split_categories,
    create_reminder,
    list_messages,
    outlook_available,
    save_new_draft,
    save_reply_draft,
    send_draft,
    send_new_mail,
    upcoming_events,
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
        ConversationID="CONV789",
    )
    p = _mail_payload(mail)
    assert p == {
        "from": "alice@big.com",
        "to": "ops@us.com",
        "subject": "Outage on web01",
        "body": "the site is down",
        "mailbox": "",  # no watch tag passed → empty
        "folder": "",
        "received": "2026-06-09 10:00",
        "categories": ["Red Category", "Blue Category"],
        "entry_id": "ENT123",
        "conversation_id": "CONV789",
    }


def test_mail_payload_carries_watch_tags():
    # The source tags each event with the mailbox + folder it watched.
    p = _mail_payload(
        SimpleNamespace(Subject="hi"),
        mailbox="it-support@corp.com",
        folder="Tickets/Open",
    )
    assert p["mailbox"] == "it-support@corp.com" and p["folder"] == "Tickets/Open"


def test_mail_payload_conversation_id_empty_when_missing():
    # A MailItem without ConversationID must not raise — the bridge then folds on
    # a normalised subject instead.
    assert _mail_payload(SimpleNamespace(Subject="hi"))["conversation_id"] == ""


# ── folder-watch helpers (pure) ───────────────────────────────────────────────


def test_folder_segments():
    assert _folder_segments("") == []  # → Inbox
    assert _folder_segments("Inbox") == []  # sentinel
    assert _folder_segments("inbox") == []  # case-insensitive
    assert _folder_segments("Tickets") == ["Tickets"]
    assert _folder_segments("Tickets/Open") == ["Tickets", "Open"]
    assert _folder_segments(" Tickets / Open ") == ["Tickets", "Open"]  # trimmed


def test_descend_walks_subfolders():
    # A fake folder tree: each node exposes .Folders[name].
    class _Folder:
        def __init__(self, name, children=None):
            self.name = name
            self.Folders = children or {}

    leaf = _Folder("Open")
    root = _Folder("root", {"Tickets": _Folder("Tickets", {"Open": leaf})})
    assert _descend(root, ["Tickets", "Open"]) is leaf
    assert _descend(root, []) is root  # no segments → the root itself


def test_handle_item_dedupes_by_entry_id():
    # _handle_item tags + enqueues a new_mail, but never twice for one EntryID.
    src = OutlookSource()
    item = SimpleNamespace(EntryID="E1", Subject="hi")
    src._handle_item(item, "it-support@corp.com", "Tickets")
    src._handle_item(item, "it-support@corp.com", "Tickets")  # same id → ignored
    events = []
    while not src._queue.empty():
        events.append(src._queue.get_nowait())
    assert len(events) == 1
    ev = events[0]
    assert ev.source == "outlook" and ev.type == "new_mail"
    assert ev.payload["mailbox"] == "it-support@corp.com"
    assert ev.payload["folder"] == "Tickets"


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


def test_watches_merges_primary_and_extras_and_dedupes(monkeypatch):
    # _watches() = the primary [mailbox] (room identity) + [[watch]] extras, with
    # exact (address, folder) duplicates collapsed so a repeat doesn't double-bind.
    import runspec_console.config as cfg

    monkeypatch.setattr(
        cfg,
        "read_mailbox",
        lambda: {"address": "shared@corp", "folders": ["Inbox"], "folder": "Inbox"},
    )
    monkeypatch.setattr(
        cfg,
        "read_watches",
        lambda: [
            {"address": "", "folders": ["Inbox"], "folder": "Inbox", "label": ""},
            {
                "address": "shared@corp",
                "folders": ["Inbox"],
                "folder": "Inbox",
                "label": "",
            },  # dup of primary
            {
                "address": "alerts@corp",
                "folders": ["Ops"],
                "folder": "Ops",
                "label": "",
            },
        ],
    )
    assert OutlookSource()._watches() == [
        {"mailbox": "shared@corp", "folder": "Inbox"},
        {"mailbox": "", "folder": "Inbox"},
        {"mailbox": "alerts@corp", "folder": "Ops"},
    ]


def test_watches_expands_a_folders_list(monkeypatch):
    # One [mailbox] with several folders → one watch per folder (deduped).
    import runspec_console.config as cfg

    monkeypatch.setattr(
        cfg,
        "read_mailbox",
        lambda: {
            "address": "shared@corp",
            "folders": ["Tickets/Open", "Alerts", "Tickets/Open"],
            "folder": "Tickets/Open",
        },
    )
    monkeypatch.setattr(cfg, "read_watches", lambda: [])
    assert OutlookSource()._watches() == [
        {"mailbox": "shared@corp", "folder": "Tickets/Open"},
        {"mailbox": "shared@corp", "folder": "Alerts"},
    ]


def test_watches_extras_only_without_primary(monkeypatch):
    # No primary [mailbox] but a watch-only extra still produces a watch.
    import runspec_console.config as cfg

    monkeypatch.setattr(cfg, "read_mailbox", lambda: None)
    monkeypatch.setattr(
        cfg,
        "read_watches",
        lambda: [{"address": "", "folders": ["Inbox"], "folder": "Inbox", "label": ""}],
    )
    assert OutlookSource()._watches() == [{"mailbox": "", "folder": "Inbox"}]


def test_watches_empty_when_nothing_configured(monkeypatch):
    import runspec_console.config as cfg

    monkeypatch.setattr(cfg, "read_mailbox", lambda: None)
    monkeypatch.setattr(cfg, "read_watches", lambda: [])
    assert OutlookSource()._watches() == []


def test_start_is_noop_when_unavailable(monkeypatch):
    # Guarantee the unavailable path regardless of host platform.
    monkeypatch.setattr(OutlookSource, "available", lambda self: (False, "nope"))
    src = OutlookSource()
    emitted = []
    src.start(emitted.append)
    src.stop()  # must not raise
    assert emitted == []
    assert src._thread is None


# ── claim cores (pure, duck-typed) ────────────────────────────────────────────


def test_claim_category_and_owner_roundtrip():
    cat = _claim_category("alice")
    assert cat == "Claimed: alice"
    assert _owner_from_categories(cat) == "alice"
    # Alongside unrelated categories, comma-joined as Outlook stores them.
    assert _owner_from_categories("Red, Claimed: bob, Blue") == "bob"
    # No claim marker present.
    assert _owner_from_categories("Red, Blue") == ""
    assert _owner_from_categories("") == ""


def test_claim_category_blank_owner():
    assert _claim_category("") == "Claimed: unknown"
    assert _claim_category("   ") == "Claimed: unknown"


def test_same_folder_compares_parent_entry_id():
    item = SimpleNamespace(Parent=SimpleNamespace(EntryID="F1"))
    assert _same_folder(item, SimpleNamespace(EntryID="F1")) is True
    assert _same_folder(item, SimpleNamespace(EntryID="F2")) is False


def test_same_folder_false_on_missing_parent():
    class NoParent:
        @property
        def Parent(self):  # noqa: N802 — COM name
            raise RuntimeError("gone")

    assert _same_folder(NoParent(), SimpleNamespace(EntryID="F1")) is False
    # Empty entry ids never match (avoids two blank ids reading as "same").
    item = SimpleNamespace(Parent=SimpleNamespace(EntryID=""))
    assert _same_folder(item, SimpleNamespace(EntryID="")) is False


def test_claim_message_requires_windows():
    if sys.platform == "win32":
        pytest.skip("COM path exercised only off-Windows for the guard")
    from runspec_console.sources.outlook import claim_message

    with pytest.raises(RuntimeError, match="requires Windows"):
        claim_message("E1", "alice", claim_folder="Claimed")


def test_stop_joins_pump_and_dispatch_threads():
    """stop() must fully unwind its threads before returning — not just signal
    them and orphan the references.

    A configured [mailbox] force-starts this source, and every mailbox-settings
    save calls restart_triggers → stop() then an *immediate* start(). If stop()
    returned while the old COM pump was still bound to the singleton
    Outlook.Application, the restart would bind a second STA pump to the same
    folders concurrently — overlapping STA pumps deadlock on Windows and froze
    the console. Joining serialises teardown → rebuild.
    """
    src = OutlookSource()
    started = threading.Event()

    # Stand in for the two daemon threads start() would spawn: a pump that spins
    # until _stop is set, and the real dispatch loop (drained by the None
    # sentinel stop() enqueues). Neither is the calling thread.
    def fake_pump() -> None:
        started.set()
        while not src._stop.is_set():
            src._stop.wait(0.01)

    src._thread = threading.Thread(target=fake_pump, daemon=True)
    src._dispatch_thread = threading.Thread(target=src._dispatch_loop, daemon=True)
    pump, dispatch = src._thread, src._dispatch_thread
    pump.start()
    dispatch.start()
    assert started.wait(2)

    src.stop()

    # Both threads have actually exited by the time stop() returns, and the
    # references are cleared so a fresh start() builds new ones.
    assert not pump.is_alive()
    assert not dispatch.is_alive()
    assert src._thread is None
    assert src._dispatch_thread is None


class _FakeItems:
    """A stand-in for a folder's ``Items`` collection: sortable + iterable, with
    **no** ``Restrict`` method — so a poll that reaches for the locale-sensitive
    date-string filter would AttributeError (caught → nothing emitted) and fail
    the test."""

    def __init__(self, mails: list) -> None:
        self._mails = list(mails)
        self.sorted_desc = False

    def Sort(self, field: str, descending: bool) -> None:  # noqa: N802 — COM name
        assert field == "[ReceivedTime]"
        self._mails.sort(key=lambda m: m.ReceivedTime, reverse=bool(descending))
        self.sorted_desc = bool(descending)

    def __iter__(self):
        return iter(self._mails)


def _fake_mail(entry_id: str, received) -> SimpleNamespace:
    return SimpleNamespace(
        EntryID=entry_id,
        Subject=f"s-{entry_id}",
        Body="body",
        SenderEmailAddress="x@y",
        To="t@y",
        ReceivedTime=received,
        Categories="",
        ConversationID="",
    )


def test_poll_emits_only_mail_after_high_water_locale_independent():
    """The poll fallback must forward only mail received *after* the high-water
    mark, comparing each item's own ReceivedTime in Python — never via
    ``Items.Restrict`` with a formatted date string (which Outlook re-parses under
    the OS regional settings, so a US ``MM/DD/YYYY`` stamp was misread on en-GB and
    re-emitted the whole inbox on every startup)."""
    from datetime import datetime, timedelta

    src = OutlookSource()
    cutoff = datetime(2026, 7, 2, 10, 0, 0)
    src._poll_high_water = cutoff.timestamp()

    older = _fake_mail("old", cutoff - timedelta(hours=1))
    newer1 = _fake_mail("new1", cutoff + timedelta(hours=1))
    newer2 = _fake_mail("new2", cutoff + timedelta(hours=2))
    # Deliberately unsorted; the poll Sorts newest-first itself.
    items = _FakeItems([older, newer2, older, newer1])
    src._bound = [(object(), items, "me@corp.com", "Inbox")]

    src._poll_all()

    assert items.sorted_desc  # it sorted newest-first before scanning
    subjects = []
    while not src._queue.empty():
        ev = src._queue.get_nowait()
        subjects.append(ev.payload["subject"])
    # Only the two newer mails, once each; the older one (and its dup) is skipped.
    assert sorted(subjects) == ["s-new1", "s-new2"]
    # High-water advanced to the newest mail seen.
    assert src._poll_high_water == (cutoff + timedelta(hours=2)).timestamp()


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


def test_send_draft_requires_entry_id(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")  # past the platform guard
    with pytest.raises(RuntimeError, match="no draft"):
        send_draft("")


def test_send_draft_off_windows():
    if sys.platform == "win32":
        return
    with pytest.raises(RuntimeError, match="Windows"):
        send_draft("ENT1")


# ── reply-draft composition (inline-response-proof, tested with COM fakes) ─────


class _FakeRecipients:
    def __init__(self):
        self.resolved = False

    def ResolveAll(self):
        self.resolved = True


class _FakeMail:
    """A standalone CreateItem draft — records what got set and saves cleanly."""

    def __init__(self):
        self.To = ""
        self.CC = ""
        self.Subject = ""
        self.Body = ""
        self.SentOnBehalfOfName = ""
        self.EntryID = ""
        self.Recipients = _FakeRecipients()
        self.saved = False

    def Save(self):
        self.saved = True
        self.EntryID = "DRAFT-NEW"


class _InlineReply:
    """What Reply() returns under inline-reply mode: reading props is fine, but
    the mutating methods raise the exact error the bug reported."""

    To = "requester@example.com"
    CC = "watcher@example.com"
    Subject = "RE: printer down"
    Body = "\n\n> original message text"

    def Save(self):
        raise RuntimeError(
            "This method can't be used with an inline response mail item"
        )

    def Send(self):
        raise RuntimeError(
            "This method can't be used with an inline response mail item"
        )


class _FakeOriginal:
    def Reply(self):
        return _InlineReply()


class _FakeApp:
    def __init__(self):
        self.mail = _FakeMail()

    def CreateItem(self, _kind):
        return self.mail


class _FakeNamespace:
    def GetItemFromID(self, _entry_id):
        return _FakeOriginal()


def test_compose_reply_draft_rebuilds_instead_of_saving_inline():
    app, ns = _FakeApp(), _FakeNamespace()
    out = _compose_reply_draft(app, ns, "ENT1", "Here is the fix.", None)

    mail = app.mail
    assert mail.saved  # saved the rebuilt CreateItem, never the inline reply
    assert out["entry_id"] == "DRAFT-NEW"
    assert out["to"] == "requester@example.com"
    assert out["subject"] == "RE: printer down"  # Outlook's RE: preserved
    # Agent body sits above the preserved quoted original.
    assert mail.Body == "Here is the fix.\n\n\n\n> original message text"
    assert mail.CC == "watcher@example.com"
    assert mail.Recipients.resolved


def test_compose_reply_draft_sets_send_as_mailbox():
    app, ns = _FakeApp(), _FakeNamespace()
    _compose_reply_draft(app, ns, "ENT1", "hi", "IT Support")
    assert app.mail.SentOnBehalfOfName == "IT Support"


# ── connection liveness / reconnect on Outlook restart ────────────────────────


class _DeadProp:
    """A COM proxy left pointing at a restarted Outlook: any read raises the
    classic RPC error."""

    @property
    def CurrentUser(self):
        raise RuntimeError("The RPC server is not available")


def test_connection_alive_true_for_live_namespace():
    ns = SimpleNamespace(CurrentUser=SimpleNamespace(Name="Ops Mailbox"))
    assert _connection_alive(ns) is True


def test_connection_alive_false_when_namespace_dead():
    # Outlook restarted → reading CurrentUser raises → probe reports dead.
    assert _connection_alive(_DeadProp()) is False


def test_connection_alive_false_when_user_read_dead():
    # CurrentUser resolves but the round-trip read of .Name fails.
    class _DeadUser:
        @property
        def Name(self):
            raise RuntimeError("The RPC server is not available")

    assert _connection_alive(SimpleNamespace(CurrentUser=_DeadUser())) is False


def test_run_pump_loop_returns_when_connection_drops():
    """The pump loop must end (so _pump can reconnect) the moment the connection
    probe fails — not spin forever against a dead proxy."""
    src = OutlookSource()
    src._POLL_INTERVAL = 0.0  # probe on the very first tick
    src._PUMP_INTERVAL = 0.0
    pumped = []
    fake_pythoncom = SimpleNamespace(PumpWaitingMessages=lambda: pumped.append(1))

    # Returns (rather than hanging) because the namespace probe reports dead.
    src._run_pump_loop(fake_pythoncom, _DeadProp())
    assert pumped  # it did at least one pump before detecting the drop


def test_run_pump_loop_exits_on_stop():
    """A healthy connection keeps looping; setting stop ends it cleanly."""
    src = OutlookSource()
    src._POLL_INTERVAL = 100.0  # never reach a poll/probe in the test window
    src._PUMP_INTERVAL = 0.0
    ns = SimpleNamespace(CurrentUser=SimpleNamespace(Name="Ops"))

    def pump():
        # Stop after the first pump so the loop exits via the stop check.
        src._stop.set()

    src._run_pump_loop(SimpleNamespace(PumpWaitingMessages=pump), ns)
    assert src._stop.is_set()


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


# ── agent-facing helpers (Outlook Classic tools) ──────────────────────────────


def test_outlook_available_off_windows():
    if sys.platform != "win32":
        assert outlook_available() is False


def test_office_version_root_parses_and_defaults():
    assert _office_version_root("16.0.12345.20000") == "16.0"
    assert _office_version_root("15.0.5000.1000") == "15.0"
    assert _office_version_root("") == "16.0"  # unknown → current Outlook family
    assert _office_version_root("not-a-version") == "16.0"


def test_calendar_options_key_path():
    key = _calendar_options_key("16.0")
    assert key == r"Software\Microsoft\Office\16.0\Outlook\Options\Calendar"


def test_extract_join_url_meetup_join():
    body = (
        "Hi team,\n\n"
        "________________________________________________________________________\n"
        "Microsoft Teams meeting\n"
        "Join on your computer or mobile app\n"
        "Click here to join the meeting<https://teams.microsoft.com/l/meetup-join/"
        "19%3ameeting_abc123%40thread.v2/0?context=%7b%22Tid%22%3a%22x%22%7d>\n"
        "Meeting ID: 123 456 789\n"
    )
    url = _extract_join_url(body)
    assert url.startswith("https://teams.microsoft.com/l/meetup-join/")
    # stops at the closing angle bracket, doesn't swallow trailing markup
    assert ">" not in url and url.endswith("%7d")


def test_extract_join_url_short_meet_form():
    body = "Join the meeting now https://teams.microsoft.com/meet/9988776655?p=AbCdEf"
    assert _extract_join_url(body) == (
        "https://teams.microsoft.com/meet/9988776655?p=AbCdEf"
    )


def test_extract_join_url_none_present():
    assert _extract_join_url("A plain personal block, no meeting.") == ""
    assert _extract_join_url("") == ""


def test_read_join_url_found_first_try_no_sleep():
    slept = []
    ns = SimpleNamespace(
        GetItemFromID=lambda eid: SimpleNamespace(
            Body="Join https://teams.microsoft.com/l/meetup-join/xyz here"
        )
    )
    url = _read_join_url(ns, "E1", attempts=4, delay=0.5, sleep=slept.append)
    assert url == "https://teams.microsoft.com/l/meetup-join/xyz"
    assert slept == []  # found immediately → never waited


def test_read_join_url_appears_on_retry():
    bodies = iter(["no link yet", "no link yet", "https://teams.microsoft.com/meet/42"])
    slept = []
    ns = SimpleNamespace(GetItemFromID=lambda eid: SimpleNamespace(Body=next(bodies)))
    url = _read_join_url(ns, "E1", attempts=4, delay=0.1, sleep=slept.append)
    assert url == "https://teams.microsoft.com/meet/42"
    assert slept == [0.1, 0.1]  # waited twice before the third read succeeded


def test_read_join_url_gives_up_empty():
    ns = SimpleNamespace(GetItemFromID=lambda eid: SimpleNamespace(Body="never"))
    assert _read_join_url(ns, "E1", attempts=3, delay=0, sleep=lambda _d: None) == ""


def test_read_join_url_blank_entry_id_short_circuits():
    called = []
    ns = SimpleNamespace(GetItemFromID=lambda eid: called.append(eid))
    assert _read_join_url(ns, "", sleep=lambda _d: None) == ""
    assert called == []  # never touched COM


def test_search_dasl_escapes_quotes_and_covers_subject_and_body():
    dasl = _search_dasl("o'brien")
    assert dasl.startswith("@SQL=(")
    assert "o''brien" in dasl  # single quote doubled
    assert "urn:schemas:httpmail:subject" in dasl
    assert "urn:schemas:httpmail:textdescription" in dasl


def test_mail_summary_shape_and_preview_cap():
    long_body = "word " * 200
    mail = SimpleNamespace(
        Subject="Hi",
        SenderName="Alice",
        SenderEmailAddress="a@b.com",
        ReceivedTime="2026-06-21 09:00",
        UnRead=True,
        EntryID="E1",
        Body=long_body,
    )
    row = _mail_summary(mail)
    assert row["subject"] == "Hi"
    assert row["from"] == "Alice"
    assert row["unread"] is True
    assert row["entry_id"] == "E1"
    assert len(row["preview"]) <= 281  # cap + ellipsis
    assert row["preview"].endswith("…")


def test_mail_summary_falls_back_to_sender_address():
    mail = SimpleNamespace(
        Subject="S", SenderName="", SenderEmailAddress="x@y.com", Body="b"
    )
    assert _mail_summary(mail)["from"] == "x@y.com"


def test_event_row_shape():
    from datetime import datetime

    it = SimpleNamespace(
        Subject="Standup",
        Start=datetime(2026, 6, 21, 9, 0),
        End=datetime(2026, 6, 21, 9, 30),
        Location="Room 1",
        Organizer="Bob",
        BusyStatus=2,
    )
    row = _event_row(it)
    assert row["subject"] == "Standup"
    assert row["location"] == "Room 1"
    assert row["busy"] is True


def test_read_and_compose_helpers_guard_non_windows():
    if sys.platform == "win32":
        return
    # Reads that degrade return None; the rest raise a clear Windows-only error.
    assert upcoming_events() is None
    for fn in (
        lambda: list_messages(),
        lambda: save_new_draft("a@b.com", "s", "b"),
        lambda: send_new_mail("a@b.com", "s", "b"),
    ):
        with pytest.raises(RuntimeError):
            fn()


# ── create_reminder (first create-calendar-item helper) ───────────────────────


class _FakeItem:
    def __init__(self, entry_id: str) -> None:
        self.Subject = ""
        self.Start = None
        self.Duration = None
        self.ReminderSet = False
        self.ReminderMinutesBeforeStart = None
        self.StartDate = None
        self.DueDate = None
        self.ReminderTime = None
        self.TaskStartDate = None
        self.TaskDueDate = None
        self.marked_as_task = None
        self.EntryID = entry_id
        self.saved = False

    def MarkAsTask(self, interval: int) -> None:  # noqa: N802 — COM name
        self.marked_as_task = interval

    def Save(self) -> None:  # noqa: N802 — COM name
        self.saved = True


class _FakeReminderApp:
    """A fake Outlook.Application. ``fail_kinds`` is the set of olItemType ints
    whose CreateItem raises — to exercise the appointment→task→mail fallback."""

    def __init__(self, fail_kinds: frozenset[int] = frozenset()) -> None:
        self.fail_kinds = fail_kinds
        self.created: dict[int, _FakeItem] = {}

    def CreateItem(self, kind: int):  # noqa: N802 — COM name
        if kind in self.fail_kinds:
            raise RuntimeError(f"CreateItem({kind}) failed")
        item = _FakeItem({0: "MAIL1", 1: "APPT1", 3: "TASK1"}[kind])
        self.created[kind] = item
        return item


def test_build_reminder_item_prefers_appointment():
    from datetime import datetime

    app = _FakeReminderApp()
    when = datetime(2026, 6, 21, 15, 0)
    res = _build_reminder_item(app, "Break over", when)
    appt = app.created[1]
    assert appt.saved is True and appt.ReminderSet is True
    # Wall-clock string (not a datetime) so pywin32 can't UTC-shift the fire time.
    assert appt.ReminderMinutesBeforeStart == 0 and appt.Start == "2026-06-21 15:00:00"
    assert res == {"subject": "Break over", "entry_id": "APPT1", "kind": "appointment"}
    assert 3 not in app.created and 0 not in app.created  # no fallback needed


def test_build_reminder_item_falls_back_to_task():
    from datetime import datetime

    app = _FakeReminderApp(fail_kinds=frozenset({1}))  # no calendar store
    when = datetime(2026, 6, 21, 15, 0)
    res = _build_reminder_item(app, "Break over", when)
    task = app.created[3]
    assert task.saved is True and task.ReminderSet is True
    assert task.ReminderTime == "2026-06-21 15:00:00"  # wall-clock string, no shift
    assert (
        task.StartDate == "2026-06-21 15:00:00"
        and task.DueDate == "2026-06-21 15:00:00"
    )
    assert res["kind"] == "task" and res["entry_id"] == "TASK1"


def test_build_reminder_item_falls_back_to_flagged_mail():
    # The IMAP/POP case: no calendar AND no tasks store, but mail works.
    from datetime import datetime

    app = _FakeReminderApp(fail_kinds=frozenset({1, 3}))
    when = datetime(2026, 6, 21, 15, 0)
    res = _build_reminder_item(app, "Break over", when)
    mail = app.created[0]
    assert mail.saved is True and mail.ReminderSet is True
    assert mail.ReminderTime == "2026-06-21 15:00:00"  # wall-clock string, no shift
    assert mail.marked_as_task == 1  # flagged as a to-do
    assert res["kind"] == "flagged_mail" and res["entry_id"] == "MAIL1"


def test_build_reminder_item_raises_when_all_fail():
    from datetime import datetime

    app = _FakeReminderApp(fail_kinds=frozenset({0, 1, 3}))
    with pytest.raises(RuntimeError, match="could not create any reminder"):
        _build_reminder_item(app, "Break over", datetime(2026, 6, 21, 15, 0))


def test_create_reminder_requires_windows():
    if sys.platform == "win32":
        return
    from datetime import datetime

    with pytest.raises(RuntimeError, match="Windows"):
        create_reminder("x", datetime.now())
