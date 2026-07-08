"""service.py — install the per-venv systemd ``--user`` unit.

Each venv runs its own daemon, so the unit name embeds the venv name:
``runspec-scheduler-<venv>.service``. The unit is written under the operator's
own ``~/.config/systemd/user`` (no root needed); ``--enable`` runs
``systemctl --user daemon-reload`` + ``enable --now``.

**Service accounts:** ``systemctl --user`` needs the account's user session
(``XDG_RUNTIME_DIR`` + D-Bus). Under a bare ``sudo -u <acct>`` those aren't set,
so systemctl fails with *"Failed to connect to bus: No medium found."* Run from a
**login shell** instead — ``sudo -iu <acct> runspec-scheduler install-service`` —
and enable lingering (``loginctl enable-linger <acct>``) so the daemon survives
logout. When lingering is on but the env just isn't exported (plain ``sudo -u``),
:func:`_systemctl_env` recovers ``XDG_RUNTIME_DIR`` from ``/run/user/<uid>`` so
the call still succeeds; otherwise the returned ``hint`` explains the fix.
"""

from __future__ import annotations

import os
import pwd
import subprocess
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
ExecStart={exec_start} run
EnvironmentFile=-{env_file}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def unit_name() -> str:
    return f"runspec-scheduler-{venv_name()}.service"


def unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def render_unit() -> str:
    exec_start = scripts_dir() / "runspec-scheduler"
    env_file = Path.home() / ".config" / "runspec-scheduler" / f"{venv_name()}.env"
    return _UNIT_TEMPLATE.format(venv=venv_name(), exec_start=exec_start, env_file=env_file)


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
        f"  sudo -iu {user} runspec-scheduler install-service"
    )


def install_service(enable: bool = True) -> dict[str, object]:
    """Write the unit file and (optionally) enable+start it.

    The unit is always written (a plain file write, no bus needed). The enable
    step needs the account's user session; on a bus failure the result carries an
    actionable ``hint`` and the unit path, so a re-run from a login shell (or a
    manual ``systemctl --user enable --now``) finishes the job.
    """
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    name = unit_name()
    path = directory / name
    path.write_text(render_unit(), encoding="utf-8")

    result: dict[str, object] = {"ok": True, "unit": name, "path": str(path), "enabled": False}
    if not enable:
        result["hint"] = f"systemctl --user enable --now {name}"
        return result

    env = _systemctl_env()
    steps = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", name],
    ]
    for cmd in steps:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip() or f"{' '.join(cmd)} failed"
            result["ok"] = False
            result["error"] = err
            # The unit is on disk; guide the operator to finish enabling it.
            result["hint"] = _login_shell_hint() if _is_bus_error(err) else f"the unit is written at {path}; enable it with: systemctl --user enable --now {name}"
            return result
    result["enabled"] = True
    return result
