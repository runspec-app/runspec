"""Privilege-escalation command builder — run a filesystem operation as another
OS user, via the same ``sudo -n -u <user>`` / ``su <user> -c`` mechanism
``runspec-linux`` uses for its per-invocation ``run_as`` venv runnables.

Pure and stdlib-only: this *builds and validates* the become command; executing
it stays with the caller. The ``fs`` runnable re-invokes a small worker under
this wrapper so an in-process filesystem verb can run as the chosen user. There
is no jail — the selected user's own OS permissions bound what the operation can
reach, which is exactly the point of choosing the user per invocation.
"""

from __future__ import annotations

import os
import re
import shlex

from runspec_fs_core.errors import FsCoreError

# A POSIX-ish user name, validated before it is interpolated into a become
# command so a crafted value can't inject extra arguments. Mirrors
# runspec-linux-core's _USER_RE.
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*\$?$")

BECOME_METHODS = ("sudo", "su")


class BecomeError(FsCoreError):
    """An invalid run-as user, or an unsupported / unavailable become method."""


def validate_user(name: str) -> None:
    """Raise :class:`BecomeError` unless ``name`` is a well-formed OS user name."""
    if not name or not _USER_RE.fullmatch(name):
        raise BecomeError(f"invalid run-as user: {name!r}")


def build_become(argv: list[str], run_as: str | None, method: str = "sudo") -> list[str]:
    """Wrap ``argv`` so it runs as ``run_as``. Returns ``argv`` unchanged when
    ``run_as`` is falsy (no escalation requested).

    - ``sudo`` → ``sudo -n -u <user> <argv...>`` (``-n`` never prompts, so a
      missing passwordless-sudo rule fails fast instead of hanging on a
      TTY-less SSH/agent channel).
    - ``su``   → ``su <user> -c '<argv...>'``.
    """
    if not run_as:
        return argv
    validate_user(run_as)
    if method not in BECOME_METHODS:
        raise BecomeError(f"invalid become-method: {method!r} (expected sudo or su)")
    if method == "su":
        return ["su", run_as, "-c", shlex.join(argv)]
    return ["sudo", "-n", "-u", run_as, *argv]


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
