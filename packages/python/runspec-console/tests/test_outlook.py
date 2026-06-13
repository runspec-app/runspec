"""
Tests for the Outlook event source (sources/outlook.py).

The payload extractors are pure, so they're tested with plain fakes on any
platform. The COM pump can't run off-Windows, so we test the platform/availability
guards and that start()/stop() are safe no-ops here.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from types import SimpleNamespace

from runspec_console.events import Event, EventSource, available_sources, create_source
import pytest

from runspec_console.sources.outlook import (
    OutlookSource,
    _PR_TRANSPORT_HEADERS,
    _PS_INTERNET_HEADERS,
    _mail_payload,
    _named_header,
    _received_ts_from_headers,
    _recipients_from_headers,
    _recent_filter,
    _recent_items,
    _reminder_payload,
    _shared_inbox,
    _split_categories,
    read_mailbox_items,
    save_reply_draft,
    send_claim_marker,
    send_draft,
)


# ── payload extraction ────────────────────────────────────────────────────────


def _prop_accessor(*, transport: str = "", named: dict[str, str] | None = None):
    """A fake COM PropertyAccessor: the transport-header blob for
    PR_TRANSPORT_MESSAGE_HEADERS, a value per PS_INTERNET_HEADERS named property,
    and "" otherwise — so a test can carry a header on either path independently
    (real mail has the marker on one or the other, not "blob for every property")."""
    named = named or {}

    def get(prop):
        if prop == _PR_TRANSPORT_HEADERS:
            return transport
        for n, v in named.items():
            if prop == _PS_INTERNET_HEADERS + n:
                return v
        return ""

    return SimpleNamespace(GetProperty=get)


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
        "received": "2026-06-09 10:00",
        "categories": ["Red Category", "Blue Category"],
        "entry_id": "ENT123",
        "conversation_id": "CONV789",
        # No PropertyAccessor on the fake → no transport headers → no header fields.
        "recipients": [],
        "received_ts": 0.0,
        "message_id": "",
        "claim_marker": "",
    }


def test_mail_payload_conversation_id_empty_when_missing():
    # A MailItem without ConversationID must not raise — the bridge then folds on
    # a normalised subject instead.
    assert _mail_payload(SimpleNamespace(Subject="hi"))["conversation_id"] == ""


def test_mail_payload_recipients_from_transport_headers():
    # The real SMTP address comes from the transport header block, not the To
    # display name — read via the COM PropertyAccessor (faked here).
    blob = "To: IT Support <it-support@corp.com>\r\nSubject: hi\r\n"
    mail = SimpleNamespace(
        Subject="hi",
        To="IT Support",  # display name only — no address here
        PropertyAccessor=_prop_accessor(transport=blob),
    )
    assert _mail_payload(mail)["recipients"] == ["it-support@corp.com"]


# ── transport-header recipient parsing (pure) ─────────────────────────────────


def test_recipients_from_headers_to_and_cc():
    blob = (
        "To: IT Support <it-support@corp.com>, other@x.com\r\n"
        "Cc: Boss <boss@corp.com>\r\nSubject: x\r\n"
    )
    assert _recipients_from_headers(blob) == [
        "it-support@corp.com",
        "other@x.com",
        "boss@corp.com",
    ]


def test_recipients_from_headers_envelope_delivery_for_alias():
    # An alias hides the mailbox from To; Delivered-To and the Received `for`
    # clause still name the real envelope recipient.
    blob = (
        "To: help@corp.com\r\n"
        "Delivered-To: it-support@corp.com\r\n"
        "Received: from mx.corp.com by store.corp.com for <it-support@corp.com>;"
        " Tue, 9 Jun 2026 10:00:00 +0000\r\n"
        "Subject: x\r\n"
    )
    got = _recipients_from_headers(blob)
    assert "help@corp.com" in got and "it-support@corp.com" in got


def test_recipients_from_headers_dedupes_and_lowercases():
    blob = "To: IT-Support@Corp.com\r\nCc: it-support@corp.com\r\nSubject: x\r\n"
    assert _recipients_from_headers(blob) == ["it-support@corp.com"]


def test_recipients_from_headers_empty_blob():
    assert _recipients_from_headers("") == []
    assert _recipients_from_headers("   ") == []


def test_received_ts_from_headers_parses_date():
    blob = "Date: Tue, 09 Jun 2026 10:00:00 +0000\r\nSubject: x\r\n"
    assert _received_ts_from_headers(blob) == 1780999200.0  # 2026-06-09T10:00:00Z


def test_received_ts_from_headers_absent_or_bad():
    assert _received_ts_from_headers("Subject: x\r\n") == 0.0
    assert _received_ts_from_headers("Date: not-a-date\r\n") == 0.0


def test_mail_payload_marker_and_ordering_from_headers():
    blob = (
        "To: it-support@corp.com\r\n"
        "Date: Tue, 09 Jun 2026 10:00:00 +0000\r\n"
        "Message-ID: <ack-1@corp.com>\r\n"
        "X-Runspec-Claim: it-support/CONV1/ana@web01/claim\r\n"
        "Subject: RE: help\r\n"
    )
    mail = SimpleNamespace(
        Subject="RE: help",
        PropertyAccessor=_prop_accessor(transport=blob),
    )
    p = _mail_payload(mail)
    assert p["claim_marker"] == "it-support/CONV1/ana@web01/claim"
    assert p["message_id"] == "<ack-1@corp.com>"
    assert p["received_ts"] == 1780999200.0


def test_named_header_reads_ps_internet_property():
    mail = SimpleNamespace(
        PropertyAccessor=_prop_accessor(
            named={"x-runspec-claim": "it-support/CONV1/me/claim"}
        )
    )
    assert _named_header(mail, "x-runspec-claim") == "it-support/CONV1/me/claim"
    assert _named_header(SimpleNamespace(), "x-runspec-claim") == ""  # no accessor → ""


def test_mail_payload_marker_from_named_property_for_internal_mail():
    # Internal same-org mail: PR_TRANSPORT_MESSAGE_HEADERS is empty, but the marker
    # rides the named property — it must still be detected, else the ack loops as a
    # fresh requester email.
    mail = SimpleNamespace(
        Subject="RE: help",
        PropertyAccessor=_prop_accessor(
            named={"x-runspec-claim": "it-support/CONV1/ana@web01/claim"}
        ),
    )
    assert _mail_payload(mail)["claim_marker"] == "it-support/CONV1/ana@web01/claim"


def test_mail_payload_marker_prefers_named_property_over_transport_blob():
    # When both carry it (externally-routed mail), the named property wins — the
    # canonical source we set.
    mail = SimpleNamespace(
        Subject="RE: help",
        PropertyAccessor=_prop_accessor(
            transport="X-Runspec-Claim: from-blob\r\nSubject: x\r\n",
            named={"x-runspec-claim": "from-named"},
        ),
    )
    assert _mail_payload(mail)["claim_marker"] == "from-named"


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


# ── shared-mailbox Inbox resolution (delegate / account / auto-mapped store) ──
#
# _shared_inbox operates on the COM namespace object, so the strategy selection is
# testable off-Windows with fakes. E_NOTIMPL (0x80004001) is the real-world signal
# that the mailbox is mounted as its own store rather than delegated.

INBOX = SimpleNamespace(tag="inbox")  # the folder a successful lookup returns


class _Folders:
    """A fake Folders collection supporting both iteration and ['Inbox']."""

    def __init__(self, items=(), by_name=None):
        self._items = list(items)
        self._by_name = by_name or {}

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, key):
        return self._by_name[key]  # KeyError if absent — mirrors COM


def _ns(*, delegate=None, accounts=(), folders=()):
    """A fake MAPI namespace. ``delegate`` is the folder GetSharedDefaultFolder
    returns; a string instead makes it raise that as a COM error (E_NOTIMPL)."""

    def get_shared(recipient, folder_type):
        if isinstance(delegate, str):
            raise OSError(delegate)
        if delegate is None:
            raise OSError("(-2147467263, 'Not implemented', None, None)")  # E_NOTIMPL
        return delegate

    return SimpleNamespace(
        CreateRecipient=lambda addr: SimpleNamespace(
            Resolve=lambda: None, Resolved=True
        ),
        GetSharedDefaultFolder=get_shared,
        Accounts=list(accounts),
        Folders=list(folders),
    )


def test_shared_inbox_delegate_path():
    ns = _ns(delegate=INBOX)
    assert _shared_inbox(ns, "it-support@corp.com") is INBOX


def test_shared_inbox_falls_back_to_account_on_e_notimpl():
    # GetSharedDefaultFolder raises E_NOTIMPL → match the account by SMTP.
    acct = SimpleNamespace(
        SmtpAddress="IT-Support@Corp.com",  # case-insensitive match
        DeliveryStore=SimpleNamespace(GetDefaultFolder=lambda ft: INBOX),
    )
    ns = _ns(delegate=None, accounts=[acct])
    assert _shared_inbox(ns, "it-support@corp.com") is INBOX


def test_shared_inbox_falls_back_to_auto_mapped_store():
    # No delegate, no matching account → a top-level store folder named for the box.
    store = SimpleNamespace(
        Name="it-support", Folders=_Folders(by_name={"Inbox": INBOX})
    )
    ns = _ns(delegate=None, accounts=[], folders=[store])
    assert _shared_inbox(ns, "it-support@corp.com") is INBOX


def test_shared_inbox_returns_none_when_nothing_matches():
    other = SimpleNamespace(Name="someone-else", Folders=_Folders(by_name={}))
    ns = _ns(delegate=None, accounts=[], folders=[other])
    assert _shared_inbox(ns, "it-support@corp.com") is None


# ── bounded backlog read (don't sort/enumerate a massive shared mailbox) ──────


class _Items:
    """A fake Items collection that records Restrict calls."""

    def __init__(self, restrict_result=None, raises=False):
        self.restrict_calls = []
        self._result = restrict_result
        self._raises = raises

    def Restrict(self, filt):  # noqa: N802 — COM name
        self.restrict_calls.append(filt)
        if self._raises:
            raise OSError("Restrict not supported on this store")
        return self._result


def test_recent_filter_is_locale_independent_dasl():
    f = _recent_filter(datetime(2026, 6, 13, 12, 0), 14)
    assert f.startswith('@SQL="urn:schemas:httpmail:datereceived" >=')
    assert "2026-05-30 12:00" in f  # 14 days before the reference instant (ISO)


def test_recent_items_restricts_to_window():
    restricted = object()
    items = _Items(restrict_result=restricted)
    out = _recent_items(SimpleNamespace(Items=items), datetime(2026, 6, 13, 12, 0), 14)
    assert out is restricted
    assert items.restrict_calls and "datereceived" in items.restrict_calls[0]


def test_recent_items_no_window_returns_all_without_restrict():
    items = _Items()
    out = _recent_items(SimpleNamespace(Items=items), datetime(2026, 6, 13), 0)
    assert out is items
    assert items.restrict_calls == []  # days<=0 → never narrows


def test_recent_items_falls_back_when_restrict_unsupported():
    items = _Items(raises=True)
    out = _recent_items(SimpleNamespace(Items=items), datetime(2026, 6, 13), 14)
    assert out is items  # a rejecting store → unfiltered, not a failed scan


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


def test_send_draft_requires_entry_id(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")  # past the platform guard
    with pytest.raises(RuntimeError, match="no draft"):
        send_draft("")


def test_send_draft_off_windows():
    if sys.platform == "win32":
        return
    with pytest.raises(RuntimeError, match="Windows"):
        send_draft("ENT1")


# ── claim marker send guards ──────────────────────────────────────────────────


def test_send_claim_marker_requires_entry_id(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")  # past the platform guard
    with pytest.raises(RuntimeError, match="no source message"):
        send_claim_marker("", "it-support/CONV1/ana/claim", "it-support@corp.com")


def test_send_claim_marker_requires_queue_address(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="shared-mailbox address"):
        send_claim_marker("ENT1", "it-support/CONV1/ana/claim", "")


def test_send_claim_marker_off_windows():
    if sys.platform == "win32":
        return
    with pytest.raises(RuntimeError, match="Windows"):
        send_claim_marker("ENT1", "m", "it-support@corp.com")


def test_read_mailbox_items_off_windows_or_empty_address():
    # Best-effort: returns [] off-Windows or with no mailbox address (never raises).
    assert read_mailbox_items("") == []
    if sys.platform != "win32":
        assert read_mailbox_items("it-support@corp.com") == []


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
