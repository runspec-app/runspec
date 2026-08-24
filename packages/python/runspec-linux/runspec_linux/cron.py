"""cron.py — the ``cron`` runnable (crontab management, folded into runspec-linux).

One binary, a verb per crontab operation: ``list`` / ``add`` / ``remove`` /
``clear``. ``rs.parse("cron")`` resolves the subcommand and its args (from the
merged ``[cron]`` tables in the pack's ``runspec.toml``); we unpack each
``.value`` at the boundary and call the matching :mod:`runspec_linux_core.cron`
helper, which does the crontab read-modify-write.

**Per-invocation ``--run-as``** (a global arg inherited by every verb, like the
venv runnables): the operator/agent picks whose crontab to manage per call. The
``crontab`` command is escalated via ``sudo -n -u <user>`` / ``su`` inside the
core helper, so "run as user *bob*" means "manage *bob*'s crontab". With no
``--run-as`` the caller's own crontab is used.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs
from runspec_linux_core.cron import add_cron, clear_cron, list_cron, remove_cron
from runspec_linux_core.errors import CommandError, ToolNotFoundError


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str))


def _run(spec: rs.RunSpec) -> dict[str, Any]:
    command = spec.runspec_command
    run_as = spec.run_as.value or None
    become_method = spec.become_method.value

    if command == "list":
        return list_cron(run_as=run_as, become_method=become_method)
    if command == "add":
        return add_cron(
            spec.schedule.value,
            spec.command.value,
            comment=spec.comment.value or None,
            run_as=run_as,
            become_method=become_method,
            allow_duplicate=spec.allow_duplicate.value,
        )
    if command == "remove":
        return remove_cron(
            match=spec.match.value or None,
            index=spec.index.value,
            run_as=run_as,
            become_method=become_method,
        )
    if command == "clear":
        return clear_cron(run_as=run_as, become_method=become_method)

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `cron --help`")  # pragma: no cover


def main() -> None:
    spec = rs.parse("cron")
    try:
        result = _run(spec)
    except ToolNotFoundError as exc:
        _emit({"error": str(exc), "kind": "ToolNotFoundError"})
        sys.exit(1)
    except (CommandError, ValueError) as exc:
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    _emit(result)


if __name__ == "__main__":
    main()
