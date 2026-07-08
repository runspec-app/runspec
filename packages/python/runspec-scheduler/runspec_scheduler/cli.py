"""cli.py — the `runspec-scheduler` entry point.

One binary, several verbs. ``rs.parse("runspec-scheduler")`` resolves the
subcommand + args; we dispatch on ``spec.runspec_command``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import runspec as rs

from .config import ScheduleEntry, config_path, load_schedules, remove_schedule, upsert_schedule
from .daemon import configure_logging, run_daemon
from .discovery import resolve_target
from .envfile import capture_env, remove_env_file, write_env_file
from .runner import run_runnable, venv_name
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


def _cmd_run(spec: rs.RunSpec) -> None:
    configure_logging()
    run_daemon(_cfg_path(spec), reload_interval=int(spec.reload_interval.value))


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
    args = _parse_json_arg(_opt(spec.args), "--args") or {}
    if not isinstance(args, dict):
        _emit({"error": "--args must be a JSON object"})
        sys.exit(2)

    target = resolve_target(runnable)
    if target is None:
        _emit({"error": f"runnable '{runnable}' is not installed in this venv ({venv_name()})"})
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
        args=args,
        enabled=bool(spec.enabled.value),
        run_as=target.run_as,
        become_method=target.become_method,
        env_file=env_file,
    )
    upsert_schedule(path, entry)
    _emit({"ok": True, "id": sid, "runnable": runnable, "run_as": target.run_as, "env_file": bool(env_file)})


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
    for entry in load_schedules(path):
        record = entry.to_dict()
        nxt = next_fire_time(entry.when) if entry.enabled else None
        record["next_run"] = nxt.isoformat() if nxt else None
        out.append(record)
    _emit({"schedules": out, "venv": venv_name(), "config": str(path)})


def _cmd_run_now(spec: rs.RunSpec) -> None:
    path = _cfg_path(spec)
    sid = str(spec.id.value)
    entry = next((e for e in load_schedules(path) if e.id == sid), None)
    if entry is None:
        _emit({"error": f"no schedule with id '{sid}'"})
        sys.exit(2)
    target = resolve_target(entry.runnable)
    if target is None:
        _emit({"error": f"runnable '{entry.runnable}' not found in this venv"})
        sys.exit(2)
    result = run_runnable(
        target.binary,
        [],
        dict(entry.args),
        source_toml=target.source,
        run_as=entry.run_as or target.run_as,
        become_method=entry.become_method or target.become_method,
        become_flags=target.become_flags,
        env_file=entry.env_file,
    )
    _emit(
        {
            "id": sid,
            "runnable": entry.runnable,
            "exit_code": result.returncode,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
        }
    )
    sys.exit(0 if result.returncode == 0 else 1)


def _cmd_install_service(spec: rs.RunSpec) -> None:
    from .service import install_service

    result = install_service(enable=bool(spec.enable.value))
    _emit(result)
    # Non-zero exit when the enable step failed (the unit is still written; the
    # emitted `hint` explains the fix), so scripts can detect it.
    if not result.get("ok"):
        sys.exit(1)


def main() -> None:
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
    elif command == "install-service":
        _cmd_install_service(spec)
    else:  # pragma: no cover - require-command makes this unreachable
        _emit({"error": "no command — try: run, add, remove, list, run-now, install-service"})
        sys.exit(2)
