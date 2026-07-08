"""discovery.py — find the runnables installed in this venv + resolve run_as.

Reuses core runspec's importlib.metadata discovery so the daemon sees exactly the
runnables ``runspec local`` would. The scheduler and the reserved ``runspec`` name
are excluded — a scheduler must never schedule itself or the control binary.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runspec.become import resolve_run_as
from runspec.cli import _deduplicate, _discover_installed

from .runner import find_binary

#: Runnable names the scheduler will never run.
EXCLUDED = {"runspec-scheduler", "runspec"}


@dataclass(frozen=True)
class Target:
    """A resolved schedulable runnable in this venv."""

    runnable: str
    source: str  # path to its runspec.toml (→ RUNSPEC_CONFIG)
    binary: Path
    run_as: str
    become_method: str
    become_flags: str | None


def discover() -> dict[str, dict[str, Any]]:
    """Map runnable name → its discovery record ({source, runnable, spec}).

    Excludes the scheduler itself and ``runspec``. When a name appears more than
    once (unusual in one venv), the first wins.
    """
    out: dict[str, dict[str, Any]] = {}
    for item in _deduplicate(_discover_installed()):
        name = item.get("runnable", "")
        if not name or name in EXCLUDED or name in out:
            continue
        out[name] = item
    return out


def resolve_target(name: str, hostname: str | None = None) -> Target | None:
    """Resolve a runnable name to a :class:`Target`, or ``None`` if not present."""
    record = discover().get(name)
    if record is None:
        return None
    binary = find_binary(name)
    if binary is None:
        return None
    spec = record.get("spec") or {}
    host = hostname or socket.gethostname()
    run_as = resolve_run_as(spec.get("run_as"), host)
    become_method = str(spec.get("become_method") or "sudo").strip() or "sudo"
    become_flags = spec.get("become_flags")
    return Target(
        runnable=name,
        source=str(record.get("source") or ""),
        binary=binary,
        run_as=run_as,
        become_method=become_method,
        become_flags=str(become_flags) if become_flags else None,
    )
