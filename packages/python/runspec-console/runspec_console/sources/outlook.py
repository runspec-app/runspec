"""
sources/outlook.py — the Outlook COM event source (Windows).

Source #1 for triggers. The desktop Outlook client is already signed in with the
org's SSO session, so subscribing to its COM events rides that session and
sidesteps the Conditional-Access wall that blocks the MSAL device-code flow.
``pywin32`` is already a console dependency, so this needs no new package.

Two event types, both **forward-only** — they fire for occurrences that happen
while we're running; we never scan for backlog:

* ``new_mail`` — a **folder watch**: for the primary ``[mailbox]`` plus any
  watch-only ``[[watch]]`` extras, bind ``Folder.Items.ItemAdd`` on each watched
  folder (+ a poll fallback for the bulk moves ItemAdd misses, deduped by EntryID).
  ``NewMailEx`` is *not* used — it only sees the default account's primary Inbox,
  not subfolders or other mailboxes.
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
import os
import queue
import sys
import tempfile
import threading
import time
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

    Reads the primary ``[mailbox]`` (``config.read_mailbox()``) plus any watch-only
    ``[[watch]]`` extras (``config.read_watches()``) and binds an ``Items.ItemAdd``
    sink to each resolved folder (+ a poll fallback for the bulk moves ItemAdd can
    miss), plus the Application ``Reminder`` handler.
    Each ``new_mail`` event is tagged with the ``mailbox``/``folder`` it came from
    so triggers can scope to it. See docs/design/teams-and-email-monitoring.md."""

    id = "outlook"

    _PUMP_INTERVAL = 0.5  # s between COM message pumps (≈ ItemAdd latency)
    _POLL_INTERVAL = 30.0  # s between fallback folder polls
    _SEEN_CAP = 50_000  # bound the EntryID dedupe set over a long session
    _RECONNECT_MIN = 5.0  # s: first wait before re-attaching to a restarted Outlook
    _RECONNECT_MAX = 60.0  # s: cap on the reconnect backoff
    _STOP_JOIN_TIMEOUT = 5.0  # s: cap on waiting for the pump to unwind on stop()

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

        # The primary ``[mailbox]`` (the room/helpdesk identity, when set) plus any
        # watch-only ``[[watch]]`` extras (e.g. the operator's own Inbox alongside a
        # shared helpdesk mailbox). Each becomes one folder watch; the payload tags
        # keep the ``mailbox``/``folder`` keys the bind/poll loops and trigger
        # ``match`` already use (mailbox = the configured address). Exact
        # ``(address, folder)`` duplicates collapse so a watch that repeats the
        # primary doesn't double-bind.
        watches: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(address: str, folder: str) -> None:
            key = (address, folder)
            if key in seen:
                return
            seen.add(key)
            watches.append({"mailbox": address, "folder": folder})

        primary = config.read_mailbox()
        if primary is not None:
            add(primary["address"], primary["folder"])
        for extra in config.read_watches():
            add(extra["address"], extra["folder"])
        return watches

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
        self._queue.put(None)  # wake the dispatcher so it exits its blocking get()
        # Join both threads before returning. The pump owns a COM apartment with
        # WithEvents sinks bound to the *singleton* Outlook.Application; if stop()
        # returned while they were still bound, a restart (every mailbox-settings
        # save calls restart_triggers → stop + immediate start) would spin up a
        # second STA pump that binds the same folders concurrently. Overlapping
        # STA pumps on one Outlook instance deadlock on Windows — which froze the
        # console ("not responding") whenever the mailbox settings were changed.
        # Joining serialises teardown → rebuild so the old apartment (and its
        # CoUninitialize) completes first. Bounded: a pump wedged inside a COM
        # call (e.g. Dispatch launching Outlook) must not block the caller
        # forever — log and move on rather than hang.
        current = threading.current_thread()
        for label, thread in (
            ("pump", self._thread),
            ("dispatch", self._dispatch_thread),
        ):
            if thread is not None and thread is not current:
                thread.join(timeout=self._STOP_JOIN_TIMEOUT)
                if thread.is_alive():
                    logger.warning(
                        "outlook: %s thread did not stop within %.0fs; a restart "
                        "may briefly overlap COM apartments",
                        label,
                        self._STOP_JOIN_TIMEOUT,
                    )
        self._thread = None
        self._dispatch_thread = None
        self._app = None

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
        items received after the high-water mark, hand them to the deduped handler,
        and advance the mark. Best-effort; a per-folder failure is logged, not fatal.

        The cutoff is compared in Python from each item's own ``ReceivedTime`` — it
        deliberately does **not** use ``Items.Restrict`` with a formatted date
        string. Restrict re-parses that string using the OS **regional settings**,
        so a hardcoded US ``MM/DD/YYYY`` stamp was misread on non-US-date locales
        (en-GB reads ``07/02/2026`` as 7 Feb, not 2 Jul), pushing the cutoff months
        into the past and re-emitting the whole inbox as ``new_mail`` on every
        startup — worst on a large personal "also watch" Inbox. Sorting newest-first
        + breaking at the first older item keeps this to the recent tail instead of
        walking the whole folder."""
        high = self._poll_high_water
        newest = high
        for _sink, items, mailbox, folder in self._bound:
            try:
                items.Sort("[ReceivedTime]", True)  # newest first
                for item in items:
                    ts = _to_dt(_attr(item, "ReceivedTime", None))
                    if ts is None:
                        continue
                    stamp = ts.timestamp()
                    if stamp <= high:
                        break  # sorted newest-first → everything after is older
                    self._handle_item(item, mailbox, folder)
                    newest = max(newest, stamp)
            except Exception:
                logger.debug("outlook: poll of %r failed", folder, exc_info=True)
        self._poll_high_water = max(self._poll_high_water, newest)


def _compose_reply_draft(
    app: Any,
    namespace: Any,
    entry_id: str,
    body: str,
    from_mailbox: str | None,
    reply_all: bool = False,
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
    # ReplyAll copies To + every other recipient into CC; Reply just the sender.
    # Either is read-only use here — we never Save()/Send() this object.
    reply = original.ReplyAll() if reply_all else original.Reply()
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
    entry_id: str,
    body: str,
    from_mailbox: str | None = None,
    reply_all: bool = False,
) -> dict[str, str]:
    """Create a reply to the message ``entry_id`` and save it to Drafts.

    ``reply_all`` replies to the sender **and** all other recipients (CC included)
    rather than just the sender.

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
        return _compose_reply_draft(
            app, namespace, entry_id, body, from_mailbox, reply_all
        )
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


