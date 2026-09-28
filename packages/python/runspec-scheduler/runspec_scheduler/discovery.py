"""discovery.py — find the runnables installed in this venv + resolve run_as.

Reuses core runspec's importlib.metadata discovery so the daemon sees exactly the
runnables ``runspec local`` would. The scheduler and the reserved ``runspec`` name
are excluded — a scheduler must never schedule itself or the control binary.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runspec.become import resolve_run_as
from runspec.cli import _deduplicate, _discover_installed

from .invocation import InvocationError, plan_invocation
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
    # The runnable's normalised spec (args + nested commands) — used to validate a
    # schedule's command path/args and to order its argv. Not part of identity.
    spec: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


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
        spec=spec,
    )


# ── subcommands: validate a schedule's command path + args against the spec ─────


def _norm(name: str) -> str:
    return str(name).replace("_", "-")


def _commands_of(node: dict[str, Any]) -> dict[str, Any]:
    cmds = node.get("commands")
    return cmds if isinstance(cmds, dict) else {}


def check_invocation(spec: dict[str, Any], command: tuple[str, ...], args: dict[str, Any]) -> str | None:
    """Why ``command`` + ``args`` isn't a valid invocation of this runnable, or
    ``None`` when it is.

    Walks the command path through the spec's nested ``commands``: an unknown
    verb, a runnable that has no verbs, or stopping at a node that requires a
    command (``require-command``) are all errors naming the valid choices. Arg
    names must be declared on the runnable or on a command along the path
    (``_``/``-`` are interchangeable, as on the command line). Caught at
    ``add`` time so a bad schedule never lands in a crontab to fail every fire."""
    node = spec
    walked: list[str] = []
    allowed = {_norm(n) for n in (spec.get("args") or {})}
    for verb in command:
        cmds = _commands_of(node)
        where = " ".join(walked) or "the runnable"
        if not cmds:
            return f"'{verb}' isn't a command — {where} has no subcommands"
        if verb not in cmds:
            return f"unknown command '{verb}' for {where}; choose one of: {', '.join(sorted(cmds))}"
        node = cmds[verb]
        walked.append(verb)
        allowed |= {_norm(n) for n in (node.get("args") or {})}
    cmds = _commands_of(node)
    if cmds and node.get("require_command", False):
        where = f"'{' '.join(walked)}'" if walked else "this runnable"
        return f"{where} needs a command (--command); choose one of: {', '.join(sorted(cmds))}"
    unknown = sorted(k for k in args if _norm(k) not in allowed)
    if unknown:
        return f"unknown argument(s) {', '.join(unknown)}; valid: {', '.join(sorted(allowed)) or '(none)'}"
    try:  # every value must be expressible on the command line (flags, lists, secrets…)
        plan_invocation("runnable", spec, command, args)
    except InvocationError as exc:
        return str(exc)
    return None
