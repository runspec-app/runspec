"""Shared wrapper helper for the ``--run-as`` / ``--sudo`` / ``--become-method``
escalation args.

The privileged runnables declare a mutually-exclusive ``escalation`` group with
``--run-as <user>`` and ``--sudo`` (a flag = "run as root"), plus
``--become-method sudo|su``. The group guarantees at most one of run-as/sudo is
set, so this resolves them to the ``(run_as, become_method)`` the core helpers
take: ``--sudo`` → ``run_as="root"``, ``--run-as x`` → ``run_as="x"``, neither →
``run_as=None`` (no escalation, runs as the login user).
"""

from __future__ import annotations

import runspec as rs


def resolve_escalation(spec: rs.RunSpec) -> tuple[str | None, str]:
    """Return ``(run_as, become_method)`` from a spec carrying the escalation args."""
    run_as = "root" if spec.sudo.value else (spec.run_as.value or None)
    return run_as, spec.become_method.value
