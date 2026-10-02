"""Privilege-escalation command builder — run a command as another OS user via
``sudo -n -u <user>`` / ``su <user> -c``.

This is the single source of truth for runspec-linux's per-invocation ``run_as``
escalation (mechanism B): the ``--run-as`` args on the logs, services,
containers, investigate and cron runnables all build their become command here. It is a
sibling of ``runspec_fs_core.become`` (kept a separate copy because the ``*-core``
packages stay dependency-free and must not cross-import); the canonical user
regex here allows a dot (``[a-z0-9_.-]``), a superset of the fs-core form, so it
matches ``sudoers._USER_RE`` and every name runspec-linux already accepts.

Pure and stdlib-only: this *builds and validates* the become command; executing
it (and interpreting a passwordless-sudo failure) stays with the caller — see
``check_become_error`` below.
"""

from __future__ import annotations

import os
import re
import shlex

from runspec_linux_core.errors import CommandError

# A POSIX-ish user/group name, validated before it is interpolated into a become
# command (or a chgrp) so a crafted value can't inject extra tokens — everything
# still goes to argv, never a shell, but this keeps the error early and clear.
# Allows a dot, matching sudoers._USER_RE.
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]*\$?$")

BECOME_METHODS = ("sudo", "su")


class BecomeError(CommandError):
    """An invalid run-as user, or an unsupported / unavailable become method."""


def validate_user(name: str) -> str:
    """Raise :class:`BecomeError` unless ``name`` is a well-formed OS user name;
    return it unchanged on success (so it can be used inline)."""
    if not name or not _USER_RE.match(name):
        raise BecomeError(f"invalid run-as user: {name!r}")
    return name


def build_become(argv: list[str], run_as: str | None, method: str = "sudo") -> list[str]:
    """Wrap ``argv`` so it runs as ``run_as``. Returns ``argv`` unchanged when
    ``run_as`` is falsy (no escalation requested).

    - ``sudo`` → ``sudo -n -u <user> <argv...>`` (``-n`` never prompts, so a
      missing passwordless-sudo rule fails fast instead of hanging on a
      TTY-less SSH/agent channel).
    - ``su``   → ``su <user> -c '<argv...>'`` (su has no ``-n``; it only works
      without a password when the caller is already root).
    """
    if not run_as:
        return argv
    validate_user(run_as)
    if is_current_user(run_as):
        # Already that account: nothing to escalate. `sudo -u self` would still
        # need a sudoers rule nobody has (a service account running its own
        # venv's tools with `--run-as <itself>`).
        return argv
    if method not in BECOME_METHODS:
        raise CommandError(f"invalid become-method: {method!r} (expected sudo or su)")
    if method == "su":
        return ["su", run_as, "-c", shlex.join(argv)]
    return ["sudo", "-n", "-u", run_as, *argv]


def check_become_error(what: str, stderr: str, run_as: str | None) -> None:
    """Raise :class:`CommandError` for a failed escalated subprocess, adding a
    passwordless-sudo hint on the classic "password/terminal required" cases.

    ``what`` labels the operation; ``stderr`` is the subprocess's captured
    stderr; ``run_as`` is the target user (``None`` when no escalation was in
    play — then the hint is skipped). Shared by every
    ``--run-as`` helper so the message is identical everywhere.
    """
    err = stderr.strip()
    low = err.lower()
    if run_as and ("password is required" in low or "a terminal is required" in low):
        raise CommandError(f"{what}: passwordless sudo is not configured for run-as {run_as!r} ({err})")
    raise CommandError(err or f"{what} failed")


def is_current_user(name: str) -> bool:
    """True when ``name`` is the account this process already runs as
    (compared by uid, so aliases sharing a uid count). ``False`` when that
    can't be told — no POSIX uids, or no such user."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    try:
        import pwd

        return bool(pwd.getpwnam(name).pw_uid == geteuid())
    except (ImportError, KeyError):
        return False


def escalation_supported() -> bool:
    """True on a POSIX host where ``sudo``/``su`` escalation makes sense."""
    return hasattr(os, "geteuid")


def current_user() -> str:
    """The effective OS user name — used to decide whether escalation is needed
    (running as the user you already are is a no-op)."""
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        import getpass

        return getpass.getuser()
