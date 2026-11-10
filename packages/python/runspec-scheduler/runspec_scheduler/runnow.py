"""``run-now --detach``: start one schedule's fire in the background and return.

The fire is the same ``runspec exec --label sched:<id> --trigger
schedule:run-now --lock …`` command a cron line or the daemon runs (:func:`cronsync.exec_invocation`),
so it is recorded in the runs index like any other fire, and ``--lock`` makes it a
recorded *skipped* run when the schedule is already running. It runs as the
account a scheduled fire would: on the cron backend the schedule's ``run_as``
(escalated with ``sudo -n``/``su``, values on stdin as the SPEC env frame), on
the daemon backend the venv owner the daemon runs as. It starts in its own
session with no stdio, so the caller (often an SSH session from the console)
can hang up without stopping it.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import time
from typing import Any

from . import cronsync
from .config import ScheduleEntry, SchedulerSettings
from .cronsync import _group_key, _home_of, exec_invocation, resolve_entry

#: Variables a fire must not inherit from the caller (mirrors the daemon).
_DROP_ENV = (
    "RUNSPEC_RUN_ID",
    "RUNSPEC_RUN_LOG",
    "RUNSPEC_EXEC_UID",
    "RUNSPEC_LABEL",
    "RUNSPEC_TRIGGER",
    "RUNSPEC_CONFIG",
    "RUNSPEC_ENV_FILE",
    "RUNSPEC_RUNBOOK",
    "RUNSPEC_SCHEDULER_CONFIG",
    # a fire is a schedule, not the assistant, even when an agent started the daemon
    "RUNSPEC_AGENT",
)

#: How long to watch the fire before returning, to report one that ends at once.
SETTLE_S = 1.5


def _me() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — exotic passwd setups
        return ""


def plan(entry: ScheduleEntry, settings: SchedulerSettings) -> dict[str, Any]:
    """What a detached fire of ``entry`` runs: ``{argv, env, user, become_method,
    become_flags, home}`` (``user`` empty = as the caller), or ``{error}``."""
    target, reason = resolve_entry(entry)
    if target is None:
        return {"error": f"schedule '{entry.id}' can't run: {reason}"}
    on_daemon = settings.backend == "daemon"
    if on_daemon:
        user, method, flags = "", "sudo", ""
        timeout = entry.effective_timeout(settings)
    else:
        user, method, flags = _group_key(entry, target)
        timeout = entry.timeout or 0
    if user and user == _me():
        user = ""
    home = _home_of(user)
    env, argv = exec_invocation(
        str(cronsync.scripts_dir() / "runspec"),
        entry.runnable,
        dict(entry.args),
        entry.env_file,
        home=home,
        sched_id=entry.id,
        command=entry.command,
        spec=target.spec,
        timeout=timeout or None,
        trigger=cronsync.TRIGGER_RUN_NOW,
    )
    return {"argv": argv, "env": env, "user": user, "become_method": method, "become_flags": flags, "home": home}


def start(entry: ScheduleEntry, settings: SchedulerSettings, *, settle: float = SETTLE_S) -> dict[str, Any]:
    """Start ``entry``'s fire in its own session. Returns ``{ok, id, runnable,
    user, pid, finished?, exit_code?}`` or ``{ok: False, error}``. A fire that
    ends within ``settle`` seconds reports its exit code (its run is in the
    runs index either way)."""
    from runspec.become import build_become_argv, build_exec_argv

    p = plan(entry, settings)
    if "error" in p:
        return {"ok": False, "id": entry.id, "error": p["error"]}
    base_env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    user = p["user"]
    method = p["become_method"]
    flags = p["become_flags"] or ("-n" if method == "sudo" else "")  # never wait on a password prompt
    stdin: bytes | None = None
    if user:
        # Check the escalation first: a refused sudo would otherwise die silently
        # in the background with nothing recorded.
        probe = build_become_argv(["true"], user, method, flags or None)
        try:
            ok = subprocess.run(probe, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "id": entry.id, "error": f"can't run as {user}: {exc}"}
        if ok.returncode != 0:
            why = (ok.stderr or ok.stdout).strip().splitlines()
            return {"ok": False, "id": entry.id, "error": f"can't run as {user}: {why[-1] if why else f'exit {ok.returncode}'}"}
        argv, stdin = build_exec_argv(p["argv"], user, method, flags or None, env=p["env"])
        child_env = base_env
    else:
        argv = p["argv"]
        child_env = {**base_env, **p["env"]}
    home = p["home"]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,  # runspec exec captures the output into the run's log
            stderr=subprocess.DEVNULL,
            env=child_env,
            cwd=home if home and os.path.isdir(home) and not user else None,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return {"ok": False, "id": entry.id, "error": f"could not start {argv[0]}: {exc}"}
    if stdin and proc.stdin is not None:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except OSError:
            pass
    out: dict[str, Any] = {"ok": True, "id": entry.id, "runnable": entry.runnable, "user": user or _me(), "pid": proc.pid}
    deadline = time.monotonic() + max(0.0, settle)
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            out.update(finished=True, exit_code=rc)
            break
        time.sleep(0.1)
    return out