# ── agent-facing reads + compose (Outlook Classic tools) ──────────────────────
#
# On-demand helpers the chat agent calls to read the inbox / calendar / reminders
# and to draft or send mail — all via the same desktop COM session the source
# uses, so they ride the org SSO and dodge the Graph device-code wall. Each opens
# its own ``CoInitialize`` (they run on the bridge's tool-call thread, not the
# pump). The pure row-shapers (``_mail_summary`` / ``_event_row`` / ``_search_dasl``)
# are duck-typed and unit-tested with fakes on any platform.

_PREVIEW_CAP = 280  # chars of body preview in a list/search row (tokens stay small)
# A full single-message read (outlook_get_message) is allowed a larger body than a
# list-row preview, but still bounded so one giant message can't blow the context.
_FULL_BODY_CAP = 50_000


def outlook_available() -> bool:
    """True when Outlook Classic COM can be driven from this process (Windows +
    pywin32). Cheap; used to gate the agent tools' presence."""
    if sys.platform != "win32":
        return False
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except Exception:
        return False
    return True


def _mail_summary(mail: Any) -> dict[str, Any]:
    """Compact row for a message (subject/from/received/unread/entry_id + a short
    preview) — small enough to list many without bloating the model context. The
    agent can fetch the full body via the entry_id if it needs it."""
    body = " ".join(_str(_attr(mail, "Body")).split())
    preview = body[:_PREVIEW_CAP] + ("…" if len(body) > _PREVIEW_CAP else "")
    return {
        "subject": _str(_attr(mail, "Subject")),
        "from": _str(_attr(mail, "SenderName") or _attr(mail, "SenderEmailAddress")),
        "received": _str(_attr(mail, "ReceivedTime")),
        "unread": bool(_attr(mail, "UnRead", False)),
        "entry_id": _str(_attr(mail, "EntryID")),
        "preview": preview,
    }


def _event_row(it: Any) -> dict[str, Any]:
    """Compact row for a calendar item."""
    return {
        "subject": _str(_attr(it, "Subject")),
        "start": _str(_to_dt(_attr(it, "Start", None))),
        "end": _str(_to_dt(_attr(it, "End", None))),
        "location": _str(_attr(it, "Location")),
        "organizer": _str(_attr(it, "Organizer")),
        "busy": int(_attr(it, "BusyStatus", 0) or 0) in _BUSY_STATUSES,
    }


def _search_dasl(query: str) -> str:
    """A DASL ``@SQL`` Restrict filter matching ``query`` in subject or body.

    Pure + testable. Single quotes in the query are doubled (DASL string escape);
    the property names are the standard httpmail schema URNs."""
    q = query.replace("'", "''")
    subj = "urn:schemas:httpmail:subject"
    text = "urn:schemas:httpmail:textdescription"
    return f"@SQL=(\"{subj}\" LIKE '%{q}%' OR \"{text}\" LIKE '%{q}%')"


