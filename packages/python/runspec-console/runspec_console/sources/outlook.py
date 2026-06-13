"""
sources/outlook.py — the Outlook COM event source (Windows).

Source #1 for triggers. The desktop Outlook client is already signed in with the
org's SSO session, so subscribing to its COM events rides that session and
sidesteps the Conditional-Access wall that blocks the MSAL device-code flow.
``pywin32`` is already a console dependency, so this needs no new package.

Two event types, both **forward-only** (A1) — they fire for occurrences that
happen while we're running; we never scan the inbox / reminders for backlog:

* ``new_mail`` — ``Application.NewMailEx`` → resolve the EntryID to a MailItem
* ``reminder`` — ``Application.Reminder``

COM event sinks need a thread that ``CoInitialize``s, binds the sink with
``DispatchWithEvents``, and runs a message pump (``PumpMessages``). That all
lives on this source's own daemon thread; ``stop()`` posts ``WM_QUIT`` to it.

The sink does **not** call the engine's ``emit`` directly — that would route the
event (and run the working-hours rota's *calendar* COM read) re-entrantly inside
the COM callback on the STA pump thread, which breaks the calendar read. Instead
each event is marshalled onto a queue and a separate dispatcher thread calls
``emit`` (and thus any downstream COM read) off the pump thread.

The pure payload-extraction helpers (``_mail_payload`` / ``_reminder_payload``)
take any duck-typed Outlook item, so they're unit-tested on any platform with
plain fakes — the win32 imports stay inside the pump thread.

See docs/design/console-triggers.md (§4.1).
"""

from __future__ import annotations

import email.parser
import email.utils
import logging
import queue
import re
import sys
import threading
from datetime import datetime, timedelta
from typing import Any

from ..events import EmitFn, Event, register_source

logger = logging.getLogger("runspec.console.triggers.outlook")

# Cap a mail body so a pathological message can't bloat the event/card. The
# agent turn (step 4) can ask Outlook for more if it ever needs the full text.
_BODY_CAP = 20_000


# ── pure payload extraction (platform-independent, unit-tested with fakes) ─────


def _attr(obj: Any, name: str, default: Any = "") -> Any:
    """getattr that swallows COM access errors and missing attributes."""
    try:
        val = getattr(obj, name)
    except Exception:
        return default
    return default if val is None else val


def _str(val: Any) -> str:
    return "" if val is None else str(val)


def _split_categories(val: Any) -> list[str]:
    """Outlook ``Categories`` is a comma-joined string → list of names."""
    text = _str(val).strip()
    if not text:
        return []
    return [c.strip() for c in text.split(",") if c.strip()]


# MAPI PR_TRANSPORT_MESSAGE_HEADERS — the raw RFC 5322 header block as delivered
# by transport. Read via the COM PropertyAccessor; isolated in _transport_headers
# so the parser below stays a pure, platform-independent function.
_PR_TRANSPORT_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001F"

# `Received: … for <addr>` names the envelope recipient that caused delivery —
# the mailbox address even when an alias / distribution list hides it from To/Cc.
_RECEIVED_FOR = re.compile(r"\bfor\s+<([^>]+)>")


def _transport_headers(mail: Any) -> str:
    """The raw transport header block via the MAPI ``PropertyAccessor``, or ``""``
    when unavailable — off-Windows, a COM error, or internal MAPI mail that never
    traversed SMTP transport (so it carries no header block). Kept duck-typed and
    failure-swallowing so ``_mail_payload`` stays pure and testable with fakes."""
    try:
        return _str(mail.PropertyAccessor.GetProperty(_PR_TRANSPORT_HEADERS))
    except Exception:
        return ""


def _recipients_from_headers(blob: str) -> list[str]:
    """Real SMTP recipient addresses parsed from a raw transport-header block,
    lowercased and de-duplicated (order preserved). Reads ``To``/``Cc`` *and* the
    envelope-delivery headers (``Delivered-To`` / ``X-Original-To`` / the
    ``Received: … for <addr>`` trace) so a shared mailbox reached via an alias or
    distribution list is still named. ``[]`` when there is no header block — the
    caller then falls back to the display ``To`` string. Pure stdlib; no COM."""
    if not blob.strip():
        return []
    try:
        msg = email.parser.Parser().parsestr(blob, headersonly=True)
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(addr: str) -> None:
        normalised = addr.strip().lower()
        if normalised and normalised not in seen:
            seen.add(normalised)
            out.append(normalised)

    for field in ("To", "Cc", "Delivered-To", "X-Original-To"):
        for _name, addr in email.utils.getaddresses(msg.get_all(field, [])):
            add(addr)
    for received in msg.get_all("Received", []):
        match = _RECEIVED_FOR.search(str(received))
        if match:
            add(match.group(1))
    return out


