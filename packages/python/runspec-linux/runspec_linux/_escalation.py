"""Shared wrapper helper for the ``--run-as`` / ``--become-method`` escalation args.

runspec-linux never escalates to root: tools that need root live in the separate
``runspec-linux-root`` package, installed into a root-owned venv. ``--run-as``
picks an unprivileged account (typically the service account that owns a shared
venv); a root target is refused before anything runs.
"""

from __future__ import annotations

import json
import sys
from typing import Any

ROOT_REFUSAL = "runspec-linux does not run anything as root — root tools live in runspec-linux-root"


def is_root(name: str) -> bool:
    """True when ``name`` names uid 0 (``root``, ``0`` or any alias)."""
    if name in ("root", "0"):
        return True
    try:
        import pwd

        return pwd.getpwnam(name).pw_uid == 0
    except (ImportError, KeyError):
        return False


def refuse_root(run_as: str | None) -> None:
    """Print a JSON error and exit 2 when ``run_as`` targets root."""
    if run_as and is_root(run_as):
        print(json.dumps({"error": f"--run-as {run_as}: {ROOT_REFUSAL}"}))
        sys.exit(2)


def resolve_escalation(spec: Any) -> tuple[str | None, str]:
    """Return ``(run_as, become_method)`` from a spec carrying the escalation args
    (``None`` = no escalation, runs as the caller). Exits on a root target."""
    run_as = spec.run_as.value or None
    refuse_root(run_as)
    return run_as, spec.become_method.value