def list_messages(
    mailbox: str = "", folder: str = "", unread_only: bool = True, count: int = 20
) -> list[dict[str, Any]]:
    """Most-recent messages in the watched folder (default: the configured/Inbox),
    newest first, optionally unread-only. Windows-only; win32 imports stay here."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        target = _resolve_watch(ns, mailbox, folder)
        items = target.Items
        items.Sort("[ReceivedTime]", True)  # descending — newest first
        if unread_only:
            items = items.Restrict("[Unread] = true")
        out: list[dict[str, Any]] = []
        for it in items:
            out.append(_mail_summary(it))
            if len(out) >= count:
                break
        return out
    finally:
        pythoncom.CoUninitialize()


def search_messages(
    query: str, mailbox: str = "", folder: str = "", count: int = 20
) -> list[dict[str, Any]]:
    """Messages in the watched folder whose subject or body match ``query``
    (substring), newest first. Windows-only."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        target = _resolve_watch(ns, mailbox, folder)
        items = target.Items
        items.Sort("[ReceivedTime]", True)
        restricted = items.Restrict(_search_dasl(query))
        out: list[dict[str, Any]] = []
        for it in restricted:
            out.append(_mail_summary(it))
            if len(out) >= count:
                break
        return out
    finally:
        pythoncom.CoUninitialize()


def upcoming_events(days: int = 1, count: int = 20) -> list[dict[str, Any]] | None:
    """Calendar events overlapping the window now…now+``days`` (default today),
    soonest first. ``None`` if the calendar can't be read (non-Windows / Outlook
    closed / COM error). Windows-only."""
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
            end = now + timedelta(days=max(1, days))
            fmt = "%m/%d/%Y %I:%M %p"
            restricted = items.Restrict(
                f"[Start] <= '{end.strftime(fmt)}' AND [End] >= '{now.strftime(fmt)}'"
            )
            out: list[dict[str, Any]] = []
            for it in restricted:
                out.append(_event_row(it))
                if len(out) >= count:
                    break
            return out
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        logger.exception("outlook: upcoming_events read failed")
        return None


def list_reminders(count: int = 20) -> list[dict[str, Any]] | None:
    """Currently-active Outlook reminders (caption + due time). ``None`` on a COM
    error. Windows-only."""
    if sys.platform != "win32":
        return None
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            app = win32com.client.Dispatch("Outlook.Application")
            out: list[dict[str, Any]] = []
            for r in app.Reminders:
                if not _attr(r, "IsVisible", True):
                    continue
                out.append(
                    {
                        "caption": _str(_attr(r, "Caption")),
                        "due": _str(_attr(r, "NextReminderDate")),
                    }
                )
                if len(out) >= count:
                    break
            return out
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        logger.exception("outlook: list_reminders read failed")
        return None


def _build_appointment_reminder(
    app: Any, subject: str, when: datetime
) -> dict[str, str]:
    """A calendar appointment (``CreateItem(1)``) with a reminder at ``when`` — the
    nicest reminder (shows on the calendar), for Exchange/M365 profiles."""
    appt = app.CreateItem(1)  # olAppointmentItem
    appt.Subject = subject
    appt.Start = when
    appt.Duration = 0  # a point-in-time nudge, not a busy block
    appt.ReminderSet = True
    appt.ReminderMinutesBeforeStart = 0  # fire at Start
    appt.Save()  # → default calendar
    return {
        "subject": _str(_attr(appt, "Subject")),
        "entry_id": _str(_attr(appt, "EntryID")),
        "kind": "appointment",
    }


def _build_task_reminder(app: Any, subject: str, when: datetime) -> dict[str, str]:
    """A task (``CreateItem(3)``) with a reminder — falls back here when the default
    store has no usable calendar (e.g. some non-Exchange profiles)."""
    task = app.CreateItem(3)  # olTaskItem
    task.Subject = subject
    task.StartDate = when
    task.DueDate = when
    task.ReminderSet = True
    task.ReminderTime = when
    task.Save()
    return {
        "subject": _str(_attr(task, "Subject")),
        "entry_id": _str(_attr(task, "EntryID")),
        "kind": "task",
    }


def _build_flagged_mail_reminder(
    app: Any, subject: str, when: datetime
) -> dict[str, str]:
    """A draft mail (``CreateItem(0)``) flagged as a to-do with a reminder. The most
    portable fallback: it uses the mail item, which works on **IMAP/POP** profiles
    whose default store has no Calendar/Tasks folder (the case that breaks the
    appointment + task paths). Saved to Drafts, never sent."""
    mail = app.CreateItem(0)  # olMailItem
    mail.Subject = subject
    mail.MarkAsTask(1)  # olMarkToday — flag it as a to-do so a reminder can attach
    mail.TaskStartDate = when
    mail.TaskDueDate = when
    mail.ReminderSet = True
    mail.ReminderTime = when
    mail.Save()  # → Drafts
    return {
        "subject": _str(_attr(mail, "Subject")),
        "entry_id": _str(_attr(mail, "EntryID")),
        "kind": "flagged_mail",
    }