def _header_field(blob: str, name: str) -> str:
    """A single header value from a transport-header block ("" when absent).
    Case-insensitive per RFC 5322. Pure stdlib."""
    if not blob.strip():
        return ""
    try:
        msg = email.parser.Parser().parsestr(blob, headersonly=True)
    except Exception:
        return ""
    return _str(msg.get(name)).strip()


def _named_header(mail: Any, name: str) -> str:
    """Read a custom internet header carried as a **PS_INTERNET_HEADERS named
    property** directly off the MailItem — the same property :func:`send_claim_marker`
    sets. This is the reliable read for **internal same-org mail**, whose
    ``PR_TRANSPORT_MESSAGE_HEADERS`` block is empty (it never traversed SMTP
    transport), so parsing the transport blob can't see the header — the marker on
    an internal claim ack would otherwise be missed and the ack mistaken for a new
    requester email. ``name`` is the lowercase header (e.g. ``x-runspec-claim``);
    ``""`` when absent / off-COM. Duck-typed + failure-swallowing, like
    :func:`_transport_headers`."""
    try:
        return _str(mail.PropertyAccessor.GetProperty(_PS_INTERNET_HEADERS + name))
    except Exception:
        return ""


def _received_ts_from_headers(blob: str) -> float:
    """The originating ``Date`` header as a POSIX timestamp (0.0 when absent /
    unparseable). Gives the mailbox-claim fold a stable *server-ish* order key —
    timezone-aware, comparable across senders — rather than a local clock."""
    date = _header_field(blob, "Date")
    if not date:
        return 0.0
    try:
        return email.utils.parsedate_to_datetime(date).timestamp()
    except Exception:
        return 0.0


def _received_ts(mail: Any, headers: str) -> float:
    """A sortable receive timestamp for the mailbox-claim fold. Prefers the
    store's ``ReceivedTime`` (≈ the server ``receivedDateTime``, set on every
    received item — including internal mail that carries no transport headers),
    falling back to the originating ``Date`` header. 0.0 when neither is usable."""
    received = _to_dt(_attr(mail, "ReceivedTime", None))
    if received is not None:
        try:
            return received.timestamp()
        except Exception:
            pass
    return _received_ts_from_headers(headers)


def _mail_payload(mail: Any) -> dict[str, Any]:
    """Extract the ``new_mail`` payload from a MailItem-like object."""
    sender = _attr(mail, "SenderEmailAddress") or _attr(mail, "SenderName")
    body = _str(_attr(mail, "Body"))
    if len(body) > _BODY_CAP:
        body = body[:_BODY_CAP] + "…"
    # Read the transport header block once (one COM call) and derive every
    # header-sourced field from it: real recipients, the ordering metadata for the
    # mailbox-claim fold, and the X-Runspec-Claim marker on an ack email.
    headers = _transport_headers(mail)
    return {
        "from": _str(sender),
        "to": _str(_attr(mail, "To")),
        "subject": _str(_attr(mail, "Subject")),
        "body": body,
        "received": _str(_attr(mail, "ReceivedTime")),
        "categories": _split_categories(_attr(mail, "Categories")),
        "entry_id": _str(_attr(mail, "EntryID")),
        # ConversationID threads a customer email and its replies onto one queue
        # card (the email analog of a chat conversation room); empty when Outlook
        # can't supply it, in which case the bridge folds on a normalised subject.
        "conversation_id": _str(_attr(mail, "ConversationID")),
        # Real SMTP recipients from the transport headers — the authoritative
        # signal for which shared mailbox an email belongs to (To shows display
        # names, not addresses). Empty for internal mail with no header block.
        "recipients": _recipients_from_headers(headers),
        # Ordering metadata for the mailbox-claim fold (server order, not local
        # clocks): the receive time + the Message-ID tiebreak.
        "received_ts": _received_ts(mail, headers),
        "message_id": _header_field(headers, "Message-ID"),
        # The X-Runspec-Claim marker value on a claim/lifecycle ack email (""
        # for an ordinary message); the bridge parses it via mailbox_claim. Read
        # the PS_INTERNET_HEADERS named property first (the canonical source we
        # set — survives internal same-org delivery, where the transport blob is
        # empty), falling back to the transport header for externally-routed mail.
        "claim_marker": (
            _named_header(mail, "x-runspec-claim")
            or _header_field(headers, "X-Runspec-Claim")
        ),
    }


