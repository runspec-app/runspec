"""hookexec.py — the process one webhook run is, started by the daemon.

``python -m runspec_scheduler.hookexec`` reads one JSON request on stdin and
runs ``runspec.runexec.exec_runnable`` with it, so a hook run is recorded like
any other (runs index, captured output, label ``hook:<name>``, trigger
``webhook``) and its argument values travel as ``RUNSPEC_<R>_ARG_<NAME>``
environment variables — never on the command line, where ``ps`` shows them —
including through ``sudo`` when the runnable has its own ``run_as`` (exec
carries ``env`` in the env frame).

Request: ``{"runnable", "argv", "env", "label", "trigger", "env_file",
"timeout"}``. Exits with the run's exit code.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    # The request came on stdin; the runnable gets an empty one.
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    sys.stdin = open(0, closefd=False)  # noqa: SIM115 — fd 0 stays open for the process

    from runspec.runexec import ExecError, exec_runnable

    try:
        return exec_runnable(
            str(request["runnable"]),
            [str(a) for a in request.get("argv") or []],
            label=str(request.get("label") or "") or None,
            trigger=str(request.get("trigger") or "") or None,
            timeout=float(request["timeout"]) if request.get("timeout") else None,
            env={str(k): str(v) for k, v in (request.get("env") or {}).items()},
            env_file=str(request.get("env_file") or "") or None,
        )
    except ExecError as exc:
        print(f"runspec-scheduler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