def _build_reminder_item(app: Any, subject: str, when: datetime) -> dict[str, str]:
    """Set a reminder at ``when`` using whichever item type the profile supports
    (testable core; duck-typed ``app``). Tries the calendar appointment first
    (best UX), then a task, then a flagged to-do mail — because a mail-only/IMAP
    profile can have a default store with no Calendar/Tasks folder, so
    ``CreateItem(1)``/``(3)`` fail there while ``CreateItem(0)`` (mail) works.
    Raises only if *every* method fails."""
    attempts = (
        ("appointment", _build_appointment_reminder),
        ("task", _build_task_reminder),
        ("flagged_mail", _build_flagged_mail_reminder),
    )
    errors: list[str] = []
    for name, build in attempts:
        try:
            result = build(app, subject, when)
            if errors:
                logger.info(
                    "outlook: reminder via %s (after %s)", name, "; ".join(errors)
                )
            return result
        except Exception as exc:  # noqa: BLE001 — try the next item type
            errors.append(f"{name}: {exc}")
    raise RuntimeError("could not create any reminder item — " + "; ".join(errors))


def create_reminder(subject: str, when: datetime) -> dict[str, str]:
    """Set an Outlook reminder for ``when`` — used by the Rota "Take a break" button
    to nudge the operator at the return time. Tries a calendar appointment, then a
    task, then a flagged to-do mail (so it works on IMAP/POP profiles too). Returns
    {subject, entry_id, kind}. Windows-only; win32 imports stay inside. Callers
    treat this as best-effort (no Outlook ⇒ no reminder, the break still works)."""
    if sys.platform != "win32":
        raise RuntimeError("create_reminder requires Windows (Outlook COM)")
    if not subject:
        raise RuntimeError("create_reminder needs a subject")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        return _build_reminder_item(app, subject, when)
    finally:
        pythoncom.CoUninitialize()


def save_new_draft(
    to: str, subject: str, body: str, cc: str = "", from_mailbox: str = ""
) -> dict[str, str]:
    """Compose a fresh mail and save it to Drafts (never sent). Returns
    {to, subject, entry_id}. Windows-only."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        mail = app.CreateItem(0)  # olMailItem
        mail.To = to
        mail.Subject = subject
        mail.Body = body
        if cc:
            mail.CC = cc
        if from_mailbox:
            mail.SentOnBehalfOfName = from_mailbox
        try:
            mail.Recipients.ResolveAll()
        except Exception:
            logger.debug("outlook: ResolveAll on new draft failed", exc_info=True)
        mail.Save()
        return {
            "to": _str(_attr(mail, "To")),
            "subject": _str(_attr(mail, "Subject")),
            "entry_id": _str(_attr(mail, "EntryID")),
        }
    finally:
        pythoncom.CoUninitialize()


def send_new_mail(
    to: str, subject: str, body: str, cc: str = "", from_mailbox: str = ""
) -> dict[str, str]:
    """Compose and **send** a fresh mail in one step (the agent's confirm-gated
    send path). Returns {to, subject}. Windows-only."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    if not to:
        raise RuntimeError("send has no recipient")
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        mail = app.CreateItem(0)
        mail.To = to
        mail.Subject = subject
        mail.Body = body
        if cc:
            mail.CC = cc
        if from_mailbox:
            mail.SentOnBehalfOfName = from_mailbox
        try:
            mail.Recipients.ResolveAll()
        except Exception:
            logger.debug("outlook: ResolveAll on send failed", exc_info=True)
        mail.Send()
        return {"to": to, "subject": subject}
    finally:
        pythoncom.CoUninitialize()


# ── agent-facing: full-message read, attachments, recipient resolve ───────────
#
# Tranche-1 reads that go beyond the list-row preview: fetch one message in full
# (the primitive the list/search rows promise via their entry_id), save an
# attachment to disk (so it can be fed to e.g. the logops runnables), and resolve
# a name against the address book before drafting. The pure shapers
# (_full_message / _attachment_rows / _match_attachment_index / _safe_attachment_name)
# are duck-typed and unit-tested with fakes on any platform.


def _attachment_rows(attachments: Any) -> list[dict[str, Any]]:
    """Compact rows for a message's attachments (name/size/1-based index). Pure;
    the COM ``Attachments`` collection is 1-indexed, so we fall back to the
    enumeration position when an item doesn't expose ``Index``."""
    rows: list[dict[str, Any]] = []
    for pos, att in enumerate(attachments or [], start=1):
        rows.append(
            {
                "name": _str(_attr(att, "FileName")),
                "size": int(_attr(att, "Size", 0) or 0),
                "index": int(_attr(att, "Index", pos) or pos),
            }
        )
    return rows