def _reminder_payload(reminder: Any) -> dict[str, Any]:
    """Extract the ``reminder`` payload. A ``Reminder`` wraps its underlying
    item in ``.Item``; fall back to the reminder object itself."""
    item = _attr(reminder, "Item", None)
    obj = item if item is not None else reminder
    return {
        "subject": _str(_attr(obj, "Subject")),
        "start": _str(_attr(obj, "Start")),
        "location": _str(_attr(obj, "Location")),
        # olNonMeeting == 0; any non-zero MeetingStatus means it's a meeting.
        "is_meeting": bool(_attr(obj, "MeetingStatus", 0)),
        "organizer": _str(_attr(obj, "Organizer")),
    }


# ── COM event sink ────────────────────────────────────────────────────────────
#
# Used as the user-event class for win32com.client.DispatchWithEvents. pywin32
# builds a combined object (Outlook Application + this class); its On* methods
# run with `self` bound to that combined instance, so `self.GetNamespace(...)`
# reaches the live Application and `self.emit` is the callback we attach after
# construction. No win32 import is needed *here* — only attribute access.


class _OutlookEventSink:
    """Event handlers bound onto the dispatched Outlook Application."""

    def OnNewMailEx(self, entry_id_collection: Any) -> None:  # noqa: N802 — COM name
        emit = getattr(self, "emit", None)
        if emit is None:
            return
        try:
            namespace = self.GetNamespace("MAPI")  # type: ignore[attr-defined]
        except Exception:
            logger.exception("outlook: GetNamespace failed; dropping new mail")
            return
        for entry_id in _str(entry_id_collection).split(","):
            entry_id = entry_id.strip()
            if not entry_id:
                continue
            try:
                item = namespace.GetItemFromID(entry_id)
            except Exception:
                logger.exception("outlook: could not resolve EntryID %s", entry_id)
                continue
            payload = _mail_payload(item)
            payload["entry_id"] = entry_id  # authoritative id from the event
            _safe_emit(emit, Event.new("outlook", "new_mail", payload))

    def OnReminder(self, item: Any) -> None:  # noqa: N802 — COM name
        emit = getattr(self, "emit", None)
        if emit is None:
            return
        _safe_emit(emit, Event.new("outlook", "reminder", _reminder_payload(item)))


def _safe_emit(emit: EmitFn, event: Event) -> None:
    try:
        emit(event)
    except Exception:
        logger.exception("outlook: emit failed for %s", event.type)


# ── the source ────────────────────────────────────────────────────────────────


