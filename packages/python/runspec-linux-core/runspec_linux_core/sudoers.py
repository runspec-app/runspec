"""Bootstrap passwordless sudo so the console's ``run_as = "root"`` path works.

This is the one-time, interactive complement to ``run_as``: the operator supplies
their sudo password (delivered out of band by the caller — never on argv) and we
install a **validated** drop-in under ``/etc/sudoers.d`` so subsequent
``run_as = "root"`` invocations escalate without a password.

The password is read from stdin via ``sudo -S`` — it never appears in argv, the
process table, shell history, or the audit log. The drop-in is validated with
``visudo`` before *and* after install, and rolled back if the post-install check
fails, so a typo can't lock the operator out of sudo.

Two grant scopes:

- ``"scoped"`` — ``NOPASSWD`` only for the runspec venv's runnable scripts
  (``{venv_bin}/``). Least privilege, and enough for **manual** console runs and
  direct CLI/cron. Note: the console's *agent* path wraps the command in
  ``env(1)`` to carry ``RUNSPEC_AGENT`` through sudo, so under the agent the
  command sudo sees is ``/usr/bin/env …`` — which this scope does **not** match.
  Agent-driven fleet management therefore needs ``"all"``.
- ``"all"`` — ``NOPASSWD: ALL``. Works for every path (manual *and* agent).
"""

import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile

from runspec_linux_core.errors import CommandError, ToolNotFoundError

_DROP_IN = "/etc/sudoers.d/runspec"
_MANAGED_HEADER = "# Managed by runspec-linux enable-passwordless-sudo\n"

# POSIX-ish user/group name: starts with a letter or underscore, then letters,
# digits, _, -, . and an optional trailing $ (machine accounts). Validated before
# interpolation into the sudoers file so a crafted name can't inject extra rules.
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]*\$?$")


def _runspec_bin_dir() -> str:
    """The venv bin directory holding the runspec runnable scripts.

    The console invokes a runnable as ``{bin_dir}/{runnable}``; on a standard
    venv install that is the directory of the running interpreter (kept as the
    venv path, not resolved through symlinks).
    """
    return os.path.dirname(sys.executable)


def _sudoers_content(user: str, scope: str, bin_dir: str) -> str:
    # scoped: a trailing slash matches any command in the runspec venv bin dir.
    rule = f"{user} ALL=(ALL) NOPASSWD: ALL" if scope == "all" else f"{user} ALL=(root) NOPASSWD: {bin_dir}/"
    return f"{_MANAGED_HEADER}{rule}\n"


def _sudo(password: str, argv: list[str]) -> subprocess.CompletedProcess:
    """Run ``sudo -S -p '' argv``, feeding ``password`` on stdin (never on argv)."""
    return subprocess.run(
        ["sudo", "-S", "-p", "", *argv],
        input=password + "\n",
        capture_output=True,
        text=True,
    )


def _auth_hint(result: subprocess.CompletedProcess) -> str:
    err = result.stderr.strip()
    low = err.lower()
    if "incorrect password" in low or "try again" in low or "authentication failure" in low:
        return "sudo authentication failed — check the password"
    return err or "sudo command failed"


def enable_passwordless_sudo(password: str, user: str | None = None, scope: str = "scoped") -> dict:
    """Install a validated ``/etc/sudoers.d/runspec`` granting passwordless sudo.

    ``user`` defaults to the current login user. ``scope`` is ``"scoped"`` (the
    runspec venv runnables only) or ``"all"`` (``NOPASSWD: ALL``). Returns
    ``{user, scope, path, installed: True, already_configured}``.

    Raises ValueError on a bad scope/user, ToolNotFoundError if sudo/visudo are
    missing, or CommandError on a sudo auth failure or a sudoers validation
    failure (in which case the drop-in is rolled back).
    """
    if scope not in ("scoped", "all"):
        raise ValueError(f"invalid scope: {scope!r} (expected 'scoped' or 'all')")
    user = user or getpass.getuser()
    if not _USER_RE.match(user):
        raise ValueError(f"invalid user name: {user!r}")
    if not shutil.which("sudo"):
        raise ToolNotFoundError("sudo is not installed")
    if not shutil.which("visudo"):
        raise ToolNotFoundError("visudo is not installed (needed to validate the sudoers file)")

    content = _sudoers_content(user, scope, _runspec_bin_dir())

    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as tf:
        tf.write(content)
        tmp_path = tf.name
    try:
        # 1) Validate the generated syntax locally before touching the system.
        check = subprocess.run(["visudo", "-cf", tmp_path], capture_output=True, text=True)
        if check.returncode != 0:
            detail = check.stdout.strip() or check.stderr.strip()
            raise CommandError(f"generated sudoers failed validation: {detail}")

        # 2) Idempotent: identical drop-in already present? (reading it needs root)
        existing = _sudo(password, ["cat", _DROP_IN])
        if existing.returncode == 0 and existing.stdout == content:
            return {"user": user, "scope": scope, "path": _DROP_IN, "installed": True, "already_configured": True}

        # 3) Install atomically as root, 0440 root:root.
        install = _sudo(password, ["install", "-m", "0440", "-o", "root", "-g", "root", tmp_path, _DROP_IN])
        if install.returncode != 0:
            raise CommandError(_auth_hint(install))

        # 4) Re-validate the whole sudoers tree; roll our drop-in back if broken.
        verify = _sudo(password, ["visudo", "-c"])
        if verify.returncode != 0:
            _sudo(password, ["rm", "-f", _DROP_IN])
            detail = verify.stdout.strip() or verify.stderr.strip()
            raise CommandError(f"sudoers validation failed after install — rolled back: {detail}")

        return {"user": user, "scope": scope, "path": _DROP_IN, "installed": True, "already_configured": False}
    finally:
        os.unlink(tmp_path)