def _full_message(item: Any) -> dict[str, Any]:
    """Full single-message shape for ``outlook_get_message`` (duck-typed/testable).

    Like ``_mail_summary`` but with the **whole** body (capped at ``_FULL_BODY_CAP``)
    plus the cc line and the attachment list, so the agent can act on a message it
    found via a list/search row (which only carries a 280-char preview)."""
    body = _str(_attr(item, "Body"))
    if len(body) > _FULL_BODY_CAP:
        body = body[:_FULL_BODY_CAP] + "…"
    sender = _attr(item, "SenderName") or _attr(item, "SenderEmailAddress")
    return {
        "subject": _str(_attr(item, "Subject")),
        "from": _str(sender),
        "from_address": _str(_attr(item, "SenderEmailAddress")),
        "to": _str(_attr(item, "To")),
        "cc": _str(_attr(item, "CC")),
        "received": _str(_attr(item, "ReceivedTime")),
        "unread": bool(_attr(item, "UnRead", False)),
        "categories": _split_categories(_attr(item, "Categories")),
        "conversation_id": _str(_attr(item, "ConversationID")),
        "entry_id": _str(_attr(item, "EntryID")),
        "attachments": _attachment_rows(_attr(item, "Attachments", [])),
        "body": body,
    }


def get_message(entry_id: str) -> dict[str, Any]:
    """Read one Outlook message in full by its ``entry_id`` (full body + headers +
    attachment list). The reads that list/search messages return only a preview;
    this is how the agent gets the rest. Windows-only; win32 imports stay here."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    if not entry_id:
        raise RuntimeError("get_message needs an entry_id")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        return _full_message(ns.GetItemFromID(entry_id))
    finally:
        pythoncom.CoUninitialize()


def _safe_attachment_name(name: str) -> str:
    """Basename only — strip any directory parts so a crafted attachment filename
    can't escape the destination directory. Pure/testable."""
    base = os.path.basename(_str(name).replace("\\", "/")).strip()
    return base or "attachment"


def _match_attachment_index(
    rows: list[dict[str, Any]], name: str, index: int
) -> int | None:
    """Pick which attachment to save → its 1-based index, or ``None`` if no match.

    By explicit ``index`` if given; else by ``name`` (exact case-insensitive, then
    substring); else only when there's exactly one attachment. Pure/testable."""
    if index:
        return index if any(int(r.get("index", 0)) == index for r in rows) else None
    if name:
        nl = name.strip().lower()
        for r in rows:
            if _str(r.get("name")).lower() == nl:
                return int(r.get("index", 0))
        for r in rows:
            if nl in _str(r.get("name")).lower():
                return int(r.get("index", 0))
        return None
    return int(rows[0].get("index", 0)) if len(rows) == 1 else None


def save_attachment(
    entry_id: str, name: str = "", index: int = 0, dest_dir: str = ""
) -> dict[str, Any]:
    """Save one attachment of message ``entry_id`` to disk and return
    {name, path, size}. Identify it by ``name`` or 1-based ``index`` (omit both
    if the message has exactly one). ``dest_dir`` defaults to the system temp
    dir; the saved filename is always a basename so it can't escape it.
    Confirm-gated at the tool layer (it writes a file). Windows-only."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    if not entry_id:
        raise RuntimeError("save_attachment needs an entry_id")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        item = ns.GetItemFromID(entry_id)
        rows = _attachment_rows(_attr(item, "Attachments", []))
        if not rows:
            raise RuntimeError("message has no attachments")
        idx = _match_attachment_index(rows, name, int(index or 0))
        if idx is None:
            avail = ", ".join(r["name"] or "(unnamed)" for r in rows)
            raise RuntimeError(
                f"no attachment matched name={name!r} index={index!r}; available: {avail}"
            )
        att = item.Attachments.Item(idx)
        target_dir = dest_dir or tempfile.gettempdir()
        os.makedirs(target_dir, exist_ok=True)
        fname = _safe_attachment_name(_str(_attr(att, "FileName")) or name)
        path = os.path.join(target_dir, fname)
        att.SaveAsFile(path)
        return {"name": fname, "path": path, "size": int(_attr(att, "Size", 0) or 0)}
    finally:
        pythoncom.CoUninitialize()


def _resolve_smtp(recipient: Any) -> str:
    """Best-effort SMTP address for a resolved recipient (Exchange users expose an
    ``X.500``-style ``Address``, not SMTP). Returns ``""`` when unavailable."""
    try:
        entry = recipient.AddressEntry
        try:
            user = entry.GetExchangeUser()
            if user is not None:
                smtp = _str(_attr(user, "PrimarySmtpAddress"))
                if smtp:
                    return smtp
        except Exception:
            pass
        addr = _str(_attr(entry, "Address"))
        return addr if "@" in addr else ""
    except Exception:
        return ""


def resolve_recipient(name: str) -> dict[str, Any]:
    """Resolve ``name`` against the Outlook address book (GAL/contacts) → {resolved,
    name, address, exchange_dn}. ``address`` prefers the SMTP form. Read-only.
    Windows-only; win32 imports stay here."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    if not name:
        raise RuntimeError("resolve_recipient needs a name")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        rcpt = ns.CreateRecipient(name)
        rcpt.Resolve()
        addr = _str(_attr(rcpt, "Address"))
        smtp = _resolve_smtp(rcpt)
        return {
            "resolved": bool(_attr(rcpt, "Resolved", False)),
            "name": _str(_attr(rcpt, "Name")) or name,
            "address": smtp or addr,
            "exchange_dn": addr if addr.startswith("/") else "",
        }
    finally:
        pythoncom.CoUninitialize()


