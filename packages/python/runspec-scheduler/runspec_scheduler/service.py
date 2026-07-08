"""service.py — install the per-venv systemd ``--user`` unit.

Each venv runs its own daemon, so the unit name embeds the venv name:
``runspec-scheduler-<venv>.service``. The unit is written under the operator's
own ``~/.config/systemd/user`` (no root needed); ``--enable`` runs
``systemctl --user daemon-reload`` + ``enable --now``.
"""

from __future__ import annotations

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


def install_service(enable: bool = True) -> dict[str, object]:
    """Write the unit file and (optionally) enable+start it."""
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    name = unit_name()
    path = directory / name
    path.write_text(render_unit(), encoding="utf-8")

    result: dict[str, object] = {"ok": True, "unit": name, "path": str(path), "enabled": False}
    if not enable:
        result["hint"] = f"systemctl --user enable --now {name}"
        return result

    steps = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", name],
    ]
    for cmd in steps:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            result["ok"] = False
            result["error"] = (proc.stderr or "").strip() or f"{' '.join(cmd)} failed"
            return result
    result["enabled"] = True
    return result
