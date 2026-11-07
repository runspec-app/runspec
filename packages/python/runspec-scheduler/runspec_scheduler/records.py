"""records.py — record a fire that never reached ``runspec exec``.

Almost every fire is recorded by ``runspec exec`` itself (the daemon and the
cron line both run it). A few outcomes happen before exec could start, and
without this module they would leave no trace in History:

* a fire the daemon **missed** (it started later than the misfire grace — a VM
  pause, a clock step, the host asleep) → a ``skipped`` run;
* a fire the daemon **couldn't start** (the runnable vanished mid ``pip
  install``, its args no longer match its spec, ``runspec`` itself missing) → a
  ``failed`` run.

Each is one finished row in the runs index under the schedule's ``sched:<id>``
label plus an ``exec_result`` record in a per-run log file — the same shape
``runspec exec`` writes for its own skips, so ``runspec-scheduler status``, the
console's History and ``runspec logs`` read them like any other run. The daemon
runs as the venv owner, so it writes the index directly (no logs inbox).
Best-effort: recording must never take the daemon down.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

from runspec import runsdb

from .cronsync import LABEL_PREFIX

logger = logging.getLogger("runspec_scheduler.records")

_EXEC_LOGGER = "runspec.exec"


def _log_dir() -> Path:
    """Where this venv's runs are recorded (core's own choice, else ``{venv}/logs``)."""
    try:
        from runspec.logging_setup import _resolve_log_dir

        return _resolve_log_dir()
    except Exception:  # noqa: BLE001 — older core / unusual FS
        return Path(sys.prefix) / "logs"


def _user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001 — Windows / unknown uid
        return os.environ.get("USER", "")


def record_run(
    sched_id: str,
    runnable: str,
    *,
    status: str,
    exit_code: int,
    reason: str,
    message: str,
    when: datetime.datetime | None = None,
    log_dir: Path | None = None,
) -> str | None:
    """Write one finished run (``status`` = ``skipped`` / ``failed``) labelled
    ``sched:<id>``. Returns the run id, or ``None`` if nothing could be written."""
    try:
        d = log_dir or _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        start = when.astimezone(datetime.timezone.utc) if when else now
        run_id = str(uuid.uuid4())
        name = f"{runnable}.{start.strftime('%Y%m%dT%H%M%SZ')}.{run_id}.log"
        label = f"{LABEL_PREFIX}{sched_id}"
        runsdb.begin_run(
            d,
            {
                "run_id": run_id,
                "runnable": runnable,
                "command": "",
                "start_ts": start.isoformat(),
                "end_ts": now.isoformat(),
                "duration_ms": 0,
                "status": status,
                "exit_code": exit_code,
                "user": _user(),
                "agent": True,
                "trigger": "schedule",
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "label": label,
                "os_user": _user() or None,
                "source": "direct",
                "log_file": name,
            },
        )
        record = {
            "ts": now.isoformat(),
            "level": "INFO" if status == "skipped" else "ERROR",
            "logger": _EXEC_LOGGER,
            "message": message,
            "extra": {
                "event": "exec_result",
                "status": status,
                "exit_code": exit_code,
                "duration_ms": 0,
                "label": label,
                "trigger": "schedule",
                "reason": reason,
                "run_id": run_id,
            },
        }
        with open(d / name, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return run_id
    except Exception:  # noqa: BLE001 — never take the daemon down
        logger.exception("could not record %s run for schedule %r", status, sched_id)
        return None


def audit(event: str, outcome: str, *, target: dict[str, Any] | None = None, detail: dict[str, Any] | None = None) -> None:
    """``runspec.audit`` without ever raising (the ledger is best-effort here)."""
    with contextlib.suppress(Exception):
        import runspec

        runspec.audit(event, outcome, target=target, detail=detail)
