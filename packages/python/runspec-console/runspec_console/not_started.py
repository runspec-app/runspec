"""Runs the console launched that never got as far as the runnable.

A runnable writes its own run record (``run_summary``) once ``parse()`` runs.
When the console couldn't get that far — the host was unreachable, the
runnable isn't installed there, the run-as escalation was refused, or the
venv's Python couldn't start the runnable at all — no record exists anywhere,
so the run was missing from History and the console log. The console writes
its own record for those runs into ``~/logs/runspec-console-not-started.log``
(one JSON line per run, ``run_summary``-shaped), and History reads it back,
shown under the host the run was meant for.

Pure helpers only (no bridge, no I/O beyond reading the file): the bridge's
reporting mixin does the writing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

STEM = "runspec-console-not-started"
FILE = STEM + ".log"
# The file rolls to ``<stem>.1.log`` at this size (one backup), like the tools
# audit log, so it stays bounded.
MAX_BYTES = 2 * 1024 * 1024
# How many output lines a record keeps (the reason is usually in the last few).
OUTPUT_LINES = 40
STATUS = "not_started"

_STRIP = "✗⏱ "
# The venv-python env prelude ends ``os.execvp(sys.argv[1], …)``; when the
# runnable isn't there the traceback runs from ``File "<string>"`` into os.py.
_EXEC_FRAME = re.compile(r'File ".*os\.py", line \d+, in _?execv')
_PRELUDE_FRAME = re.compile(r'File "<string>", line \d+')
_EXC_LINE = re.compile(r"^([A-Za-z_]+(?:Error|Exception)):\s*(.*)$")
# The POSIX sh prelude: ``sh: 1: exec: /venv/bin/x: not found``.
_SH_EXEC = re.compile(r"exec: .*(not found|Permission denied|No such file)")


def launch_failure(lines: Iterable[str], exit_code: int) -> str:
    """Why a finished run never reached the runnable, or ``""`` when it did.

    Reads the run's output for the console's own markers (SSH connection
    failed, run-as refused) and for the env prelude's failure to start the
    runnable (Python ``os.execvp`` traceback, or ``sh``'s ``exec: … not
    found``). A runnable's own failure is never matched: it has its own record."""
    if exit_code == 0:
        return ""
    text = [str(x) for x in lines]
    for raw in text:
        s = raw.strip()
        if s.startswith("✗  SSH connection failed") or s.startswith(
            "✗  Couldn't run as"
        ):
            return s.lstrip(_STRIP).strip()
    for i, raw in enumerate(text):
        if not _PRELUDE_FRAME.search(raw):
            continue
        if not any(_EXEC_FRAME.search(x) for x in text[i + 1 : i + 12]):
            continue
        err = ""
        for later in text[i + 1 :]:
            m = _EXC_LINE.match(later.strip())
            if m:
                err = f"{m.group(1)}: {m.group(2)}".strip().rstrip(":")
        return "the venv's Python couldn't start the runnable" + (
            f" ({err})" if err else ""
        )
    if exit_code in (126, 127):
        for raw in text:
            if _SH_EXEC.search(raw):
                return f"the runnable couldn't be started ({raw.strip()})"
    return ""


def build_record(
    *,
    run_id: str,
    ts: str,
    host: str,
    runnable: str,
    reason: str,
    exit_code: int = -1,
    group: str = "",
    command_path: list[str] | None = None,
    args: dict[str, Any] | None = None,
    operator: str = "",
    run_as: str = "",
    trigger: str = "",
    agent: bool = False,
    runbook: str = "",
    output: Iterable[str] = (),
) -> dict[str, Any]:
    """One ``run_summary``-shaped JSON line for a run that never started."""
    tail = [str(x) for x in output if str(x).strip()][-OUTPUT_LINES:]
    return {
        "ts": ts,
        "level": "WARNING",
        "message": f"didn't start: {reason}",
        "extra": {
            "event": "run_summary",
            "status": STATUS,
            "run_id": run_id,
            "runnable": runnable,
            "host": host,
            "group": group,
            "command_path": list(command_path or []),
            "args": dict(args or {}),
            "user": operator,
            "user_target": run_as,
            "trigger": trigger,
            "agent": bool(agent),
            "runbook": runbook,
            "exit_code": int(exit_code),
            "duration_ms": 0,
            "label": "console",
            "reason": reason,
            "output": tail,
        },
    }


def to_row(record: dict[str, Any]) -> dict[str, Any] | None:
    """A stored record → the History row shape (``HistoryRecord``)."""
    extra = record.get("extra") if isinstance(record, dict) else None
    if not isinstance(extra, dict) or extra.get("status") != STATUS:
        return None
    run_id = str(extra.get("run_id") or "")
    runnable = str(extra.get("runnable") or "")
    if not run_id or not runnable:
        return None
    ts = str(record.get("ts") or "")
    reason = str(extra.get("reason") or "")
    lines = [{"ts": ts, "level": "ERROR", "message": f"Didn't start: {reason}"}]
    lines += [
        {"ts": ts, "level": "INFO", "message": str(m)}
        for m in extra.get("output") or []
    ]
    args = extra.get("args")
    return {
        "id": hashlib.md5((run_id + runnable).encode()).hexdigest()[:12],
        "runId": run_id,
        "runnable": runnable,
        "group": str(extra.get("group") or ""),
        "host": str(extra.get("host") or ""),
        "operator": str(extra.get("user") or ""),
        "runAs": str(extra.get("user_target") or ""),
        "exitCode": int(extra.get("exit_code", -1)),
        "durationMs": 0,
        "ts": ts,
        "args": args if isinstance(args, dict) else {},
        "argSources": {},
        "commandPath": list(extra.get("command_path") or []),
        "logLines": lines,
        "initiatedBy": "llm" if extra.get("agent") else "user",
        "trigger": str(extra.get("trigger") or ""),
        "runbook": str(extra.get("runbook") or ""),
        "label": "console",
        "status": STATUS,
        "reason": reason,
        "kind": "runnable",
    }


def files(log_dir: Path) -> list[Path]:
    """The live file and its one rollover, oldest first."""
    return [log_dir / f"{STEM}.1.log", log_dir / FILE]


def read_rows(
    log_dir: Path,
    *,
    since_date: str = "",
    hosts: set[str] | None = None,
    exclude_hosts: set[str] | None = None,
    exclude_runnables: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """History rows from the file(s), newest first. ``hosts`` keeps only those
    target hosts; ``exclude_hosts`` drops these; ``since_date`` (YYYY-MM-DD)
    drops older runs."""
    rows: list[dict[str, Any]] = []
    for path in files(log_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            if not raw.strip():
                continue
            try:
                row = to_row(json.loads(raw))
            except (ValueError, TypeError):
                continue
            if row is None:
                continue
            if hosts is not None and row["host"] not in hosts:
                continue
            if exclude_hosts and row["host"] in exclude_hosts:
                continue
            if row["runnable"] in exclude_runnables:
                continue
            if since_date and row["ts"][:10] < since_date:
                continue
            rows.append(row)
    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return rows


def find_row(log_dir: Path, run_id: str) -> dict[str, Any] | None:
    """The row for ``run_id``, if the console recorded it."""
    if not run_id:
        return None
    for row in read_rows(log_dir):
        if row["runId"] == run_id:
            return row
    return None