# ── agent-facing: calendar events + meeting responses ─────────────────────────
#
# Tranche-2 writes (confirm-gated at the tool layer): create a calendar event
# (an invite when attendees are given, else a personal block) and respond to a
# received meeting invite. The pure cores (_split_recipients / _build_event /
# _respond_code) are duck-typed and unit-tested with fakes.


def _split_recipients(value: str) -> list[str]:
    """Split a ``;``/``,``-separated recipient string into trimmed addresses."""
    return [c.strip() for c in _str(value).replace(",", ";").split(";") if c.strip()]


# ── online meeting (Teams link) by default ────────────────────────────────────
#
# Outlook exposes **no** per-appointment COM API to mint a Teams (or other
# provider) join link — the desktop Teams Meeting add-in isn't scriptable, and
# only the Teams service can actually create the online meeting. The one supported
# lever is the per-user "Add online meeting to all meetings" setting (Outlook
# Options → Calendar), stored in the registry under the Office version's Calendar
# options key. With it on — and the Teams Meeting add-in installed + Teams signed
# in — Outlook attaches a join link to every new meeting it creates, which is how
# ``create_event(online_meeting=True)`` gets a Teams link. It is a **machine-wide
# toggle for the signed-in user**, not a per-meeting switch, and a running Outlook
# may need a restart before the change takes effect. The key-path/version helpers
# are pure (unit-tested); the winreg read/write are Windows-only.

_ONLINE_MEETING_VALUE = "AddOnlineMeetingToAllMeeting"


def _office_version_root(version: str) -> str:
    """Normalise an Outlook ``Application.Version`` ("16.0.12345.20000") to its
    ``"<major>.0"`` Office registry segment ("16.0"). Falls back to ``"16.0"``
    (Outlook 2016/2019/2021/365) when it can't be parsed. Pure/testable."""
    parts = _str(version).split(".")
    if parts and parts[0].isdigit():
        return f"{parts[0]}.0"
    return "16.0"


def _calendar_options_key(version_root: str) -> str:
    r"""HKCU subkey path for the Outlook Calendar options of an Office version root
    (``"16.0"`` → ``Software\Microsoft\Office\16.0\Outlook\Options\Calendar``).
    Pure/testable."""
    return rf"Software\Microsoft\Office\{version_root}\Outlook\Options\Calendar"


