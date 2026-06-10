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

The pure payload-extraction helpers (``_mail_payload`` / ``_reminder_payload``)
take any duck-typed Outlook item, so they're unit-tested on any platform with
plain fakes — the win32 imports stay inside the pump thread.

See docs/design/console-triggers.md (§4.1).
"""

from __future__ import annotations

import logging
import sys
import threading
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


def _split_categories(val: Any) -> list[str]:
    """Outlook ``Categories`` is a comma-joined string → list of names."""
    text = _str(val).strip()
    if not text:
        return []
    return [c.strip() for c in text.split(",") if c.strip()]


def _mail_payload(mail: Any) -> dict[str, Any]:
    """Extract the ``new_mail`` payload from a MailItem-like object."""
    sender = _attr(mail, "SenderEmailAddress") or _attr(mail, "SenderName")
    body = _str(_attr(mail, "Body"))
    if len(body) > _BODY_CAP:
        body = body[:_BODY_CAP] + "…"
    return {
        "from": _str(sender),
        "to": _str(_attr(mail, "To")),
        "subject": _str(_attr(mail, "Subject")),
        "body": body,
        "received": _str(_attr(mail, "ReceivedTime")),
        "categories": _split_categories(_attr(mail, "Categories")),
        "entry_id": _str(_attr(mail, "EntryID")),
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
            app.emit = self._emit
            self._app = app  # hold a ref so the sink isn't GC'd mid-pump
            logger.info("outlook source: listening for new mail and reminders")
            pythoncom.PumpMessages()
        finally:
            pythoncom.CoUninitialize()


def save_reply_draft(entry_id: str, body: str) -> dict[str, str]:
    """Create a reply to the message ``entry_id`` and save it to Drafts.

    The agent's ``body`` is placed above Outlook's quoted original. The draft is
    **saved, never sent** — the user reviews and sends it from Outlook. Returns a
    small summary (to / subject). Raises on any failure so the caller can report
    it on the card. Windows-only; the win32 imports stay inside this function.
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
        reply.Save()  # → Drafts; does NOT send
        return {
            "to": _str(_attr(reply, "To")),
            "subject": _str(_attr(reply, "Subject")),
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
