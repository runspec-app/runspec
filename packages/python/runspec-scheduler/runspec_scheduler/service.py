"""service.py — the systemd units that keep the daemon running.

Each venv runs its own daemon, so the unit name embeds the venv name:
``runspec-scheduler-<venv>.service``. The ``--user`` unit goes under the venv
owner's own ``~/.config/systemd/user`` (no root needed); the system unit is
only ever printed for root to install. :mod:`runspec_scheduler.keeper` decides
which keeper to use and applies it.

**Service accounts:** ``systemctl --user`` needs the account's user session
(``XDG_RUNTIME_DIR`` + D-Bus). Under a bare ``sudo -u <acct>`` those aren't set,
so systemctl fails with *"Failed to connect to bus: No medium found."* Run from a
**login shell** instead — ``sudo -iu <acct> runspec-scheduler install-service --write`` —
and enable lingering (``loginctl enable-linger <acct>``) so the daemon survives
logout. When lingering is on but the env just isn't exported (plain ``sudo -u``),
:func:`_systemctl_env` recovers ``XDG_RUNTIME_DIR`` from ``/run/user/<uid>`` so
the call still succeeds; otherwise the returned ``hint`` explains the fix.
"""

from __future__ import annotations

import os
import pwd
import shlex
from pathlib import Path

from .runner import scripts_dir, venv_name

_UNIT_TEMPLATE = """\
[Unit]
Description=runspec scheduler ({venv})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h
ExecStart={exec_start}
EnvironmentFile=-{env_file}
Restart=on-failure
RestartSec=5
# Restarting the daemon leaves fires that are already running to finish;
# each one is its own `runspec exec`, recorded and timed out on its own.
KillMode=process

[Install]
WantedBy=default.target
"""

#: The system unit root installs once (``--manager systemd``): the daemon still
#: runs as the venv owner, never as root.
_SYSTEM_UNIT_TEMPLATE = """\
[Unit]
Description=runspec scheduler ({venv})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
KillMode=process

[Install]
WantedBy=multi-user.target
"""


def unit_name() -> str:
    return f"runspec-scheduler-{venv_name()}.service"


def unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def system_unit_path() -> Path:
    return Path("/etc/systemd/system") / unit_name()


def _exec_start(run_argv: list[str] | None) -> str:
    return shlex.join(run_argv) if run_argv else f"{scripts_dir() / 'runspec-scheduler'} run"


def render_unit(run_argv: list[str] | None = None) -> str:
    """The ``systemctl --user`` unit for this venv."""
    env_file = Path.home() / ".config" / "runspec-scheduler" / f"{venv_name()}.env"
    return _UNIT_TEMPLATE.format(venv=venv_name(), exec_start=_exec_start(run_argv), env_file=env_file)


def render_system_unit(user: str, run_argv: list[str] | None = None) -> str:
    """The system unit (``User=<venv owner>``) for root to install."""
    return _SYSTEM_UNIT_TEMPLATE.format(venv=venv_name(), user=user, exec_start=_exec_start(run_argv))


def _current_user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):  # pragma: no cover - exotic environments
        return os.environ.get("USER", "the service account")


def _systemctl_env(run_base: Path = Path("/run/user")) -> dict[str, str]:
    """os.environ, recovering ``XDG_RUNTIME_DIR`` (+ the D-Bus address) from
    ``/run/user/<uid>`` when it isn't already exported.

    Makes ``systemctl --user`` work under a bare ``sudo -u <acct>`` **when
    lingering is enabled** (so ``/run/user/<uid>`` exists) — no login shell
    needed. When the runtime dir doesn't exist there's nothing to recover and
    the caller surfaces the login-shell hint instead.
    """
    env = dict(os.environ)
    if env.get("XDG_RUNTIME_DIR"):
        return env
    run_dir = run_base / str(os.geteuid())
    if run_dir.is_dir():
        env["XDG_RUNTIME_DIR"] = str(run_dir)
        bus = run_dir / "bus"
        if bus.exists() and not env.get("DBUS_SESSION_BUS_ADDRESS"):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _is_bus_error(text: str) -> bool:
    low = text.lower()
    return "no medium found" in low or "failed to connect to bus" in low


def _login_shell_hint() -> str:
    user = _current_user()
    return (
        f"systemctl --user could not reach {user}'s user session bus. Run this from a "
        f"login shell so the session (XDG_RUNTIME_DIR + D-Bus) is set up, and enable "
        f"lingering so the daemon survives logout:\n"
        f"  sudo loginctl enable-linger {user}\n"
        f"  sudo -iu {user} runspec-scheduler install-service --manager systemd-user --write"
    )
