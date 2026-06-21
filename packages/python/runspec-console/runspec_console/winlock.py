"""winlock.py — minimal Windows lock-screen state check.

``is_screen_locked()`` is a cheap, synchronous, best-effort probe of whether the
current interactive session is locked. It backs the Rota "Take a break" feature:
when a timed break elapses but the screen is still locked, the operator is plainly
still away, so the rota stays suspended until they unlock.

Mechanism: ``OpenInputDesktop`` returns NULL when the input desktop is the secure
(Winlogon) desktop — i.e. the session is locked — and a valid handle otherwise.
This works on the console session and over RDP without a message pump, so it can
be called inline from the rota gate. Any failure (and every non-Windows platform)
is treated as *not locked*, so the break simply auto-resumes — lock-honouring is a
Windows nicety, never a hard dependency.

The broader, event-driven "pause automation while locked" feature (a follow-up)
will build on this same probe.
"""

from __future__ import annotations

import sys

# DESKTOP_SWITCHDESKTOP — the access right MSDN documents for the OpenInputDesktop
# lock probe; the call fails with this right when the secure desktop is up.
_DESKTOP_SWITCHDESKTOP = 0x0100


def is_screen_locked() -> bool:
    """True when the current session's screen is locked; False on any error or
    non-Windows platform (best-effort — never raises)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        # OpenInputDesktop(flags=0, inherit=False, desiredAccess). A NULL handle
        # means the input desktop isn't accessible — the locked/secure desktop.
        hdesk = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if not hdesk:
            return True
        user32.CloseDesktop(hdesk)
        return False
    except Exception:
        return False