def _read_online_meeting_default(version_root: str) -> bool:
    """Read the current ``AddOnlineMeetingToAllMeeting`` value (``False`` when the
    key/value is absent). Windows-only; winreg import stays inside."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _calendar_options_key(version_root)
        ) as key:
            value, _ = winreg.QueryValueEx(key, _ONLINE_MEETING_VALUE)
            return bool(value)
    except OSError:  # key or value missing → treated as off
        return False


def _write_online_meeting_default(version_root: str, enabled: bool) -> None:
    """Set ``AddOnlineMeetingToAllMeeting`` (DWORD) under HKCU, creating the key if
    needed. Windows-only."""
    import winreg

    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, _calendar_options_key(version_root)
    ) as key:
        winreg.SetValueEx(
            key, _ONLINE_MEETING_VALUE, 0, winreg.REG_DWORD, 1 if enabled else 0
        )


def set_online_meeting_default(
    enabled: bool = True, *, version: str = ""
) -> dict[str, Any]:
    """Turn Outlook's "Add online meeting to all meetings" setting on/off for the
    current user (HKCU registry).

    When on — with the desktop Teams Meeting add-in installed and Teams signed in —
    Outlook attaches an online-meeting (Teams) join link to **every** new meeting
    it creates; this is how :func:`create_event` with ``online_meeting=True`` gets
    a Teams link, since Outlook has no per-appointment COM API to add one. It is a
    **machine-wide toggle for the signed-in user**, not per-meeting, and a running
    Outlook may need a restart before it takes effect. ``version`` overrides the
    Office version root (else it's read from the running Outlook, defaulting to
    ``16.0``). Returns ``{enabled, previous, version, key}``. Windows-only;
    imports stay inside."""
    if sys.platform != "win32":
        raise RuntimeError("set_online_meeting_default requires Windows (Outlook)")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        ver = version
        if not ver:
            try:
                app = win32com.client.Dispatch("Outlook.Application")
                ver = _str(_attr(app, "Version"))
            except Exception:
                ver = ""
        version_root = _office_version_root(ver)
        previous = _read_online_meeting_default(version_root)
        _write_online_meeting_default(version_root, enabled)
        return {
            "enabled": bool(enabled),
            "previous": previous,
            "version": version_root,
            "key": _calendar_options_key(version_root),
        }
    finally:
        pythoncom.CoUninitialize()


def _build_event(
    app: Any,
    *,
    subject: str,
    start: datetime,
    end: datetime,
    attendees: str,
    location: str,
    body: str,
    reminder_minutes: int,
    online_meeting: bool = False,
) -> dict[str, Any]:
    """Create a calendar appointment (``CreateItem(1)``); send it as a meeting
    invite when ``attendees`` are given, else save it as a personal block. Pure
    core (duck-typed ``app``) so it's testable with a fake. Returns a small summary.

    ``online_meeting`` is reported back in the summary but has no effect on the
    ``appt`` itself — the Teams join link comes from Outlook's machine-wide
    "add online meeting to all meetings" setting, which :func:`create_event`
    enables (there is no per-appointment COM property to set)."""
    appt = app.CreateItem(1)  # olAppointmentItem
    appt.Subject = subject
    appt.Start = start
    appt.End = end
    if location:
        appt.Location = location
    if body:
        appt.Body = body
    if reminder_minutes is not None and reminder_minutes >= 0:
        appt.ReminderSet = True
        appt.ReminderMinutesBeforeStart = int(reminder_minutes)
    recips = _split_recipients(attendees)
    for addr in recips:
        appt.Recipients.Add(addr)
    if recips:
        appt.MeetingStatus = 1  # olMeeting → sending it invites the attendees
        try:
            appt.Recipients.ResolveAll()
        except Exception:
            logger.debug("outlook: ResolveAll on event failed", exc_info=True)
        appt.Send()
    else:
        appt.Save()  # → default calendar
    return {
        "subject": _str(_attr(appt, "Subject")),
        "start": _str(_attr(appt, "Start")),
        "end": _str(_attr(appt, "End")),
        "entry_id": _str(_attr(appt, "EntryID")),
        "attendees": recips,
        "sent": bool(recips),
        "online_meeting": bool(online_meeting),
    }


def create_event(
    subject: str,
    start: datetime,
    end: datetime,
    *,
    attendees: str = "",
    location: str = "",
    body: str = "",
    reminder_minutes: int = 15,
    online_meeting: bool = False,
) -> dict[str, Any]:
    """Create an Outlook calendar event — a meeting invite when ``attendees`` are
    given, else a personal calendar block. ``start``/``end`` are local-time
    datetimes (the tool layer parses ISO strings).

    ``online_meeting=True`` first enables Outlook's "add online meeting to all
    meetings" setting (see :func:`set_online_meeting_default`) so the new meeting
    gets a Teams join link. This is a **machine-wide toggle for the signed-in
    user**, not per-meeting — Outlook has no per-appointment COM API to add a link
    — so it requires the desktop Teams Meeting add-in installed + Teams signed in,
    affects subsequent new meetings too, and a running Outlook may need a restart
    to pick it up. Enabling is best-effort: a registry failure is logged and the
    event is still created. Confirm-gated at the tool layer. Windows-only."""
    if sys.platform != "win32":
        raise RuntimeError("create_event requires Windows (Outlook COM)")
    if not subject:
        raise RuntimeError("create_event needs a subject")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        if online_meeting:
            try:
                _write_online_meeting_default(
                    _office_version_root(_str(_attr(app, "Version"))), True
                )
            except Exception:
                logger.warning(
                    "outlook: could not enable online-meeting-by-default; the "
                    "event will be created without a Teams link",
                    exc_info=True,
                )
        return _build_event(
            app,
            subject=subject,
            start=start,
            end=end,
            attendees=attendees,
            location=location,
            body=body,
            reminder_minutes=reminder_minutes,
            online_meeting=online_meeting,
        )
    finally:
        pythoncom.CoUninitialize()


# Outlook OlMeetingResponse: olMeetingTentative=2, olMeetingAccepted=3,
# olMeetingDeclined=4.
_MEETING_RESPONSES = {"tentative": 2, "accept": 3, "decline": 4}


def _respond_code(response: str) -> int:
    """Map an accept/tentative/decline word to its OlMeetingResponse code.
    Pure/testable; raises ``ValueError`` on an unknown response."""
    code = _MEETING_RESPONSES.get(_str(response).strip().lower())
    if code is None:
        raise ValueError(
            f"response must be one of accept/tentative/decline (got {response!r})"
        )
    return code


def respond_invite(entry_id: str, response: str) -> dict[str, Any]:
    """Respond to a received meeting invite (``entry_id``) — accept / tentative /
    decline — and send the response to the organizer. Confirm-gated at the tool
    layer. Windows-only."""
    if sys.platform != "win32":
        raise RuntimeError("respond_invite requires Windows (Outlook COM)")
    if not entry_id:
        raise RuntimeError("respond_invite needs an entry_id")
    code = _respond_code(response)  # raises ValueError before any COM work

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        item = ns.GetItemFromID(entry_id)
        resp = item.Respond(code, True)  # fNoUI=True
        try:
            resp.Send()
        except Exception:
            logger.debug("outlook: sending meeting response failed", exc_info=True)
        return {
            "subject": _str(_attr(item, "Subject")),
            "response": _str(response).strip().lower(),
        }
    finally:
        pythoncom.CoUninitialize()


# ── agent-facing: mailbox triage actions (mark / categorize / move) ───────────
#
# Tranche-3 writes (confirm-gated): the "after handling" half of the helpdesk
# loop — flag a message read/unread, tag it with a category, or file it into a
# folder. The pure core (_merge_categories) is unit-tested with fakes.


def mark_message(entry_id: str, read: bool = True) -> dict[str, Any]:
    """Mark a message read (default) or unread by ``entry_id``. Returns
    {subject, unread}. Confirm-gated at the tool layer. Windows-only."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    if not entry_id:
        raise RuntimeError("mark_message needs an entry_id")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        item = app.GetNamespace("MAPI").GetItemFromID(entry_id)
        item.UnRead = not read
        item.Save()
        return {
            "subject": _str(_attr(item, "Subject")),
            "unread": bool(_attr(item, "UnRead", False)),
        }
    finally:
        pythoncom.CoUninitialize()


