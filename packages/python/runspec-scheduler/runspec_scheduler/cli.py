"""cli.py — the `runspec-scheduler` entry point.

One binary, several verbs. ``rs.parse("runspec-scheduler")`` resolves the
subcommand + args; we dispatch on ``spec.runspec_command``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import runspec as rs

from .config import ScheduleEntry, config_path, load_config, load_schedules, load_settings, parse_command, parse_duration, remove_schedule, set_backend, upsert_schedule
from .daemon import configure_logging, run_daemon, take_lock
from .discovery import check_invocation, resolve_target
from .envfile import capture_env, remove_env_file, write_env_file
from .runner import venv_name
from .timing import next_fire_time, parse_when


def _opt(arg: Any) -> Any:
    """Underlying value of a runspec ``Arg``; '' / None → None."""
    value = getattr(arg, "value", arg)
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def _parse_json_arg(raw: Any, what: str) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError as exc:
        _emit({"error": f"invalid JSON for {what}: {exc}"})
        sys.exit(2)


def _cfg_path(spec: rs.RunSpec) -> Path:
    return config_path(_opt(spec.config))


#: The venv lock when ``main`` took it before parsing (see :func:`_early_lock`).
_HELD_LOCK: Any = None


def _close_own_run() -> None:
    """End this process's own run record now. ``rs.parse()`` opened one for the
    ``run`` invocation; left alone it would sit in History as "running" for the
    daemon's whole life. The daemon's start and stop are audit events instead,
    and each fire is its own run."""
    import contextlib

    with contextlib.suppress(Exception):
        from runspec import logging_setup

        finish = getattr(logging_setup, "_emit_run_summary", None)
        if finish is not None:
            finish()
    # parse() tees print() into the run's log; the daemon logs for itself.
    sys.stdout = getattr(sys.stdout, "_original", sys.stdout)


def _cmd_run(spec: rs.RunSpec) -> None:
    _close_own_run()
    configure_logging()
    sys.exit(run_daemon(_cfg_path(spec), reload_interval=int(spec.reload_interval.value), lock_fh=_HELD_LOCK))


def _argv_config(argv: list[str]) -> str | None:
    """``--config`` from a raw argv (``--config X`` or ``--config=X``)."""
    for i, tok in enumerate(argv):
        if tok == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--config="):
            return tok.split("=", 1)[1]
    return None


#: Set by ``run --detach`` for its child: the fd of the venv lock the parent
#: took, so the lock passes to the child with no moment where nobody holds it.
LOCK_FD_ENV = "RUNSPEC_SCHEDULER_LOCK_FD"


def _argv_words(argv: list[str]) -> list[str]:
    """The non-option words of ``argv`` (skipping ``--config``'s value)."""
    words: list[str] = []
    skip = False
    for tok in argv:
        if skip:
            skip = False
        elif tok == "--config":
            skip = True
        elif not tok.startswith("-"):
            words.append(tok)
    return words


def _adopt_lock_fd() -> Any:
    """The lock a ``run --detach`` parent handed down, or ``None``."""
    import os

    raw = os.environ.pop(LOCK_FD_ENV, "")
    if not raw.isdigit():
        return None
    fd = int(raw)
    try:
        os.set_inheritable(fd, False)  # fires must not hold the lock open
        fh = os.fdopen(fd, "a+", encoding="utf-8")
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
    except OSError:
        return None
    return fh


def _detach(argv: list[str], fh: Any) -> None:
    """Start ``run`` (without ``--detach``) in a new session, holding ``fh``'s
    lock, and exit 0. Its output goes to ``logs/scheduler.stdout.log``."""
    import os
    import subprocess

    from .keeper import venv_root

    exe = Path(sys.argv[0]) if sys.argv and Path(sys.argv[0]).name.startswith("runspec-scheduler") else None
    cmd = [str(exe)] if exe else [sys.executable, "-c", "from runspec_scheduler.cli import main; main()"]
    child_args = [t for t in argv if t != "--detach"]
    log = venv_root() / "logs" / "scheduler.stdout.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env[LOCK_FD_ENV] = str(fh.fileno())
    with open(log, "ab") as out:
        proc = subprocess.Popen(
            [*cmd, *child_args],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
            pass_fds=(fh.fileno(),),
            start_new_session=True,
            close_fds=True,
        )
    fh.close()  # the child's copy keeps the lock
    print(f"runspec-scheduler: started for this venv (pid {proc.pid})")
    sys.exit(0)


def _early_lock(argv: list[str]) -> None:
    """For ``run``: take the venv lock before ``rs.parse()``. A second start (a
    watchdog every few minutes) then exits 0 at once, without a run record or a
    log line of its own. ``run --detach`` starts the daemon in the background
    holding the lock; its child adopts that lock instead of taking a new one."""
    global _HELD_LOCK
    words = _argv_words(argv)
    if not words or words[0] != "run" or "--help" in argv or "-h" in argv:
        return
    adopted = _adopt_lock_fd()
    if adopted is not None:
        _HELD_LOCK = adopted
        return
    try:
        fh, holder = take_lock(config_path(_argv_config(argv)).with_name("runspec_scheduler.lock"))
    except (OSError, ImportError):
        return  # the daemon reports it properly after parsing
    if holder is not None:
        who = f" (pid {holder})" if holder else ""
        print(f"runspec-scheduler: already running for this venv{who}")
        sys.exit(0)
    if "--detach" in argv:
        _detach(argv, fh)
    _HELD_LOCK = fh


def _cmd_add(spec: rs.RunSpec) -> None:
    path = _cfg_path(spec)
    sid = str(spec.id.value)
    runnable = str(spec.runnable.value)
    when = parse_when(
        {
            "cron": _opt(spec.cron) or "",
            "every": _opt(spec.every) or "",
            "timezone": _opt(spec.timezone) or "",
        }
    )
    try:
        settings = load_settings(path)
    except ValueError as exc:
        _emit({"error": f"invalid config {path}: {exc}"})
        sys.exit(2)
    if when.every and settings.backend != "daemon":
        _emit({"error": "interval schedules (--every) need the daemon backend; this venv uses cron — use --cron, or switch with: runspec-scheduler use daemon"})
        sys.exit(2)
    timeout: int | None = None
    if _opt(spec.timeout) is not None:
        try:
            timeout = parse_duration(_opt(spec.timeout), "--timeout")
        except ValueError as exc:
            _emit({"error": str(exc)})
            sys.exit(2)
    args = _parse_json_arg(_opt(spec.args), "--args") or {}
    if not isinstance(args, dict):
        _emit({"error": "--args must be a JSON object"})
        sys.exit(2)

    target = resolve_target(runnable)
    if target is None:
        _emit({"error": f"runnable '{runnable}' is not installed in this venv ({venv_name()})"})
        sys.exit(2)
    try:
        command = parse_command(_opt(spec.command))
    except ValueError as exc:
        _emit({"error": str(exc)})
        sys.exit(2)
    problem = check_invocation(target.spec, command, args)
    if problem:
        _emit({"error": f"'{runnable}': {problem}"})
        sys.exit(2)

    cred_vars = _parse_json_arg(_opt(spec.cred_vars), "--cred-vars")
    env_file = ""
    if cred_vars:
        if not isinstance(cred_vars, list):
            _emit({"error": "--cred-vars must be a JSON array of variable names"})
            sys.exit(2)
        if not target.run_as:
            _emit({"error": f"credentials require a run_as runnable, but '{runnable}' declares none"})
            sys.exit(2)
        env = capture_env([str(v) for v in cred_vars])
        try:
            env_file = write_env_file(sid, env, target.run_as, target.become_method, target.become_flags)
        except (RuntimeError, ValueError) as exc:
            _emit({"error": f"could not write credential env file: {exc}"})
            sys.exit(1)

    entry = ScheduleEntry(
        id=sid,
        when=when,
        runnable=runnable,
        command=command,
        args=args,
        enabled=bool(spec.enabled.value),
        run_as=target.run_as,
        become_method=target.become_method,
        env_file=env_file,
        timeout=timeout,
        catch_up=bool(spec.catch_up.value),
    )
    upsert_schedule(path, entry)
    _emit({"ok": True, "id": sid, "runnable": runnable, "command": list(command), "run_as": target.run_as, "env_file": bool(env_file)})


def _cmd_remove(spec: rs.RunSpec) -> None:
    path = _cfg_path(spec)
    sid = str(spec.id.value)
    existing = next((e for e in load_schedules(path) if e.id == sid), None)
    removed = remove_schedule(path, sid)
    if existing and existing.env_file:
        remove_env_file(existing.env_file, existing.run_as, existing.become_method)
    _emit({"ok": removed, "id": sid})


def _cmd_list(spec: rs.RunSpec) -> None:
    path = _cfg_path(spec)
    out = []
    for entry in load_config(path)[1]:
        record = entry.to_dict()
        nxt = next_fire_time(entry.when) if entry.enabled else None
        record["next_run"] = nxt.isoformat() if nxt else None
        out.append(record)
    _emit({"schedules": out, "venv": venv_name(), "config": str(path), "backend": load_settings(path).backend})


def _cmd_status(spec: rs.RunSpec) -> None:
    from .status import status

    _emit(status(_cfg_path(spec), runs=max(1, int(spec.runs.value))))


def _cmd_run_now(spec: rs.RunSpec) -> None:
    path = _cfg_path(spec)
    sid = str(spec.id.value)
    settings, entries = load_config(path)
    entry = next((e for e in entries if e.id == sid), None)
    if entry is None:
        _emit({"error": f"no schedule with id '{sid}'"})
        sys.exit(2)
    if spec.detach.value:
        from .runnow import start

        res = start(entry, settings)
        _emit(res)
        sys.exit(0 if res.get("ok") else 1)
    timeout = entry.effective_timeout(settings) if settings.backend == "daemon" else (entry.timeout or 0)
    target = resolve_target(entry.runnable)
    if target is None:
        _emit({"error": f"runnable '{entry.runnable}' not found in this venv"})
        sys.exit(2)
    # Fire through core's `runspec exec` so a manual fire is recorded exactly like
    # a cron one — same `sched:<id>` label, observed exit code, captured output.
    import io

    from runspec.runexec import exec_runnable

    from .cronsync import LABEL_PREFIX, TRIGGER_RUN_NOW
    from .invocation import plan_invocation

    out, err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8"), io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    plan = plan_invocation(entry.runnable, target.spec, entry.command, dict(entry.args))
    env = {"RUNSPEC_AGENT": "1", **plan.env}
    if entry.env_file:
        env["RUNSPEC_ENV_FILE"] = entry.env_file
    rc = exec_runnable(
        entry.runnable,
        plan.argv,
        label=f"{LABEL_PREFIX}{sid}",
        trigger=TRIGGER_RUN_NOW,
        env=env,
        stdout=out,
        stderr=err,
        timeout=timeout or None,
    )
    out.flush()
    err.flush()
    _emit(
        {
            "id": sid,
            "runnable": entry.runnable,
            "exit_code": rc,
            "stdout": out.buffer.getvalue().decode("utf-8", errors="replace").strip(),  # type: ignore[attr-defined]
            "stderr": err.buffer.getvalue().decode("utf-8", errors="replace").strip(),  # type: ignore[attr-defined]
        }
    )
    sys.exit(0 if rc == 0 else 1)


def _cmd_sync(spec: rs.RunSpec) -> None:
    from .cronsync import sync

    result = sync(_cfg_path(spec), dry_run=bool(spec.dry_run.value))
    payload = result.to_dict()
    _emit(payload)
    if not payload["ok"]:
        sys.exit(1)


def _cmd_use(spec: rs.RunSpec) -> None:
    """Switch the venv between the cron and daemon backends, never leaving both
    able to fire: to the daemon, the marked crontab lines go first; to cron, the
    config switches first (the daemon stops firing at its next tick) and then
    the lines are written."""
    from .cronsync import remove_all, sync
    from .status import daemon_status

    path = _cfg_path(spec)
    backend = str(spec.backend.value)
    try:
        previous = load_settings(path).backend
    except ValueError as exc:
        _emit({"ok": False, "error": f"invalid config {path}: {exc}"})
        sys.exit(2)
    payload: dict[str, Any] = {"ok": True, "backend": backend, "previous": previous}
    if backend == "daemon":
        removed = remove_all(path).to_dict()
        payload["crontab"] = removed
        if not removed["ok"]:
            payload.update(ok=False, backend=previous, error="could not remove this venv's crontab lines, so the backend was not switched (both would fire every schedule)")
            _emit(payload)
            sys.exit(1)
        set_backend(path, "daemon")
        daemon = daemon_status(path)
        payload["daemon"] = daemon
        if daemon["state"] != "running":
            payload["hint"] = "the daemon isn't running — set up a keeper (runspec-scheduler install-service --write) or run it in the foreground: runspec-scheduler run"
    else:
        set_backend(path, "cron")
        result = sync(path).to_dict()
        payload["crontab"] = result
        if not result["ok"]:
            payload.update(ok=False, error="switched to cron, but some crontab lines could not be written — fix the errors and run: runspec-scheduler sync")
        if daemon_status(path)["state"] == "running":
            payload["hint"] = "the daemon is still running but fires nothing now; stop its keeper if this venv stays on cron"
    _emit(payload)
    if not payload["ok"]:
        sys.exit(1)


def _cmd_install_service(spec: rs.RunSpec) -> None:
    from . import keeper

    manager = str(_opt(spec.manager) or "auto")
    config = _opt(spec.config)
    plan = keeper.plan_remove(manager, config=config) if spec.remove.value else keeper.plan_install(manager, config=config, system=bool(spec.system.value))
    result = keeper.apply(plan) if spec.write.value else plan.to_dict(written=False)
    _emit(result)
    if not result.get("ok"):
        sys.exit(1)


def _cmd_settings(spec: rs.RunSpec) -> None:
    """Show the ``[scheduler]`` settings, or change the durations given. The
    daemon reloads a changed config by itself; the backend has ``use``."""
    from .config import SETTING_KEYS, set_settings

    path = _cfg_path(spec)
    changes = {key: _opt(getattr(spec, key)) for key in SETTING_KEYS}
    changes = {k: v for k, v in changes.items() if v is not None}
    try:
        settings = set_settings(path, changes) if changes else load_settings(path)
    except ValueError as exc:
        _emit({"ok": False, "error": str(exc)})
        sys.exit(2)
    _emit(
        {
            "ok": True,
            "config": str(path),
            "changed": sorted(changes),
            "settings": {
                "backend": settings.backend,
                "timeout": settings.timeout,
                "misfire_grace": settings.misfire_grace,
                "catch_up_window": settings.catch_up_window,
            },
        }
    )


def _cmd_restart(spec: rs.RunSpec) -> None:
    """Restart (or start) the daemon through this venv's keeper, then wait a
    little for its first heartbeat so the result says whether it came back."""
    from . import keeper
    from .status import daemon_status

    config = _opt(spec.config)
    plan = keeper.plan_restart(config=config)
    if spec.dry_run.value:
        out = plan.to_dict(written=False)
        if not plan.error and plan.actions:
            out["hint"] = "nothing was changed — re-run without --dry-run to restart"
        _emit(out)
        if plan.error:
            sys.exit(1)
        return
    before = daemon_status(_cfg_path(spec)).get("pid")
    result = keeper.apply(plan)
    if result.get("ok"):
        deadline = time.monotonic() + float(_opt(spec.wait) or 0)
        daemon = daemon_status(_cfg_path(spec))
        while time.monotonic() < deadline and not (daemon["state"] == "running" and daemon["pid"] != before):
            time.sleep(0.5)
            daemon = daemon_status(_cfg_path(spec))
        result["daemon"] = {k: daemon.get(k) for k in ("state", "pid", "version", "restart_needed")}
        if daemon["state"] != "running" or daemon["pid"] == before:
            result["warnings"] = [*result.get("warnings", []), "the daemon hasn't reported back yet; check runspec-scheduler status in a minute"]
    _emit(result)
    if not result.get("ok"):
        sys.exit(1)


def main() -> None:
    _early_lock(sys.argv[1:])
    spec = rs.parse("runspec-scheduler")
    command = spec.runspec_command
    if command == "run":
        _cmd_run(spec)
    elif command == "add":
        _cmd_add(spec)
    elif command == "remove":
        _cmd_remove(spec)
    elif command == "list":
        _cmd_list(spec)
    elif command == "run-now":
        _cmd_run_now(spec)
    elif command == "status":
        _cmd_status(spec)
    elif command == "sync":
        _cmd_sync(spec)
    elif command == "use":
        _cmd_use(spec)
    elif command == "install-service":
        _cmd_install_service(spec)
    elif command == "settings":
        _cmd_settings(spec)
    elif command == "restart":
        _cmd_restart(spec)
    else:  # pragma: no cover - require-command makes this unreachable
        _emit({"error": "no command — try: sync, status, use, settings, add, remove, list, run-now, run, restart, install-service"})
        sys.exit(2)
