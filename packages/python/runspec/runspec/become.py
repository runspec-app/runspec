"""
become.py — privilege escalation (run_as / sudo) command construction.

Single source of truth for the SPEC "Remote Execution" command table. Both
``runspec serve`` (local subprocess execution) and external executing clients
(e.g. runspec-console's SSH executor) build the final command through
``build_become_argv`` so escalation behaves identically everywhere.

See spec/SPEC.md → "Remote Execution" for the canonical definitions of
``run_as``, ``become_method`` and ``become_flags``.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

# Methods that share the ``method {flags} -u {user} {command}`` syntax. ``su``
# is handled separately because it wraps the command in a ``-c`` string.
_DASH_U_METHODS = frozenset({"sudo", "pbrun", "dzdo"})


def resolve_run_as(run_as_spec: Any, hostname: str) -> str:
    """Resolve a ``run_as`` spec to a concrete username for ``hostname``.

    Forms (see SPEC): simple string, ``$ENV_VAR``, or a table with
    ``hosts`` (exact match) / ``patterns`` (``re.fullmatch``, first wins) /
    ``default``. Returns ``""`` for "no privilege escalation".
    """
    if run_as_spec is None:
        return ""

    # Simple string or $ENV_VAR reference
    if isinstance(run_as_spec, str):
        if run_as_spec.startswith("$"):
            return os.environ.get(run_as_spec[1:], "")
        return run_as_spec

    # Table form: hosts / patterns / default
    if isinstance(run_as_spec, dict):
        hosts = run_as_spec.get("hosts", {})
        if hostname in hosts:
            return str(hosts[hostname])

        for pattern, user in run_as_spec.get("patterns", {}).items():
            if re.fullmatch(pattern, hostname):
                return str(user)

        return str(run_as_spec.get("default", ""))

    return ""


def validate_run_as_patterns(run_as_spec: Any) -> list[str]:
    """Return error messages for any invalid regex patterns in a run_as table."""
    if not isinstance(run_as_spec, dict):
        return []

    errors: list[str] = []
    for pattern in run_as_spec.get("patterns", {}):
        try:
            re.compile(pattern)
        except re.error as e:
            errors.append(f"invalid pattern '{pattern}': {e}")
    return errors


def _uid_username(uid: int) -> str:
    """Best-effort username for a uid; falls back to the numeric uid as a string."""
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return str(uid)


def verify_run_as_identity(run_as_user: str) -> str | None:
    """Check that the current process is running as ``run_as_user``.

    Returns ``None`` when the identity matches *or* the check cannot be
    performed — an empty target (no escalation intended, so no identity to
    assert) or a platform with no POSIX uid model (e.g. Windows). Returns a
    human-readable detail string describing the mismatch otherwise.

    The target is resolved to a uid (a numeric string is taken as a uid
    directly, otherwise it is looked up by name) and compared to the process's
    effective uid, so account aliases that share a uid are treated as equal.
    This complements ``run_as``: an executor (``runspec serve`` / SSH ``sudo
    -u``) escalates *before* the process starts, so under it the effective user
    already matches and this passes silently; it only fires on the
    direct/import path (or if escalation silently failed).
    """
    if not run_as_user:
        return None

    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return None  # no POSIX uid model (Windows) — nothing to compare

    try:
        import pwd
    except ImportError:  # pragma: no cover — POSIX ships pwd wherever geteuid exists
        return None

    try:
        if run_as_user.isdigit():
            target_uid = int(run_as_user)
            pwd.getpwuid(target_uid)  # confirm it exists; raises KeyError otherwise
        else:
            target_uid = pwd.getpwnam(run_as_user).pw_uid
    except KeyError:
        return f"run_as user {run_as_user!r} does not exist on this host"

    current_uid = geteuid()
    if current_uid == target_uid:
        return None

    current_name = _uid_username(current_uid)
    return f"the current process runs as {current_name!r} (uid {current_uid}), not {run_as_user!r} (uid {target_uid})"


def build_become_argv(
    cmd_argv: list[str],
    run_as: str = "",
    become_method: str = "sudo",
    become_flags: str | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Wrap ``cmd_argv`` with privilege escalation per the SPEC command table.

    ``cmd_argv`` is the bare command (``[binary, *args]``) with no env prefix.
    ``env`` are variables to set in the *target* user's context — placed via
    shell-assignment when there is no escalation, or via ``env(1)`` after the
    become so they survive ``sudo`` (which otherwise strips the environment).

    The result is a token list. It is valid both for non-shell execution
    (``subprocess.run(list)``) and, when space-joined, for a remote shell.

    | run_as | env | result |
    |--------|-----|--------|
    | absent | absent | ``cmd args`` |
    | absent | present | ``KEY=val … cmd args`` |
    | present | absent | ``method flags -u user cmd args`` |
    | present | present | ``method flags -u user env KEY=val … cmd args`` |

    ``su`` uses ``su flags -c "env KEY=val … cmd args" user`` instead.
    """
    env = env or {}
    assigns = [f"{k}={v}" for k, v in env.items()]
    flags = shlex.split(become_flags) if become_flags else []

    # No escalation: env vars become shell-assignments (caller runs via a shell)
    # or are supplied out of band via the process environment.
    if not run_as:
        return [*assigns, *cmd_argv]

    if become_method == "su":
        # su wraps the whole command (and its env) in a single -c string.
        inner = [*(["env", *assigns] if assigns else []), *cmd_argv]
        inner_str = " ".join(shlex.quote(part) for part in inner)
        return ["su", *flags, "-c", inner_str, run_as]

    # sudo / pbrun / dzdo share -u syntax; default to sudo for unknown methods.
    method = become_method if become_method in _DASH_U_METHODS else "sudo"
    env_part = ["env", *assigns] if assigns else []
    return [method, *flags, "-u", run_as, *env_part, *cmd_argv]
