"""systemd service helpers: check status, list units, restart."""

import shutil
import subprocess

from runspec_linux_core.become import build_become, check_become_error
from runspec_linux_core.errors import ToolNotFoundError


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
