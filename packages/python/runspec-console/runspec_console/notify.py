"""
notify.py — *sending* desktop notifications (the counterpart to the listener).

`sources/notifications.py` *reads* system toasts via
``Windows.UI.Notifications.Management.UserNotificationListener`` — which needs the
"let apps access my notifications" consent. This module *sends* a toast via
``Windows.UI.Notifications.ToastNotificationManager`` (no ``.Management``), which
is gated only by the per-app "send notifications" toggle. The two are
independent: a machine with reading **off** can still send.

Used to alert the user when an agent tool call is parked on the human-in-the-loop
autonomy gate (``Bridge._register_confirm``) and the console isn't in focus. The
toast is the attention-getter; clicking it calls ``on_click`` (in-process — works
without a COM activator because the console is alive and holds the toast object),
and the user answers in the existing in-app Approve/Deny card.

Windows-only: every entry point is a safe no-op on other platforms (and when the
optional ``[notifications]`` extra isn't installed). The winrt imports stay
inside the functions so the module imports cleanly anywhere; the pure helpers
(``build_confirm_message`` and friends) are unit-tested with no winrt at all.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable

logger = logging.getLogger("runspec.console.notify")

# Stable AppUserModelID so toasts carry one identity (and Action Center groups
# them). Set on the process at startup via set_app_id(); also passed explicitly
# to create_toast_notifier so an unpackaged app still resolves an identity.
_APP_ID = "runspec.console"

# Keep the toast body to one readable line — the detail lives in the in-app card.
_MAX_BODY = 140
_MAX_VAL = 40


# ── pure helpers (unit-tested, no winrt) ──────────────────────────────────────


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _short_val(value: Any) -> str:
    s = str(value)
    if len(s) > _MAX_VAL:
        s = s[: _MAX_VAL - 1].rstrip() + "…"
    return s


def summarize_args(tool_input: dict[str, Any] | None) -> str:
    """A compact, truncated one-line ``k=v`` summary of the tool input."""
    if not tool_input:
        return "Open runspec console to approve or deny."
    summary = ", ".join(f"{k}={_short_val(v)}" for k, v in tool_input.items())
    if len(summary) > _MAX_BODY:
        summary = summary[: _MAX_BODY - 1].rstrip() + "…"
    return summary


def build_confirm_message(
    tool_name: str, tool_input: dict[str, Any] | None
) -> tuple[str, str]:
    """(title, body) for an autonomy-gate confirmation toast.

    ``tool_name`` may carry the ``host__name`` / ``mcp__…__name`` prefix the gate
    uses internally; only the trailing display name is shown."""
    display = tool_name.split("__", 1)[-1]
    return (f"Approval needed: {display}", summarize_args(tool_input))


def _toast_xml(title: str, body: str) -> str:
    return (
        "<toast><visual><binding template='ToastGeneric'>"
        f"<text>{_xml_escape(title)}</text>"
        f"<text>{_xml_escape(body)}</text>"
        "</binding></visual></toast>"
    )


# ── Windows surface (winrt; no-op elsewhere) ──────────────────────────────────


def available() -> tuple[bool, str]:
    """Whether a toast can be *sent* here. Checks the sender namespaces only —
    sending needs no notification-access consent (that gates the listener)."""
    if sys.platform != "win32":
        return (False, "Desktop notifications require Windows")
    try:
        from winrt.windows.data.xml.dom import XmlDocument  # noqa: F401
        from winrt.windows.ui.notifications import (  # noqa: F401
            ToastNotificationManager,
        )
    except Exception:
        return (
            False,
            "winrt not installed — pip install runspec-console[notifications]",
        )
    return (True, "")


def set_app_id() -> None:
    """Pin the process AppUserModelID so toasts get a stable identity. Cheap,
    Windows-only, best-effort — call once at startup before any toast."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(_APP_ID)
        )
    except Exception:
        logger.debug("set_app_id failed", exc_info=True)


def notify(
    title: str, body: str, *, on_click: Callable[[], None] | None = None
) -> bool:
    """Raise a desktop toast. Returns True if shown, False on any failure or
    non-Windows. Never raises — the caller's flow must not depend on it."""
    ok, _ = available()
    if not ok:
        return False
    try:
        from winrt.windows.data.xml.dom import XmlDocument
        from winrt.windows.ui.notifications import (
            ToastNotification,
            ToastNotificationManager,
        )

        xml = XmlDocument()
        xml.load_xml(_toast_xml(title, body))
        toast = ToastNotification(xml)
        if on_click is not None:

            def _activated(_sender: Any, _args: Any) -> None:
                try:
                    on_click()
                except Exception:
                    logger.debug("toast on_click failed", exc_info=True)

            toast.add_activated(_activated)
        ToastNotificationManager.create_toast_notifier(_APP_ID).show(toast)
        return True
    except Exception:
        logger.debug("desktop notification failed", exc_info=True)
        return False
