"""Windows Application event-log writes — a native, tamper-*evident* record.

Used for the AI-use disclaimer acknowledgement: when an operator accepts the
notice we write one event to the Windows **Application** log, the OS facility made
for exactly this kind of record. It is written *in addition to* the console's own
audit-log line (``bridge_reporting._audit_disclaimer_ack``), which stays the
always-there operational record on every platform.

Honest framing (see docs/design/console-disclaimer.md): this is tamper-**evident**,
not tamper-**proof**. A local administrator can clear the Application log, but the
OS records the clear itself (System log event 104), so tampering is visible rather
than silent. Nothing local to a Windows box is immutable to a local admin; a truly
immutable record has to leave the machine (that is what the optional OTel telemetry
enrichment is for, when telemetry is configured).

Implementation notes:

- We shell out to the built-in ``eventcreate`` rather than depend on pywin32 —
  a standard (non-admin) user may write to the Application log, and ``eventcreate``
  does **not** require pre-registering an event source, so there is nothing to
  install and no admin step.
- ``event_id`` must be 1–1000 (an ``eventcreate`` constraint).
- Windows-only and best-effort: a no-op that returns False everywhere else, and it
  swallows every failure (locked-down box, missing ``eventcreate``, permissions),
  so it can never block acknowledgement or raise into a caller.
- It never touches the network and is called on the acknowledge click, never at
  startup, so it can't affect launch time.
"""

from __future__ import annotations

import subprocess
import sys

from .procutil import no_window_kwargs

# Fixed event id for a disclaimer acknowledgement (within eventcreate's 1–1000).
DISCLAIMER_EVENT_ID = 100

# eventcreate rejects a /D longer than ~1000 chars; keep well under it.
_MAX_MESSAGE = 1000


def is_windows() -> bool:
    """True on Windows, where the Application event log exists."""
    return sys.platform == "win32"


def write_application_event(
    *,
    source: str,
    message: str,
    event_id: int = DISCLAIMER_EVENT_ID,
    event_type: str = "INFORMATION",
) -> bool:
    """Write one INFORMATION event to the Windows Application log via
    ``eventcreate``. Returns True on a clean write, False on any failure or off
    Windows. Never raises.

    ``source`` is the event source label (we pass the branded app slug so a
    white-label install shows its own brand); ``message`` is the human-readable
    body (truncated to eventcreate's limit)."""
    if not is_windows():
        return False
    src = (source or "runspec-console").strip() or "runspec-console"
    body = (message or "").strip()[:_MAX_MESSAGE] or "disclaimer acknowledged"
    try:
        eid = int(event_id)
    except (TypeError, ValueError):
        eid = DISCLAIMER_EVENT_ID
    eid = min(1000, max(1, eid))
    etype = (
        event_type
        if event_type in ("INFORMATION", "WARNING", "ERROR")
        else "INFORMATION"
    )
    argv = [
        "eventcreate",
        "/T",
        etype,
        "/ID",
        str(eid),
        "/L",
        "APPLICATION",
        "/SO",
        src,
        "/D",
        body,
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            **no_window_kwargs(),
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 — the event-log write is best-effort
        return False
