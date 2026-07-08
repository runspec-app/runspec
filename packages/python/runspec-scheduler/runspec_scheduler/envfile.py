"""envfile.py — the per-schedule credential env file (``RUNSPEC_ENV_FILE``).

The console isn't present when a remote schedule fires, so service-account
credentials selected in the console must be **persisted on the host**. We persist
them to a dotenv file that is:

  - owned by the runnable's ``run_as`` service account, mode ``600`` — so only that
    account (and root) can read it; and
  - located in that account's own ``$HOME`` (``~/.config/runspec-scheduler/env/``),
    so no shared, world-writable directory is needed and no ``chown`` (root) is
    required — the file is *created by* ``run_as`` and is therefore owned by it.

At fire time the daemon sets ``RUNSPEC_ENV_FILE`` to this path and escalates to
``run_as``; runspec's ``parse()`` then loads it (``env.py``). Credentials never
touch argv: at write time the secret dotenv content is piped on **stdin** to a
tiny writer run as ``run_as`` (only the non-secret schedule id is an argument).

Escalation reuses runspec's ``build_become_argv`` so sudo/su/pbrun/dzdo behave
exactly as they do for a normal ``run_as`` run.
"""

from __future__ import annotations

import contextlib
import os
import pwd
import subprocess
from pathlib import Path

from runspec.become import build_become_argv

# A POSIX-sh writer, run AS the run_as account. Resolves the account's own home
# via getent (robust even if sudo didn't reset $HOME), makes a 0700 config dir,
# writes the file with umask 077 (→ 0600) from stdin, and prints the final path.
_WRITER_SH = r"""
set -eu
home="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)"
[ -n "$home" ] || home="${HOME:-}"
[ -n "$home" ] || { echo "runspec-scheduler: no home directory for $(id -un)" >&2; exit 1; }
dir="$home/.config/runspec-scheduler/env"
mkdir -p "$dir"
chmod 700 "$dir" "$home/.config/runspec-scheduler" 2>/dev/null || true
umask 077
cat > "$dir/$1.env"
printf '%s\n' "$dir/$1.env"
"""


def _current_user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):  # pragma: no cover - exotic environments
        return os.environ.get("USER", "")


def capture_env(names: list[str]) -> dict[str, str]:
    """Read the named variables from this process's environment.

    The console injects credential **values** into the ``runspec-scheduler add``
    process's environment (via its existing ``env(1)``-through-sudo channel) and
    passes only the variable **names** as ``--cred-vars``. This reads them back.
    A name that isn't set is skipped.
    """
    out: dict[str, str] = {}
    for name in names:
        val = os.environ.get(name)
        if val is not None:
            out[name] = val
    return out


def serialize_dotenv(env: dict[str, str]) -> str:
    """Render ``env`` as dotenv. Values are double-quoted so spaces / ``#`` are
    safe; the runspec reader strips one matched quote layer and does no
    interpolation, so we escape backslash and double-quote."""
    lines = []
    for key in sorted(env):
        value = str(env[key]).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{value}"')
    return "\n".join(lines) + ("\n" if lines else "")


def write_env_file(
    schedule_id: str,
    env: dict[str, str],
    run_as: str,
    become_method: str = "sudo",
    become_flags: str | None = None,
) -> str:
    """Write ``env`` to a 600 dotenv file owned by ``run_as``; return its path.

    ``run_as`` must be non-empty (credential injection requires it — the file is
    access-protected by that identity). When ``run_as`` is the current user the
    write is direct; otherwise it escalates via ``build_become_argv`` and pipes the
    content on stdin. Raises ``RuntimeError`` on failure.
    """
    if not run_as:
        raise ValueError("write_env_file requires a run_as account")
    content = serialize_dotenv(env)

    if run_as == _current_user():
        home = Path(pwd.getpwnam(run_as).pw_dir)
        dir_ = home / ".config" / "runspec-scheduler" / "env"
        dir_.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(dir_, 0o700)
            os.chmod(dir_.parent, 0o700)
        path = dir_ / f"{schedule_id}.env"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return str(path)

    argv = build_become_argv(
        ["/bin/sh", "-c", _WRITER_SH, "runspec-scheduler-envwriter", schedule_id],
        run_as=run_as,
        become_method=become_method,
        become_flags=become_flags,
    )
    proc = subprocess.run(argv, input=content, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to write env file as {run_as} (exit {proc.returncode}): {(proc.stderr or '').strip()}")
    return proc.stdout.strip()


def remove_env_file(
    path: str,
    run_as: str,
    become_method: str = "sudo",
    become_flags: str | None = None,
) -> None:
    """Best-effort delete of a per-schedule env file (as ``run_as`` if needed)."""
    if not path:
        return
    if not run_as or run_as == _current_user():
        with contextlib.suppress(OSError):
            os.unlink(path)
        return
    argv = build_become_argv(
        ["/bin/rm", "-f", path],
        run_as=run_as,
        become_method=become_method,
        become_flags=become_flags,
    )
    subprocess.run(argv, capture_output=True, text=True)
