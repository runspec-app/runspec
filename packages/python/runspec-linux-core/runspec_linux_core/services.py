"""systemd service helpers: check status, list units, restart, and root service control."""

import re
import shutil
import subprocess

from runspec_linux_core.become import build_become, check_become_error
from runspec_linux_core.errors import CommandError, ToolNotFoundError


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def check_service(service: str) -> dict:
    """Return the status of a systemd service.

    Raises ToolNotFoundError if systemctl is not present.
    """
    if not _systemctl_available():
        raise ToolNotFoundError("systemctl not available — not a systemd system")

    result = subprocess.run(
        [
            "systemctl",
            "show",
            service,
            "--property=ActiveState,SubState,LoadState,Description,ActiveEnterTimestamp",
        ],
        capture_output=True,
        text=True,
    )
    props: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v

    active = props.get("ActiveState", "unknown")
    return {
        "service": service,
        "active": active == "active",
        "active_state": active,
        "sub_state": props.get("SubState", "unknown"),
        "load_state": props.get("LoadState", "unknown"),
        "description": props.get("Description", ""),
        "since": props.get("ActiveEnterTimestamp", ""),
    }


def list_services(state_filter: str = "all") -> list[dict]:
    """Return systemd service units, optionally filtered by state.

    Raises ToolNotFoundError if systemctl is not present.
    """
    if not _systemctl_available():
        raise ToolNotFoundError("systemctl not available — not a systemd system")

    cmd = ["systemctl", "list-units", "--type=service", "--no-pager", "--no-legend"]
    if state_filter != "all":
        cmd += [f"--state={state_filter}"]

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    rows = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        rows.append(
            {
                "unit": parts[0],
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            }
        )
    return rows


def restart_service(service: str, *, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """Restart a systemd service. Returns ``{service, restarted: True}`` on success.

    Restarting a system service needs privilege, so ``run_as`` (with a
    ``become_method``) escalates the ``systemctl restart`` via sudo/su — a
    ``--sudo`` flag on the runnable maps to ``run_as="root"``. Raises
    ToolNotFoundError if systemctl is not present, or CommandError if the restart
    returns a non-zero exit (with a passwordless-sudo hint when relevant).
    """
    if not _systemctl_available():
        raise ToolNotFoundError("systemctl not available — not a systemd system")

    argv = build_become(["systemctl", "restart", service], run_as, become_method)
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        check_become_error("restart service", result.stderr, run_as)
    out = {"service": service, "restarted": True}
    if run_as:
        out["run_as"] = run_as
    return out


# Actions control_service accepts. ``enable``/``disable`` take ``now`` to also
# start/stop the unit (``systemctl enable --now``).
SERVICE_ACTIONS = ("start", "stop", "restart", "reload", "enable", "disable")

# A unit name: what systemd allows, and never a leading '-' (an option).
_UNIT = re.compile(r"^[A-Za-z0-9:_.\\@-]+$")


def _validate_unit(service: str) -> str:
    if not service or service.startswith("-") or not _UNIT.match(service):
        raise ValueError(f"invalid service name: {service!r}")
    return service


def control_service(service: str, action: str, *, now: bool = False) -> dict:
    """Start, stop, restart, reload, enable or disable a systemd service. Needs root.

    Runs ``systemctl <action> [--now] <service>`` as the current process (the
    caller is already root — no escalation here, unlike ``restart_service``),
    then reports the unit's state. ``now`` only applies to enable/disable.

    Returns ``{service, action, now, active_state, sub_state, enabled}`` where
    ``enabled`` is ``systemctl is-enabled`` (``enabled``, ``disabled``,
    ``static``, ...). Raises ValueError on a bad name or action,
    ToolNotFoundError without systemctl, and CommandError when systemctl fails
    (with the last journal lines for the unit when there are any).
    """
    _validate_unit(service)
    if action not in SERVICE_ACTIONS:
        raise ValueError(f"unknown action {action!r}; use one of {', '.join(SERVICE_ACTIONS)}")
    if not _systemctl_available():
        raise ToolNotFoundError("systemctl not available — not a systemd system")

    cmd = ["systemctl", action]
    if now and action in ("enable", "disable"):
        cmd.append("--now")
    cmd += ["--", service]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or f"systemctl {action} {service} failed"
        journal = subprocess.run(
            ["journalctl", "-u", service, "-n", "15", "--no-pager", "-q"],
            capture_output=True,
            text=True,
        )
        if journal.returncode == 0 and journal.stdout.strip():
            message += "\nLast log lines:\n" + journal.stdout.strip()
        raise CommandError(message)

    status = check_service(service)
    enabled = subprocess.run(["systemctl", "is-enabled", "--", service], capture_output=True, text=True)
    return {
        "service": service,
        "action": action,
        "now": bool(now and action in ("enable", "disable")),
        "active_state": status["active_state"],
        "sub_state": status["sub_state"],
        "enabled": enabled.stdout.strip() or "unknown",
    }
