"""winlock.py — minimal Windows lock-screen state probe.

A cheap, synchronous probe of whether the current interactive session is locked.
Two callers, two postures:

* ``is_screen_locked() -> bool`` — best-effort, **fail-open** (any error or
  non-Windows ⇒ ``False``/not-locked). Used by the Rota "Take a break" feature:
  when a timed break elapses but the screen is still locked, the rota stays
  suspended until unlock; if we can't tell, the break just auto-resumes.

* ``screen_lock_state() -> "locked" | "unlocked" | "unknown"`` — the tri-state
  the lock **auto-suspend** governance control uses. ``"unknown"`` happens only
  when the probe was expected to work (Windows) but raised; that control treats
  ``"unknown"`` as locked (**fail-closed**) so an uncertain probe can't let
  automation run unattended. Non-Windows returns ``"unlocked"`` (there is no lock
  concept, so it must never trip a fail-closed gate and disable automation).

Mechanism: ``OpenInputDesktop`` returns NULL when the input desktop is the secure
(Winlogon) desktop — i.e. the session is locked — and a valid handle otherwise.
This works on the console session and over RDP without a message pump, so it can
be called inline from the rota gate.
"""

from __future__ import annotations

import sys

# DESKTOP_SWITCHDESKTOP — the access right MSDN documents for the OpenInputDesktop
# lock probe; the call fails with this right when the secure desktop is up.
_DESKTOP_SWITCHDESKTOP = 0x0100


def screen_lock_state() -> str:
    """``"locked"`` | ``"unlocked"`` | ``"unknown"``.

    ``"unknown"`` is returned only on Windows when the probe raises — callers that
    need a governance posture treat it as locked (fail-closed). Non-Windows is
    ``"unlocked"`` (no lock concept; never trips a fail-closed gate)."""
    if sys.platform != "win32":
        return "unlocked"
    try:
        import ctypes

        user32 = ctypes.windll.user32
        # OpenInputDesktop(flags=0, inherit=False, desiredAccess). A NULL handle
        # means the input desktop isn't accessible — the locked/secure desktop.
        hdesk = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if not hdesk:
            return "locked"
        user32.CloseDesktop(hdesk)
        return "unlocked"
    except Exception:
        return "unknown"


def is_screen_locked() -> bool:
    """True only when the session is *definitely* locked (fail-open: ``unknown`` ⇒
    False). Best-effort — never raises. Used by the lenient "Take a break" path."""
    return screen_lock_state() == "locked"
