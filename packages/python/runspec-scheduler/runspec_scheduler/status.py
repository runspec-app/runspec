"""status.py — what the shared schedules are doing, from three sources.

``runspec-scheduler status`` joins, per schedule id:

* the **config** (``runspec_scheduler.toml``) — what should run;
* the **crontab** of each ``run_as`` account — what is installed (and whether the
  line is the current ``runspec exec`` form or a pre-0.3 legacy line);
* the **runs index** (``runspec_runs.db``) rows labelled ``sched:<id>`` by
  ``runspec exec`` — what actually ran, with the last run's captured output from
  its audit log.

Read-only, and every source is best-effort: a crontab the invoking user can't
read reports ``installed: null`` rather than failing the whole status (reading
another account's crontab needs passwordless ``sudo -n``). This is the read
runspec-console's **Shared schedules** view makes over SSH.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from runspec import logs as rs_logs
from runspec import runsdb

from .config import ScheduleEntry, load_schedules
from .cronsync import LABEL_PREFIX, MARKER_PREFIX, _crontab_argv, _installable, read_managed_users, resolve_entry
from .runner import venv_name
from .timing import next_fire_time

#: Bytes of each captured stream returned for the last run (the log keeps more).
DEFAULT_OUTPUT_BYTES = 8 * 1024


def _effective_user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001 — Windows / unknown uid
        return ""


# ── crontab inventory ───────────────────────────────────────────────────────────


def parse_installed(lines: list[str]) -> dict[str, str]:
    """``{schedule id: "exec" | "legacy"}`` for every block we wrote in a crontab.

    ``exec`` is the current line (``runspec exec --label …``); ``legacy`` a line
    from a pre-0.3 sync that runs the binary directly (unrecorded skips, no
    captured output) — re-run ``sync`` to upgrade it."""
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        if not line.startswith(MARKER_PREFIX):
            continue
        sid = line[len(MARKER_PREFIX) :].strip()
        job = lines[i + 1] if i + 1 < len(lines) else ""
        out[sid] = "exec" if " exec --label " in job else "legacy"
    return out


def _read_crontab_noninteractive(user: str, become_method: str) -> list[str] | None:
    """An account's crontab lines, or ``None`` when it can't be read without a
    prompt (another account and no passwordless ``sudo -n``)."""
    import subprocess

    me = _effective_user()
    target = "" if (not user or user == me) else user
    flags = "-n" if target and become_method in ("sudo", "") else None
    try:
        proc = subprocess.run(
            _crontab_argv("-l", target, become_method or "sudo", flags),
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        if "no crontab for" in (proc.stderr or "").lower():
            return []
        return None
    return proc.stdout.splitlines()


# ── runs index ──────────────────────────────────────────────────────────────────


def _runs_for(label: str, limit: int) -> list[dict[str, Any]]:
    """Newest-first rows labelled ``label`` across this venv's log dirs, each
    tagged with the ``_dir`` it came from (to find its log file)."""
    rows: list[dict[str, Any]] = []
    for d in rs_logs.log_dirs():
        for r in runsdb.list_runs(d, label=label, limit=limit):
            r["_dir"] = str(d)
            rows.append(r)
    rows.sort(key=lambda r: r.get("start_ts") or "", reverse=True)
    return rows[:limit]


def _run_summary(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("run_id", "status", "exit_code", "start_ts", "end_ts", "duration_ms", "trigger", "user", "log_file", "log_pruned_at")
    return {k: row.get(k) for k in keys}


def last_output(row: dict[str, Any], limit: int = DEFAULT_OUTPUT_BYTES) -> dict[str, Any] | None:
    """The captured output + result of one run from its audit log: ``{stdout,
    stderr, truncated, result, errors}`` (``errors`` = the runnable's own ERROR
    records, most recent last). ``None`` when the log file is gone or unreadable."""
    name = row.get("log_file")
    if not name or not row.get("_dir"):
        return None
    path = Path(row["_dir"]) / Path(str(name)).name
    run_id = row.get("run_id")
    out: dict[str, Any] = {"stdout": "", "stderr": "", "truncated": False, "result": None, "errors": []}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                extra = rec.get("extra") or {}
                if extra.get("run_id") != run_id:
                    continue
                event = extra.get("event")
                if event == "exec_output" and extra.get("stream") in ("stdout", "stderr"):
                    text = str(extra.get("text") or "")
                    if len(text.encode("utf-8")) > limit:
                        text = text.encode("utf-8")[-limit:].decode("utf-8", errors="replace")
                        out["truncated"] = True
                    out["truncated"] = out["truncated"] or bool(extra.get("truncated"))
                    out[extra["stream"]] = text
                elif event == "exec_result":
                    out["result"] = {"message": rec.get("message"), **{k: extra.get(k) for k in ("status", "exit_code", "duration_ms", "reason")}}
                elif rec.get("level") in ("ERROR", "CRITICAL") and rec.get("logger") != "runspec.exec":
                    out["errors"].append(str(rec.get("message") or ""))
    except OSError:
        return None
    out["errors"] = out["errors"][-5:]
    return out


# ── the join ────────────────────────────────────────────────────────────────────


def status(config_path: Path, *, runs: int = 5, output_bytes: int = DEFAULT_OUTPUT_BYTES) -> dict[str, Any]:
    """The full status payload (see module docstring)."""
    payload: dict[str, Any] = {
        "ok": True,
        "venv": venv_name(),
        "config": str(config_path),
        "backend": "cron",
        "schedules": [],
        "stale": [],
        "errors": [],
    }
    try:
        entries: list[ScheduleEntry] = load_schedules(config_path)
    except Exception as exc:  # noqa: BLE001 — a bad config is reported, not raised
        payload["ok"] = False
        payload["errors"].append(f"could not read {config_path}: {exc}")
        entries = []

    # Crontabs of every account a schedule targets + any we've managed before.
    accounts: dict[str, str] = {e.run_as: e.become_method for e in entries}
    for u in read_managed_users(config_path):
        accounts.setdefault(u, "sudo")
    installed: dict[str, dict[str, str] | None] = {user: (None if (lines := _read_crontab_noninteractive(user, method)) is None else parse_installed(lines)) for user, method in accounts.items()}
    configured_ids = {e.id for e in entries}
    for user, ids in installed.items():
        for sid in sorted(ids or {}):
            if sid not in configured_ids:
                payload["stale"].append({"id": sid, "user": user or "(self)"})

    for entry in entries:
        record = entry.to_dict()
        installable, reason = _installable(entry)
        if installable:
            target, reason = resolve_entry(entry)
            installable = target is not None
        record["installable"] = installable
        record["not_installable_reason"] = reason or None
        user_ids = installed.get(entry.run_as)
        record["installed"] = None if user_ids is None else entry.id in user_ids
        record["line"] = None if user_ids is None else user_ids.get(entry.id)
        nxt = next_fire_time(entry.when) if entry.enabled else None
        record["next_run"] = nxt.isoformat() if nxt else None

        rows = _runs_for(f"{LABEL_PREFIX}{entry.id}", runs)
        record["runs"] = [_run_summary(r) for r in rows]
        finished = next((r for r in rows if r.get("status") != "running"), None)
        record["last_output"] = last_output(finished, output_bytes) if finished else None
        payload["schedules"].append(record)
    return payload
