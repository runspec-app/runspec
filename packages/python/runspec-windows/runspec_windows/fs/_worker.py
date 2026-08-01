"""Run-as worker: read ``{"verb": ..., "kwargs": ...}`` JSON from stdin, run the
verb via the pure core, print the JSON result. ``cli.py`` invokes this as the
target OS user under ``sudo``/``su`` when ``--run-as`` is set, so an in-process
filesystem verb runs with that user's permissions.

Imports only ``runspec_windows.fs._dispatch`` (→ ``runspec_fs_core``), never
``runspec`` — the escalated user only needs the pure core importable, and the
payload is already fully resolved, so no ``runspec.toml`` lookup happens here.
"""

from __future__ import annotations

import json
import sys

from runspec_fs_core import FsCoreError

from runspec_windows.fs._dispatch import run_verb


def main() -> None:
    payload = json.loads(sys.stdin.read())
    try:
        result = run_verb(payload["verb"], payload["kwargs"])
    except (FsCoreError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "kind": type(exc).__name__}, default=str))
        sys.exit(1)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