def _merge_categories(existing: Any, new: str, replace: bool) -> str:
    """Compute the new ``Categories`` string. ``replace`` ⇒ just the incoming ones;
    else union with the existing, preserving order and de-duping case-insensitively.
    Pure/testable. Accepts ``,`` or ``;``-separated incoming categories."""
    incoming = [c.strip() for c in _str(new).replace(";", ",").split(",") if c.strip()]
    if replace:
        chosen = incoming
    else:
        chosen = _split_categories(existing)
        seen = {c.lower() for c in chosen}
        for c in incoming:
            if c.lower() not in seen:
                seen.add(c.lower())
                chosen.append(c)
    return ", ".join(chosen)


def categorize_message(
    entry_id: str, categories: str, replace: bool = False
) -> dict[str, Any]:
    """Add (default) or replace Outlook categories on a message. Returns
    {subject, categories}. Confirm-gated at the tool layer. Windows-only."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    if not entry_id:
        raise RuntimeError("categorize_message needs an entry_id")
    if not str(categories or "").strip():
        raise RuntimeError("categorize_message needs at least one category")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        item = app.GetNamespace("MAPI").GetItemFromID(entry_id)
        item.Categories = _merge_categories(
            _attr(item, "Categories"), categories, replace
        )
        item.Save()
        return {
            "subject": _str(_attr(item, "Subject")),
            "categories": _split_categories(_attr(item, "Categories")),
        }
    finally:
        pythoncom.CoUninitialize()


def move_message(entry_id: str, mailbox: str = "", folder: str = "") -> dict[str, Any]:
    """Move a message to another folder (resolved like a watch: ``mailbox`` store +
    a ``/``-separated ``folder`` path). Returns the destination plus the moved
    item's **new** entry_id (Move re-keys it). Confirm-gated. Windows-only."""
    if sys.platform != "win32":
        raise RuntimeError("Outlook tools require Windows (Outlook COM)")
    if not entry_id:
        raise RuntimeError("move_message needs an entry_id")
    if not folder:
        raise RuntimeError("move_message needs a destination folder")

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        item = ns.GetItemFromID(entry_id)
        target = _resolve_watch(ns, mailbox, folder)
        moved = item.Move(target)  # returns the moved item on modern Outlook
        ref = moved if moved is not None else item
        return {
            "subject": _str(_attr(ref, "Subject")),
            "mailbox": mailbox,
            "folder": folder,
            "entry_id": _str(_attr(moved, "EntryID")) if moved is not None else "",
        }
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