class OutlookSource:
    """Forward-only Outlook event source. Satisfies the ``EventSource`` protocol."""

    id = "outlook"

    def __init__(self) -> None:
        self._emit: EmitFn | None = None
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._app: Any = None  # keep the dispatched sink alive while pumping
        # Events captured on the COM pump thread are handed to a dispatcher
        # thread through this queue, so routing/emit never runs re-entrantly
        # inside a COM callback (see module docstring). A None is the stop sentinel.
        self._queue: queue.Queue[Event | None] = queue.Queue()
        self._dispatch_thread: threading.Thread | None = None

    def available(self) -> tuple[bool, str]:
        if sys.platform != "win32":
            return (False, "Outlook source requires Windows")
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except Exception:
            return (False, "pywin32 is not installed")
        return (True, "")

    def event_types(self) -> list[str]:
        return ["new_mail", "reminder"]

    def start(self, emit: EmitFn) -> None:
        ok, reason = self.available()
        if not ok:
            logger.info("outlook source not started: %s", reason)
            return
        if self._thread is not None:
            return  # already started
        self._emit = emit
        # Fresh queue per start so a previous run's stop sentinel can't linger.
        self._queue = queue.Queue()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, name="outlook-dispatch", daemon=True
        )
        self._dispatch_thread.start()
        self._thread = threading.Thread(
            target=self._pump, name="outlook-com", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        tid = self._thread_id
        if tid is not None:
            try:
                import win32api
                import win32con

                win32api.PostThreadMessage(tid, win32con.WM_QUIT, 0, 0)
            except Exception:
                logger.exception("outlook: failed to signal pump thread to quit")
        self._thread_id = None
        self._thread = None
        self._app = None
        if self._dispatch_thread is not None:
            self._queue.put(None)  # wake the dispatcher so it exits
            self._dispatch_thread = None

    def _enqueue(self, event: Event) -> None:
        """Hand an event from the COM pump thread to the dispatcher thread.

        Bound onto the sink as ``app.emit``; the COM handlers call this instead
        of the engine's emit, so routing happens off the pump thread.
        """
        self._queue.put(event)

    def _dispatch_loop(self) -> None:
        """Drain queued events and route them via the real emit, off the pump
        thread, until ``stop()`` enqueues the None sentinel."""
        while True:
            event = self._queue.get()
            if event is None:
                return
            if self._emit is not None:
                _safe_emit(self._emit, event)

    def _pump(self) -> None:
        """Own a COM apartment, bind the Outlook event sink, and pump messages
        until ``stop()`` posts WM_QUIT. Runs on the source's daemon thread."""
        import pythoncom
        import win32api
        import win32com.client

        pythoncom.CoInitialize()
        try:
            self._thread_id = win32api.GetCurrentThreadId()
            try:
                app = win32com.client.DispatchWithEvents(
                    "Outlook.Application", _OutlookEventSink
                )
            except Exception:
                logger.exception(
                    "outlook: could not attach to Outlook (is it installed/running?)"
                )
                return
            # Marshal events off this STA pump thread via the queue, never call
            # the engine emit (and its calendar COM read) directly from here.
            app.emit = self._enqueue
            self._app = app  # hold a ref so the sink isn't GC'd mid-pump
            logger.info("outlook source: listening for new mail and reminders")
            pythoncom.PumpMessages()
        finally:
            pythoncom.CoUninitialize()


def save_reply_draft(
    entry_id: str, body: str, from_mailbox: str | None = None
) -> dict[str, str]:
    """Create a reply to the message ``entry_id`` and save it to Drafts.

    The agent's ``body`` is placed above Outlook's quoted original. The draft is
    **saved, never sent** — the user reviews it and sends from Outlook *or* via
    the card's Send button (see :func:`send_draft`). Returns a small summary
    (to / subject / ``entry_id`` of the saved draft, which the Send button uses).
    Raises on any failure so the caller can report it on the card. Windows-only;
    the win32 imports stay inside this function.

    ``from_mailbox`` sends the reply **as that shared mailbox** (sets
    ``SentOnBehalfOfName``), so a help-desk requester sees the queue identity
    rather than the operator. Whether it lands as send-as ("IT Support") or
    send-on-behalf ("X on behalf of IT Support") is decided by the Exchange
    permission granted on the mailbox — the COM call is the same.
    """
    if sys.platform != "win32":
        raise RuntimeError("draft_reply requires Windows (Outlook COM)")
    if not entry_id:
        raise RuntimeError("draft_reply has no source message to reply to")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        namespace = app.GetNamespace("MAPI")
        original = namespace.GetItemFromID(entry_id)
        reply = original.Reply()
        existing = _str(_attr(reply, "Body"))
        reply.Body = body + ("\n\n" + existing if existing else "")
        if from_mailbox:
            reply.SentOnBehalfOfName = from_mailbox  # reply as the shared mailbox
        reply.Save()  # → Drafts; does NOT send
        return {
            "to": _str(_attr(reply, "To")),
            "subject": _str(_attr(reply, "Subject")),
            # EntryID is assigned once the draft is saved; the card's Send button
            # reopens the draft by this id and sends it (human-initiated).
            "entry_id": _str(_attr(reply, "EntryID")),
        }
    finally:
        pythoncom.CoUninitialize()


def send_draft(entry_id: str) -> dict[str, str]:
    """Send a previously-saved draft by its ``entry_id`` (the card's Send button).

    Reopens the draft saved by :func:`save_reply_draft` and sends it. This is the
    one place that actually sends mail, and it is only ever reached from an
    explicit human click — the agent never calls it. Returns a small summary
    (to / subject); raises on failure. Windows-only.
    """
    if sys.platform != "win32":
        raise RuntimeError("send requires Windows (Outlook COM)")
    if not entry_id:
        raise RuntimeError("send has no draft to send")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        namespace = app.GetNamespace("MAPI")
        draft = namespace.GetItemFromID(entry_id)
        summary = {
            "to": _str(_attr(draft, "To")),
            "subject": _str(_attr(draft, "Subject")),
        }
        draft.Send()
        return summary
    finally:
        pythoncom.CoUninitialize()


# ── mailbox-claim marker send ─────────────────────────────────────────────────
#
# Claiming a shared-mailbox conversation (or changing its lifecycle) sends a tiny
# marker email *to the shared mailbox itself*, threaded on the conversation, with
# the marker as an X-Runspec-Claim internet header. Every staffing console sees it
# land and folds it (mailbox_claim.fold_conversation); first ack wins. The marker
# rides a PS_INTERNET_HEADERS named property, the documented way to add a custom
# internet header to outbound Outlook mail so Exchange transport emits it.

# PS_INTERNET_HEADERS namespace — a string named property under this GUID becomes
# the internet header of the same name on the sent message.
_PS_INTERNET_HEADERS = (
    "http://schemas.microsoft.com/mapi/string/{00020386-0000-0000-C000-000000000046}/"
)


def send_claim_marker(
    entry_id: str, marker_value: str, queue_address: str
) -> dict[str, str]:
    """Send a claim/lifecycle marker email: a reply **threaded** on the
    conversation (so it inherits the ConversationID) but re-addressed to the
    shared mailbox ``queue_address`` only — never the requester, who must not see
    an internal ack (helpdesk-channels.md, rule 3). The marker rides the
    ``X-Runspec-Claim`` header set via the PS_INTERNET_HEADERS named property.

    Ownership is *not* asserted here — it changes only when this ack lands back in
    the mailbox and folds (read-after-write). Returns a small summary; raises on
    failure so the caller can report it on the card. Windows-only; COM stays here.
    """
    if sys.platform != "win32":
        raise RuntimeError("claim requires Windows (Outlook COM)")
    if not entry_id:
        raise RuntimeError("claim has no source message to thread on")
    if not queue_address:
        raise RuntimeError("claim has no shared-mailbox address to send to")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        namespace = app.GetNamespace("MAPI")
        original = namespace.GetItemFromID(entry_id)
        reply = original.Reply()  # inherits the ConversationID → stays threaded
        # Re-address to the shared mailbox alone — drop the requester Reply() added.
        while reply.Recipients.Count > 0:
            reply.Recipients.Remove(1)
        reply.Recipients.Add(queue_address)
        reply.Recipients.ResolveAll()
        reply.Body = (
            "runspec claim marker (automated) — see the X-Runspec-Claim header."
        )
        reply.PropertyAccessor.SetProperty(
            _PS_INTERNET_HEADERS + "x-runspec-claim", marker_value
        )
        reply.Send()
        return {"to": queue_address, "marker": marker_value}
    finally:
        pythoncom.CoUninitialize()


# ── shared-mailbox backlog scan ───────────────────────────────────────────────
#
# NewMailEx is forward-only — it misses mail (and claim/lifecycle acks) that
# arrived while the console was down. A bounded scan of the shared mailbox's Inbox
# on startup replays recent items through the same fold, so a freshly-started
# console reconstructs in-flight conversations and their current claim/state.


def _shared_inbox(namespace: Any, mailbox_address: str) -> Any | None:
    """Resolve a shared mailbox's Inbox folder, tolerant of how it's mounted in the
    Outlook profile. Returns the folder, or ``None`` if no strategy works.

    Outlook surfaces a shared mailbox three different ways, and only the first
    works with ``GetSharedDefaultFolder`` (which is for *delegate* access to
    someone else's default folders):

    1. **Delegated** — granted access to the mailbox →
       ``GetSharedDefaultFolder(recipient, olFolderInbox)``.
    2. **Added as an account** (own credentials / a manual add) — it appears in
       ``namespace.Accounts`` with its ``SmtpAddress``; use
       ``account.DeliveryStore.GetDefaultFolder(olFolderInbox)``.
    3. **Auto-mapped as a secondary store** — it appears as a top-level folder in
       ``namespace.Folders`` named for the mailbox; match it and take ``Inbox``.

    When the mailbox is mounted as its own store (cases 2/3 — the common setup),
    ``GetSharedDefaultFolder`` raises **E_NOTIMPL (0x80004001)**, so we fall
    through to the store-based strategies. Operates on the COM ``namespace`` passed
    in (no win32 import here), so the strategy selection is unit-testable with
    fakes."""
    OL_FOLDER_INBOX = 6
    target = mailbox_address.strip().lower()

    # 1) Delegate access.
    try:
        recipient = namespace.CreateRecipient(mailbox_address)
        recipient.Resolve()
        if getattr(recipient, "Resolved", False):
            return namespace.GetSharedDefaultFolder(recipient, OL_FOLDER_INBOX)
    except Exception as exc:  # noqa: BLE001 — E_NOTIMPL when it's its own store
        logger.info(
            "outlook: delegate access to %s unavailable (%s); trying stores",
            mailbox_address,
            exc,
        )

    # 2) Added as an account — match on SMTP, take its delivery store's Inbox.
    try:
        for account in namespace.Accounts:
            smtp = str(getattr(account, "SmtpAddress", "") or "").strip().lower()
            if smtp and smtp == target:
                store = getattr(account, "DeliveryStore", None)
                if store is not None:
                    return store.GetDefaultFolder(OL_FOLDER_INBOX)
    except Exception:  # noqa: BLE001 — best-effort; fall through to folders
        logger.info("outlook: account lookup for %s failed", mailbox_address)

    # 3) Auto-mapped secondary store — a top-level folder named for the mailbox.
    local = target.split("@", 1)[0]
    try:
        for store_folder in namespace.Folders:
            name = str(getattr(store_folder, "Name", "") or "").strip().lower()
            if name and (
                name == target or local == name or local in name.replace(" ", "-")
            ):
                try:
                    return store_folder.Folders["Inbox"]
                except Exception:  # noqa: BLE001 — this store has no Inbox; keep looking
                    continue
    except Exception:  # noqa: BLE001 — best-effort
        logger.info("outlook: store-folder lookup for %s failed", mailbox_address)

    return None


def _recent_filter(now: datetime, days: int) -> str:
    """A **DASL** date filter for "received in the last ``days``". DASL uses an
    ISO-8601 date, so it's **locale-independent** — unlike a jet ``[ReceivedTime]``
    restriction, whose date must match the machine's short-date format. Pure, so
    the filter string is unit-tested without COM."""
    cutoff = now - timedelta(days=days)
    stamp = cutoff.strftime("%Y-%m-%d %H:%M")
    return f"@SQL=\"urn:schemas:httpmail:datereceived\" >= '{stamp}'"


def _recent_items(folder: Any, now: datetime, days: int) -> Any:
    """The folder's ``Items`` restricted to the last ``days`` — so a *massive*
    shared mailbox is never sorted or enumerated whole. ``days <= 0`` means no
    window. Best-effort: if the store rejects the ``Restrict`` (some do), fall back
    to the unfiltered collection rather than failing the scan."""
    items = folder.Items
    if days <= 0:
        return items
    try:
        return items.Restrict(_recent_filter(now, days))
    except Exception:  # noqa: BLE001 — Restrict unsupported on this store
        logger.info("outlook: date Restrict unsupported; scanning unfiltered")
        return items


def read_mailbox_items(
    mailbox_address: str, limit: int = 50, days: int = 14
) -> list[dict[str, Any]]:
    """The most recent ``limit`` items in a shared mailbox's Inbox, newest first,
    as ``new_mail`` payloads (same shape OnNewMailEx emits). The mailbox's Inbox is
    resolved by :func:`_shared_inbox`, which tolerates delegate / account /
    auto-mapped-store mountings.

    **Bounded for a large mailbox** (a shared help-desk inbox can hold hundreds of
    thousands of items): only mail received in the last ``days`` is considered (a
    ``Restrict``, so the whole folder is never sorted), and only the newest
    ``limit`` are read — fetched lazily via ``GetFirst``/``GetNext``, so at most
    ``limit`` message bodies are ever touched.

    Best-effort: ``[]`` off-Windows, on an unresolvable mailbox, or on any COM
    error — a failed scan never blocks startup. Windows-only.
    """
    if sys.platform != "win32" or not mailbox_address:
        return []

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        namespace = app.GetNamespace("MAPI")
        folder = _shared_inbox(namespace, mailbox_address)
        if folder is None:
            # Not a crash — the live NewMailEx channel still surfaces new mail; we
            # just couldn't read existing items to recover in-flight threads on
            # start. A clear hint beats an alarming traceback.
            logger.warning(
                "outlook: could not open the Inbox for shared mailbox %s — backlog "
                "scan skipped. Make sure the mailbox is added to your Outlook "
                "profile (its own account or an auto-mapped mailbox).",
                mailbox_address,
            )
            return []
        items = _recent_items(folder, datetime.now(), days)
        items.Sort("[ReceivedTime]", True)  # descending — newest first
        out: list[dict[str, Any]] = []
        # Lazy: walk newest-first and stop at `limit`, so a massive mailbox never
        # has more than `limit` items materialised (the window already bounds Sort).
        item = items.GetFirst()
        while item is not None and len(out) < limit:
            try:
                out.append(_mail_payload(item))
            except Exception:
                pass  # skip a single unreadable item, keep scanning
            item = items.GetNext()
        return out
    except Exception:
        logger.exception("outlook: backlog scan of %s failed", mailbox_address)
        return []
    finally:
        pythoncom.CoUninitialize()


# ── calendar-driven working-hours rota ────────────────────────────────────────
#
# When the rota's source = "outlook", "are we on shift right now?" is derived
# from the user's calendar: a marker event (subject contains e.g. "shift")
# covering now means on-shift. Optionally also hold while a meeting/OOO is on.
# The decision is a pure function (tested with fakes); the COM read is Windows-
# only and returns None when it can't read, so the caller falls back to the grid.

# Outlook OlBusyStatus: olFree=0, olTentative=1, olBusy=2, olOutOfOffice=3,
# olWorkingElsewhere=4. "Busy" for hold purposes = Busy or OutOfOffice.
_BUSY_STATUSES = frozenset({2, 3})


def _covers(event: dict[str, Any], now: datetime) -> bool:
    start, end = event.get("start"), event.get("end")
    return bool(start and end and start <= now <= end)


def decide_calendar(
    events: list[dict[str, Any]],
    now: datetime,
    marker: str,
    hold_during_meetings: bool,
) -> bool:
    """Pure rota decision from calendar events covering ``now``.

    On-shift = a marker event (subject contains ``marker``, case-insensitive)
    covers ``now``. If ``hold_during_meetings``, a *non-marker* busy/OOO event
    covering now suppresses firing too. Each event: {subject, start, end, busy}.
    """
    marker_l = marker.lower()

    def is_marker(e: dict[str, Any]) -> bool:
        return marker_l in str(e.get("subject", "")).lower()

    if not any(is_marker(e) and _covers(e, now) for e in events):
        return False
    if hold_during_meetings:
        for e in events:
            if not is_marker(e) and e.get("busy") and _covers(e, now):
                return False
    return True


def _to_dt(value: Any) -> datetime | None:
    """Best-effort naive datetime from a COM/pywintypes time (None on failure)."""
    try:
        return datetime(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            getattr(value, "second", 0),
        )
    except Exception:
        return None


def read_calendar_now() -> list[dict[str, Any]] | None:
    """Calendar events overlapping *now*, via the desktop Outlook client (COM).

    Returns a list of {subject, start, end, busy} dicts, or ``None`` if the
    calendar can't be read (non-Windows, Outlook closed, COM error) — the caller
    then falls back to the console grid. Windows-only; win32 imports stay here.
    """
    if sys.platform != "win32":
        return None
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            app = win32com.client.Dispatch("Outlook.Application")
            cal = app.GetNamespace("MAPI").GetDefaultFolder(9)  # olFolderCalendar
            items = cal.Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            now = datetime.now()
            stamp = now.strftime("%m/%d/%Y %I:%M %p")
            restricted = items.Restrict(f"[Start] <= '{stamp}' AND [End] >= '{stamp}'")
            out: list[dict[str, Any]] = []
            for it in restricted:
                out.append(
                    {
                        "subject": _str(_attr(it, "Subject")),
                        "start": _to_dt(_attr(it, "Start", None)),
                        "end": _to_dt(_attr(it, "End", None)),
                        "busy": int(_attr(it, "BusyStatus", 0) or 0) in _BUSY_STATUSES,
                    }
                )
            return out
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        logger.exception("outlook: calendar read failed")
        return None


def calendar_clock_ok(
    now: datetime, marker: str, hold_during_meetings: bool
) -> bool | None:
    """Whether triggers may fire now per the Outlook calendar, or ``None`` if the
    calendar couldn't be read (caller falls back to the console grid)."""
    events = read_calendar_now()
    if events is None:
        return None
    return decide_calendar(events, now, marker, hold_during_meetings)


register_source("outlook", OutlookSource)
