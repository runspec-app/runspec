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
import warnings
from collections.abc import Mapping
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


# The SPEC "env prelude": reads the env frame from stdin with shell builtins,
# exports each variable and execs the command. It holds no values, so it is safe
# on a command line and in a sudo log. No single quotes, so it embeds verbatim in
# ``sh -c '…'``. Verified in dash, bash, BusyBox sh, ksh and mksh.
ENV_PRELUDE = 'while IFS= read -r l; do [ "$l" = . ] && break; n=${l%%=*}; v=$(printf "%b" "${l#*=}X"); export "$n=${v%X}"; done; exec "$@"'

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def env_frame(env: Mapping[str, str]) -> bytes:
    """The SPEC "env frame" for ``env``: one ``NAME=<escaped>`` line per variable
    (every UTF-8 byte of the value as ``\\0ooo``), then a ``.`` line.

    Raises ``ValueError`` for a name that isn't ``[A-Za-z_][A-Za-z0-9_]*`` or a
    value containing NUL."""
    lines = []
    for name, value in env.items():
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        raw = str(value).encode("utf-8")
        if b"\0" in raw:
            raise ValueError(f"environment variable {name} contains a NUL byte")
        lines.append(name + "=" + "".join(f"\\0{b:03o}" for b in raw) + "\n")
    lines.append(".\n")
    return "".join(lines).encode("ascii")


def parse_env_frame(frame: bytes) -> dict[str, str]:
    """Decode an env frame back to ``{name: value}`` — the inverse of
    :func:`env_frame` (what the prelude does in ``sh``). Stops at the ``.`` line.
    Raises ``ValueError`` on a malformed frame."""
    out: dict[str, str] = {}
    for line in frame.decode("ascii").split("\n"):
        if line == ".":
            return out
        name, sep, esc = line.partition("=")
        if not sep or not _ENV_NAME.fullmatch(name) or len(esc) % 5:
            raise ValueError(f"malformed env frame line: {line!r}")
        chunks = [esc[i : i + 5] for i in range(0, len(esc), 5)]
        if any(c[:2] != "\\0" or not c[2:].isdigit() for c in chunks):
            raise ValueError(f"malformed env frame value for {name}")
        out[name] = bytes(int(c[2:], 8) for c in chunks).decode("utf-8")
    raise ValueError("env frame has no terminating '.' line")


def build_exec_argv(
    cmd_argv: list[str],
    run_as: str = "",
    become_method: str = "sudo",
    become_flags: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[list[str], bytes | None]:
    """The SPEC "Command building" table: ``(argv, stdin)``.

    With ``env``, the command is wrapped in the env prelude (``sh -c ENV_PRELUDE
    sh cmd…``) — inside the escalation when ``run_as`` is set — and ``stdin`` is
    the env frame the caller must write to the process's standard input before
    anything else. Variable values never appear in ``argv`` (a command line is
    readable by every user and ``sudo`` logs it). Without ``env``, ``stdin`` is
    ``None`` and ``argv`` is the plain (possibly escalated) command.

    A caller that runs the command locally with no ``run_as`` may skip this and
    put the variables in the child's environment instead."""
    if not env:
        return build_become_argv(cmd_argv, run_as, become_method, become_flags), None
    frame = env_frame(env)
    wrapped = ["sh", "-c", ENV_PRELUDE, "sh", *cmd_argv]
    return build_become_argv(wrapped, run_as, become_method, become_flags), frame


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

    .. deprecated:: 0.67.0
       ``env`` puts variable values on the command line, where every user on
       the host can read them (``ps``) and ``sudo`` logs them. Use
       :func:`build_exec_argv`, which passes them on standard input instead.
    """
    if env:
        warnings.warn(
            "build_become_argv(env=...) puts variable values on the command line (visible in ps, logged by sudo); use build_exec_argv(), which passes them on stdin",
            DeprecationWarning,
            stacklevel=2,
        )
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
