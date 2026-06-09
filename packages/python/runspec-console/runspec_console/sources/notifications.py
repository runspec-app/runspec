"""
sources/notifications.py — the Windows notification-listener event source.

Source #2 for triggers, and the answer for apps with no automation model (Teams,
Slack, …): instead of integrating each app, listen to the toast it already
raises. ``Windows.UI.Notifications.Management.UserNotificationListener`` exposes
every toast system-wide through one subscription.

Why it's source #2, not #1: it needs a new dependency (the WinRT projection,
``winsdk`` — the ``notifications`` extra) and a one-time **user-consent grant**.
``available()`` reports both so the UI explains a disabled source.

Forward-only (A1): on start we snapshot the toasts already present and never
replay them — only notifications that arrive while we're running fire events.
We poll ``get_notifications_async`` on an interval (the changed-event has
restrictions for unpackaged desktop apps); the diff of ids is the new set.

The payload extraction is pure and unit-tested with fakes; the winsdk imports
stay inside the listener thread. See docs/design/console-triggers.md (§4.2).
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

from ..events import EmitFn, Event, register_source

logger = logging.getLogger("runspec.console.triggers.notifications")

# WinRT UserNotificationAccessStatus: Unspecified=0, Allowed=1, Denied=2.
_ACCESS_ALLOWED = 1
_ACCESS_DENIED = 2

_POLL_INTERVAL_S = 3.0


# ── pure payload extraction (tested with fakes) ───────────────────────────────


def _text_elements(notification: Any) -> list[str]:
    """All visual text lines of a WinRT Notification (title first, then body)."""
    texts: list[str] = []
    try:
        bindings = notification.visual.bindings
    except Exception:
        return texts
    for binding in bindings:
        try:
            for element in binding.get_text_elements():
                text = getattr(element, "text", None)
                if text:
                    texts.append(str(text))
        except Exception:
            continue
    return texts


def notification_payload(user_notification: Any) -> dict[str, Any]:
    """Extract the ``toast`` payload from a WinRT UserNotification-like object."""
    app = ""
    try:
        app = str(user_notification.app_info.display_info.display_name)
    except Exception:
        pass
    inner = getattr(user_notification, "notification", user_notification)
    texts = _text_elements(inner)
    return {
        "id": str(getattr(user_notification, "id", "")),
        "app": app,
        "title": texts[0] if texts else "",
        "body": "\n".join(texts[1:]) if len(texts) > 1 else "",
        "text": "\n".join(texts),
    }


def new_ids(previous: set[str], current: list[str]) -> list[str]:
    """Ids in ``current`` not seen in ``previous`` (order preserved)."""
    return [i for i in current if i and i not in previous]


def _safe_emit(emit: EmitFn, event: Event) -> None:
    try:
        emit(event)
    except Exception:
        logger.exception("notifications: emit failed")


# ── the source ────────────────────────────────────────────────────────────────


class NotificationSource:
    """Forward-only Windows toast listener. Satisfies the ``EventSource`` protocol."""

    id = "notifications"

    def __init__(self) -> None:
        self._emit: EmitFn | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._interval = _POLL_INTERVAL_S

    def available(self) -> tuple[bool, str]:
        if sys.platform != "win32":
            return (False, "Notification listener requires Windows")
        try:
            from winsdk.windows.ui.notifications.management import (
                UserNotificationListener,
            )
        except Exception:
            return (
                False,
                "winsdk not installed — pip install runspec-console[notifications]",
            )
        try:
            status = int(UserNotificationListener.current.get_access_status())
        except Exception:
            return (True, "")  # can't check synchronously; start() will request
        if status == _ACCESS_DENIED:
            return (False, "notification access denied — enable it in Windows Settings")
        return (True, "")

    def event_types(self) -> list[str]:
        return ["toast"]

    def start(self, emit: EmitFn) -> None:
        ok, reason = self.available()
        if not ok:
            logger.info("notifications source not started: %s", reason)
            return
        if self._thread is not None:
            return
        self._emit = emit
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="notif-listener", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _run(self) -> None:
        import asyncio

        try:
            asyncio.run(self._listen())
        except Exception:
            logger.exception("notifications: listener crashed")

    async def _listen(self) -> None:
        import asyncio

        from winsdk.windows.ui.notifications import NotificationKinds
        from winsdk.windows.ui.notifications.management import UserNotificationListener

        listener = UserNotificationListener.current
        access = int(await listener.request_access_async())
        if access != _ACCESS_ALLOWED:
            logger.warning("notifications: access not granted (status=%s)", access)
            return
        logger.info("notifications source: listening for system toasts")

        previous: set[str] | None = None
        while not self._stop.is_set():
            try:
                items = list(
                    await listener.get_notifications_async(NotificationKinds.TOAST)
                )
            except Exception:
                logger.exception("notifications: poll failed")
                await asyncio.sleep(self._interval)
                continue
            ids = [str(getattr(n, "id", "")) for n in items]
            if previous is None:
                previous = set(ids)  # forward-only: skip the startup backlog (A1)
            else:
                fresh = new_ids(previous, ids)
                if fresh and self._emit is not None:
                    by_id = dict(zip(ids, items))
                    for nid in fresh:
                        _safe_emit(
                            self._emit,
                            Event.new(
                                "notifications",
                                "toast",
                                notification_payload(by_id[nid]),
                            ),
                        )
                previous = set(ids)
            await asyncio.sleep(self._interval)


register_source("notifications", NotificationSource)
