"""
sources/outlook.py — the Outlook COM event source (Windows).

Source #1 for triggers. The desktop Outlook client is already signed in with the
org's SSO session, so subscribing to its COM events rides that session and
sidesteps the Conditional-Access wall that blocks the MSAL device-code flow.
``pywin32`` is already a console dependency, so this needs no new package.

Two event types, both **forward-only** — they fire for occurrences that happen
while we're running; we never scan for backlog:

* ``new_mail`` — a **folder watch**: per ``[[mailboxes]]`` config entry, bind
  ``Folder.Items.ItemAdd`` on each watched folder (+ a poll fallback for the bulk
  moves ItemAdd misses, deduped by EntryID). ``NewMailEx`` is *not* used — it only
  sees the default account's primary Inbox, not subfolders or other mailboxes.
* ``reminder`` — ``Application.Reminder``.

Each ``new_mail`` is tagged with the ``mailbox``/``folder`` it came from so a
trigger's ``match`` can scope to it. There is **no claim system** — see
docs/design/email-folder-watch.md for the shared-mailbox duplication caveat.

COM event sinks need a thread that ``CoInitialize``s and runs a message pump. This
source owns one daemon thread that binds every sink, then loops:
``PumpWaitingMessages`` (delivers ItemAdd/Reminder) + the poll fallback, until
``stop()`` sets a flag. The watched ``Items`` collections are held for the pump's
lifetime — if one is GC'd its ``ItemAdd`` event silently stops firing.

The sink does **not** call the engine's ``emit`` directly — that would route the
event (and run the working-hours rota's *calendar* COM read) re-entrantly inside
the COM callback on the STA pump thread, which breaks the calendar read. Instead
each event is marshalled onto a queue and a separate dispatcher thread calls
``emit`` (and thus any downstream COM read) off the pump thread.

The pure helpers (``_mail_payload`` / ``_reminder_payload`` / ``_folder_segments``
/ ``_descend``) take duck-typed objects, so they're unit-tested on any platform
with plain fakes — the win32 imports stay inside the pump thread.

See docs/design/email-folder-watch.md and docs/design/console-triggers.md (§4.1).
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from datetime import datetime
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


def _connection_alive(namespace: Any) -> bool:
    """Cheap liveness probe for an Outlook MAPI namespace proxy.

    Reads a trivial property off the namespace. When Outlook restarts, the old COM
    proxy is left pointing at a dead RPC server, so the read raises ("The RPC
    server is not available") — the signal to tear down and re-``Dispatch``. Reads
    through to ``CurrentUser.Name`` to force an actual round-trip rather than a
    client-cached value."""
    try:
        user = namespace.CurrentUser
        if user is not None:
            _ = user.Name  # force a round-trip — raises if the RPC server is gone
        return True
    except Exception:
        return False


def _split_categories(val: Any) -> list[str]:
    """Outlook ``Categories`` is a comma-joined string → list of names."""
    text = _str(val).strip()
    if not text:
        return []
    return [c.strip() for c in text.split(",") if c.strip()]


def _folder_segments(folder: str) -> list[str]:
    """The ``/``-split path segments for a watch's folder, or ``[]`` for the
    Inbox sentinel (``""`` / ``"inbox"``). Pure; the source descends these from a
    store root. ``"Tickets/Open"`` → ``["Tickets", "Open"]``."""
    text = (folder or "").strip()
    if not text or text.lower() == "inbox":
        return []
    return [seg.strip() for seg in text.split("/") if seg.strip()]


def _descend(root: Any, segments: list[str]) -> Any:
    """Walk ``root.Folders[seg]`` for each segment (duck-typed, so the path-walk is
    testable with fakes). Raises if a segment is missing — the caller skips + logs
    the watch."""
    node = root
    for seg in segments:
        node = node.Folders[seg]
    return node


def _mail_payload(mail: Any, *, mailbox: str = "", folder: str = "") -> dict[str, Any]:
    """Extract the ``new_mail`` payload from a MailItem-like object.

    ``mailbox`` / ``folder`` tag the event with the watch it came from (the source
    knows which folder's ``ItemAdd`` fired), so a trigger's ``match`` can scope to
    a specific mailbox/folder. See docs/design/email-folder-watch.md."""
    sender = _attr(mail, "SenderEmailAddress") or _attr(mail, "SenderName")
    body = _str(_attr(mail, "Body"))
    if len(body) > _BODY_CAP:
        body = body[:_BODY_CAP] + "…"
    return {
        "from": _str(sender),
        "to": _str(_attr(mail, "To")),
        "subject": _str(_attr(mail, "Subject")),
        "body": body,
        # Which watch (mailbox + folder) this item landed in — the routing signal
        # a trigger's `match` can key on. Set by the source per watched folder.
        "mailbox": _str(mailbox),
        "folder": _str(folder),
        "received": _str(_attr(mail, "ReceivedTime")),
        "categories": _split_categories(_attr(mail, "Categories")),
        "entry_id": _str(_attr(mail, "EntryID")),
        # ConversationID threads a customer email and its replies; empty when
        # Outlook can't supply it. Still handy to a trigger; no longer a fold key.
        "conversation_id": _str(_attr(mail, "ConversationID")),
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


# ── COM event sinks ───────────────────────────────────────────────────────────
#
# Two sinks, both bound with win32com.client.WithEvents (which keeps `self` as the
# sink instance). A folder's `Items` collection raises `ItemAdd` for each new item;
# the Application raises `Reminder`. After binding we attach an `_on_*` callback the
# source set, so the handlers stay free of source state.


class _FolderItemsSink:
    """`ItemAdd` handler bound onto one watched folder's `Items` collection."""

    def OnItemAdd(self, item: Any) -> None:  # noqa: N802 — COM name
        cb = getattr(self, "_on_add", None)
        if cb is not None:
            cb(item)


class _AppReminderSink:
    """`Reminder` handler bound onto the Outlook Application."""

    def OnReminder(self, item: Any) -> None:  # noqa: N802 — COM name
        cb = getattr(self, "_on_reminder", None)
        if cb is not None:
            cb(item)


def _safe_emit(emit: EmitFn, event: Event) -> None:
    try:
        emit(event)
    except Exception:
        logger.exception("outlook: emit failed for %s", event.type)


def _resolve_watch(namespace: Any, mailbox: str, folder: str) -> Any:
    """Resolve a ``(mailbox, folder)`` watch to a MAPI folder (COM; Windows-only).

    ``mailbox`` blank → the default account; named → a mounted store (top-level
    `Folders[mailbox]`), falling back to the shared-mailbox default inbox's store
    root. ``folder`` blank/`"inbox"` → that store's Inbox; a `/` path descends from
    the **store root**. Raises if anything can't be resolved — the caller skips and
    logs the watch."""
    segments = _folder_segments(folder)
    if mailbox:
        try:
            root = namespace.Folders[mailbox]  # a mounted store is a top-level folder
        except Exception:
            recipient = namespace.CreateRecipient(mailbox)
            recipient.Resolve()
            root = namespace.GetSharedDefaultFolder(
                recipient, 6
            ).Parent  # 6=olFolderInbox
        if not segments:
            return root.Folders["Inbox"]
        return _descend(root, segments)
    inbox = namespace.GetDefaultFolder(6)  # default account Inbox
    if not segments:
        return inbox
    return _descend(inbox.Parent, segments)  # paths descend from the store root


# ── the source ────────────────────────────────────────────────────────────────


class OutlookSource:
    """Forward-only Outlook event source (folder-watch). Satisfies ``EventSource``.

    Reads the single configured ``[mailbox]`` from ``config.read_mailbox()`` and
    binds an ``Items.ItemAdd`` sink to its resolved folder (+ a poll fallback for
    the bulk moves ItemAdd can miss), plus the Application ``Reminder`` handler.
    Each ``new_mail`` event is tagged with the ``mailbox``/``folder`` it came from
    so triggers can scope to it. See docs/design/teams-and-email-monitoring.md."""

    id = "outlook"

    _PUMP_INTERVAL = 0.5  # s between COM message pumps (≈ ItemAdd latency)
    _POLL_INTERVAL = 30.0  # s between fallback folder polls
    _SEEN_CAP = 50_000  # bound the EntryID dedupe set over a long session
    _RECONNECT_MIN = 5.0  # s: first wait before re-attaching to a restarted Outlook
    _RECONNECT_MAX = 60.0  # s: cap on the reconnect backoff

    def __init__(self) -> None:
        self._emit: EmitFn | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Hold (sink, items, mailbox, folder) per watch for the pump's lifetime —
        # if the Items collection is GC'd its ItemAdd event silently stops firing.
        self._bound: list[tuple[Any, Any, str, str]] = []
        self._reminder_sink: Any = None
        self._app: Any = None
        self._seen: set[str] = set()  # EntryIDs already emitted (ItemAdd ∪ poll)
        self._poll_high_water: float = 0.0  # only poll items received after start
        # Events captured on the COM pump thread are handed to a dispatcher thread
        # so routing/emit never runs re-entrantly inside a COM callback.
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

    def _watches(self) -> list[dict[str, str]]:
        from .. import config

        mailbox = config.read_mailbox()
        if mailbox is None:
            return []
        # One configured inbox → one folder watch. The payload tags keep the
        # ``mailbox``/``folder`` keys the bind/poll loops and trigger ``match``
        # already use (mailbox = the configured address).
        return [{"mailbox": mailbox["address"], "folder": mailbox["folder"]}]

    def start(self, emit: EmitFn) -> None:
        ok, reason = self.available()
        if not ok:
            logger.info("outlook source not started: %s", reason)
            return
        if self._thread is not None:
            return  # already started
        self._emit = emit
        self._stop.clear()
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
        self._stop.set()  # the pump loop checks this and exits
        self._thread = None
        self._app = None
        if self._dispatch_thread is not None:
            self._queue.put(None)  # wake the dispatcher so it exits
            self._dispatch_thread = None

    def _enqueue(self, event: Event) -> None:
        """Hand an event from the COM pump thread to the dispatcher thread."""
        self._queue.put(event)

    def _handle_item(self, item: Any, mailbox: str, folder: str) -> None:
        """Emit one new mail item, tagged with its watch — deduped by EntryID so an
        item ItemAdd and the poll both see never fires twice. Runs on the pump
        thread; emit happens off it via the queue."""
        entry_id = _str(_attr(item, "EntryID"))
        if entry_id and entry_id in self._seen:
            return
        if entry_id:
            if len(self._seen) > self._SEEN_CAP:
                self._seen.clear()
            self._seen.add(entry_id)
        payload = _mail_payload(item, mailbox=mailbox, folder=folder)
        self._enqueue(Event.new("outlook", "new_mail", payload))

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
        """Own a COM apartment and keep an Outlook connection alive for the source's
        lifetime, **reconnecting when Outlook restarts**. Each connection binds the
        per-folder ItemAdd sinks (+ Reminder sink) and runs the pump/poll loop; when
        that loop detects the connection has dropped (the cached COM proxies point at
        a dead RPC server — "The RPC server is not available" after an Outlook
        restart), it tears the sinks down and re-attaches, with backoff, until
        ``stop()``. Runs on the daemon thread."""
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            # Forward-only from when the source starts; preserved across reconnects
            # (the EntryID dedupe set stops a gap-poll re-emitting anything seen).
            self._poll_high_water = time.time()
            backoff = self._RECONNECT_MIN
            while not self._stop.is_set():
                namespace = self._connect(win32com.client)
                if namespace is None:
                    # Outlook not reachable yet (closed / still relaunching). Wait
                    # and retry rather than killing the thread, so the source heals
                    # itself when Outlook comes back — no settings re-save needed.
                    if self._stop.wait(backoff):
                        break
                    backoff = min(backoff * 2, self._RECONNECT_MAX)
                    continue
                backoff = self._RECONNECT_MIN  # connected → reset the backoff
                try:
                    self._run_pump_loop(pythoncom, namespace)
                finally:
                    self._unbind()
        finally:
            self._unbind()
            pythoncom.CoUninitialize()

    def _connect(self, win32com_client: Any) -> Any:
        """Attach to Outlook and bind the watch + reminder sinks. Returns the MAPI
        namespace, or ``None`` if Outlook can't be reached (closed / mid-restart) so
        the pump retries. ``Dispatch`` relaunches Outlook if it isn't running, which
        is exactly the recovery we want after a restart."""
        try:
            app = win32com_client.Dispatch("Outlook.Application")
            namespace = app.GetNamespace("MAPI")
            # Touch the namespace so a dead/launching Outlook fails here (returns
            # None → retry) rather than later mid-loop.
            if not _connection_alive(namespace):
                return None
        except Exception:
            logger.debug("outlook: not reachable yet, will retry", exc_info=True)
            return None
        self._app = app
        self._bind_watches(win32com_client, namespace)
        self._bind_reminders(win32com_client, app)
        logger.info(
            "outlook source: watching %d folder(s) + reminders", len(self._bound)
        )
        return namespace

    def _run_pump_loop(self, pythoncom: Any, namespace: Any) -> None:
        """Pump COM messages (delivers ItemAdd/Reminder) and run the poll fallback on
        an interval. Returns when ``stop()`` is requested **or** the connection drops
        (so :meth:`_pump` reconnects). The poll-deadline tick probes the connection
        first; a restarted Outlook fails the probe and ends the loop."""
        poll_deadline = time.monotonic() + self._POLL_INTERVAL
        while not self._stop.is_set():
            pythoncom.PumpWaitingMessages()  # delivers queued ItemAdd/Reminder
            if time.monotonic() >= poll_deadline:
                if not _connection_alive(namespace):
                    logger.info(
                        "outlook: connection lost (Outlook restarted?) — reconnecting"
                    )
                    return
                self._poll_all()
                poll_deadline = time.monotonic() + self._POLL_INTERVAL
            self._stop.wait(self._PUMP_INTERVAL)

    def _unbind(self) -> None:
        """Drop the bound sinks + app handle so a stale COM proxy isn't reused on
        reconnect (the next :meth:`_connect` binds fresh ones)."""
        self._bound = []
        self._reminder_sink = None
        self._app = None

    def _bind_watches(self, win32com_client: Any, namespace: Any) -> None:
        """Resolve each watch and bind its folder's Items.ItemAdd sink. A watch that
        can't resolve logs one line and is skipped — never crashing the others."""
        for watch in self._watches():
            mailbox, folder = watch["mailbox"], watch["folder"]
            try:
                items = _resolve_watch(namespace, mailbox, folder).Items
                sink = win32com_client.WithEvents(items, _FolderItemsSink)
            except Exception:
                logger.warning(
                    "outlook: cannot watch mailbox=%r folder=%r — skipping",
                    mailbox or "(default)",
                    folder,
                )
                continue
            sink._on_add = lambda item, mb=mailbox, fd=folder: self._handle_item(
                item, mb, fd
            )
            self._bound.append((sink, items, mailbox, folder))

    def _bind_reminders(self, win32com_client: Any, app: Any) -> None:
        try:
            sink = win32com_client.WithEvents(app, _AppReminderSink)
            sink._on_reminder = lambda item: self._enqueue(
                Event.new("outlook", "reminder", _reminder_payload(item))
            )
            self._reminder_sink = sink
        except Exception:
            logger.warning("outlook: could not bind reminders")

    def _poll_all(self) -> None:
        """Fallback for bulk moves ItemAdd misses: scan each watched folder for
        items received since the high-water mark, hand them to the deduped handler,
        and advance the mark. Best-effort; a per-folder failure is logged, not fatal."""
        stamp = time.strftime(
            "%m/%d/%Y %I:%M %p", time.localtime(self._poll_high_water)
        )
        newest = self._poll_high_water
        for _sink, items, mailbox, folder in self._bound:
            try:
                recent = items.Restrict(f"[ReceivedTime] > '{stamp}'")
                for item in recent:
                    self._handle_item(item, mailbox, folder)
                    ts = _to_dt(_attr(item, "ReceivedTime", None))
                    if ts is not None:
                        newest = max(newest, ts.timestamp())
            except Exception:
                logger.debug("outlook: poll of %r failed", folder, exc_info=True)
        self._poll_high_water = max(self._poll_high_water, newest)


def _compose_reply_draft(
    app: Any, namespace: Any, entry_id: str, body: str, from_mailbox: str | None
) -> dict[str, str]:
    """Build + save the reply draft via the Outlook object model (testable core).

    We do **not** save/send the object that ``MailItem.Reply()`` returns: with
    Outlook's reading-pane inline replies, ``Reply()`` hands back an *inline
    response* item, and calling ``Save()``/``Send()`` on it (or on a draft re-opened
    from its EntryID) raises "This method can't be used with an inline response
    mail item". Instead we only **read** recipients / subject / quoted body off the
    reply, then copy them onto a fresh ``CreateItem`` mail — a standalone draft
    that saves and sends cleanly. (Reading properties off an inline response is
    fine; only the mutating methods are blocked.)

    Subject keeps Outlook's "RE: …" and the quoted original is preserved, so
    reply-by-subject threading and the quote survive even though we no longer use
    the native Reply object. Returns ``{to, subject, entry_id}``; ``entry_id`` is
    the saved draft the card's Send button later re-opens.
    """
    original = namespace.GetItemFromID(entry_id)
    reply = original.Reply()  # read-only use — never Save()/Send() this object
    quoted = _str(_attr(reply, "Body"))

    mail = app.CreateItem(0)  # olMailItem — a plain draft, not an inline response
    mail.To = _str(_attr(reply, "To"))
    cc = _str(_attr(reply, "CC"))
    if cc:
        mail.CC = cc
    mail.Subject = _str(_attr(reply, "Subject"))
    mail.Body = body + ("\n\n" + quoted if quoted else "")
    if from_mailbox:
        mail.SentOnBehalfOfName = from_mailbox  # reply as the shared mailbox
    try:
        mail.Recipients.ResolveAll()  # resolve names copied from the reply
    except Exception:
        logger.debug("outlook: ResolveAll on reply draft failed", exc_info=True)
    mail.Save()  # → Drafts; does NOT send
    return {
        "to": _str(_attr(mail, "To")),
        "subject": _str(_attr(mail, "Subject")),
        # EntryID is assigned once the draft is saved; the card's Send button
        # re-opens the draft by this id and sends it (human-initiated).
        "entry_id": _str(_attr(mail, "EntryID")),
    }


def save_reply_draft(
    entry_id: str, body: str, from_mailbox: str | None = None
) -> dict[str, str]:
    """Create a reply to the message ``entry_id`` and save it to Drafts.

    The agent's ``body`` is placed above Outlook's quoted original. The draft is
    **saved, never sent** — the user reviews it and sends from Outlook *or* via
    the card's Send button (see :func:`send_draft`). Returns a small summary
    (to / subject / ``entry_id`` of the saved draft, which the Send button uses).
    Raises on any failure so the caller can report it on the card. Windows-only;
    the win32 imports stay inside this function. See :func:`_compose_reply_draft`
    for why the reply is rebuilt rather than saved off ``Reply()`` directly.

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
        return _compose_reply_draft(app, namespace, entry_id, body, from_mailbox)
    finally:
        pythoncom.CoUninitialize()


def send_verify_mail(
    *, to: str, subject: str, body: str, from_mailbox: str = ""
) -> dict[str, str]:
    """Compose and **send** a fresh mail as the queue mailbox — the email-verify
    link (docs/design/email-queue-signup.md §7).

    Unlike :func:`save_reply_draft` / :func:`send_draft` (a human-reviewed agent
    reply) this auto-sends: it is a system verification mail, not an agent reply, so
    no card / draft step. ``from_mailbox`` sends it **as that shared mailbox**
    (``SentOnBehalfOfName``) so the recipient sees the queue identity; send-as vs
    send-on-behalf is decided by the Exchange permission, same COM call. Returns a
    small summary; raises on failure. Windows-only; win32 imports stay inside.
    """
    if sys.platform != "win32":
        raise RuntimeError("send_verify_mail requires Windows (Outlook COM)")
    if not to:
        raise RuntimeError("send_verify_mail has no recipient")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        mail = app.CreateItem(0)  # olMailItem
        mail.To = to
        mail.Subject = subject
        mail.Body = body
        if from_mailbox:
            mail.SentOnBehalfOfName = from_mailbox  # send as the queue mailbox
        mail.Send()
        return {"to": to, "subject": subject}
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
