"""Application discovery & investigation helpers — answer "where does this
bespoke app live, what runs it, what's it listening on, and which package owns
this file" for corporate application support.

- ``service_detail`` — a systemd unit's ExecStart / working dir / user / env
  files / active state (from ``systemctl show``).
- ``open_ports`` — listening sockets mapped to pid → process (from ``ss -tlnp``);
  the full pid map needs privilege, so it takes ``run_as`` / ``become_method``.
- ``owning_package`` — which package owns a path (dpkg / rpm).
- ``failed_units`` — systemd units in the failed state.

Parsers are separated from the subprocess I/O so they unit-test without the
real tools.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from runspec_linux_core.become import build_become, check_become_error
from runspec_linux_core.errors import CommandError, ToolNotFoundError

# systemctl show properties worth surfacing for "where does this app live".
_SERVICE_PROPS = [
    "Id",
    "Description",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "FragmentPath",
    "ExecStart",
    "WorkingDirectory",
    "User",
    "Group",
    "EnvironmentFiles",
    "MainPID",
]


def _parse_show(text: str) -> dict[str, str]:
    """Parse ``systemctl show`` ``key=value`` output into a dict."""
    props: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return props


def _exec_start_path(exec_start: str) -> str:
    """Pull the binary path out of a systemd ExecStart property value.

    ExecStart is rendered as ``{ path=/usr/bin/foo ; argv[]=... }`` by
    ``systemctl show``; fall back to the first token otherwise.
    """
    m = re.search(r"path=(\S+)", exec_start)
    if m:
        return m.group(1)
    return exec_start.split(";", 1)[0].strip().split(" ", 1)[0]


def service_detail(unit: str, *, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """Return where a systemd unit lives: ExecStart binary, working dir, user,
    env files, unit-file path, and active state.

    Some properties on protected units need privilege, so ``run_as`` /
    ``become_method`` escalate the ``systemctl show``. Raises ToolNotFoundError if
    systemctl is absent.
    """
    if not shutil.which("systemctl"):
        raise ToolNotFoundError("systemctl not available — not a systemd system")

    cmd = ["systemctl", "show", unit, "--property=" + ",".join(_SERVICE_PROPS)]
    result = subprocess.run(build_become(cmd, run_as, become_method), capture_output=True, text=True)
    if result.returncode != 0:
        check_become_error("service detail", result.stderr, run_as)

    props = _parse_show(result.stdout)
    exec_start = props.get("ExecStart", "")
    return {
        "unit": props.get("Id", unit),
        "description": props.get("Description", ""),
        "load_state": props.get("LoadState", ""),
        "active_state": props.get("ActiveState", ""),
        "sub_state": props.get("SubState", ""),
        "unit_file_state": props.get("UnitFileState", ""),
        "fragment_path": props.get("FragmentPath", ""),
        "exec_start": exec_start,
        "exec_start_path": _exec_start_path(exec_start) if exec_start else "",
        "working_directory": props.get("WorkingDirectory", ""),
        "user": props.get("User", ""),
        "group": props.get("Group", ""),
        "environment_files": props.get("EnvironmentFiles", ""),
        "main_pid": props.get("MainPID", ""),
    }


_PEER_WILDCARD_RE = re.compile(r"^[\d.a-fA-F:\[\]*]+:\*$")


def _parse_ss_listening(text: str) -> list[dict]:
    """Parse ``ss -tlnp`` / ``-ulnp`` / ``-tulnp`` output into listening-socket
    rows with port → pid/process.

    Column layout shifts with the flags (``-tu`` prepends a Netid column that
    ``-t`` alone omits), so the local address is anchored on the wildcard peer
    field (``*:*`` / ``0.0.0.0:*`` / ``[::]:*``) rather than a fixed index.
    """
    rows: list[dict] = []
    lines = text.strip().splitlines()
    for line in lines[1:] if lines else []:  # skip header
        parts = line.split()
        if len(parts) < 4:
            continue

        peer_idx = next((i for i, p in enumerate(parts) if _PEER_WILDCARD_RE.match(p)), None)
        if peer_idx is None or peer_idx == 0:
            continue
        local = parts[peer_idx - 1]
        port = local.rsplit(":", 1)[-1]
        # A leading Netid column (tcp/udp) is present with -tu but not -t alone.
        proto = parts[0] if parts[0] in ("tcp", "udp", "tcp6", "udp6") else ""

        procs: list[dict] = []
        m = re.search(r"users:\(\((.*)\)\)", line)
        if m:
            for pm in re.finditer(r'"([^"]+)",pid=(\d+)', m.group(1)):
                procs.append({"process": pm.group(1), "pid": int(pm.group(2))})
        rows.append({"proto": proto, "local": local, "port": port, "processes": procs})
    return rows


def open_ports(proto: str = "tcp", *, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """Return listening sockets mapped to the owning process (port → pid → name).

    The full pid/process map needs privilege (``ss`` only shows other users'
    processes as root), so ``run_as`` / ``become_method`` escalate the probe.
    ``proto`` is ``tcp`` / ``udp`` / ``both``. Raises ToolNotFoundError if ``ss``
    is absent.
    """
    if not shutil.which("ss"):
        raise ToolNotFoundError("ss not available (iproute2)")

    flags = {"tcp": "-tlnp", "udp": "-ulnp", "both": "-tulnp"}.get(proto)
    if flags is None:
        raise ValueError(f"invalid proto: {proto!r} (expected tcp, udp, or both)")

    result = subprocess.run(build_become(["ss", flags], run_as, become_method), capture_output=True, text=True)
    if result.returncode != 0:
        check_become_error("open ports", result.stderr, run_as)

    rows = _parse_ss_listening(result.stdout)
    out = {"proto": proto, "count": len(rows), "listeners": rows}
    if run_as:
        out["run_as"] = run_as
    return out


def owning_package(path: str) -> dict:
    """Return which installed package owns ``path`` (dpkg on Debian/Ubuntu, rpm on
    RHEL/SUSE). Raises ToolNotFoundError if neither dpkg nor rpm is present;
    returns ``{owned: False}`` when the file belongs to no package.
    """
    if shutil.which("dpkg"):
        result = subprocess.run(["dpkg", "-S", path], capture_output=True, text=True)
        if result.returncode == 0 and ":" in result.stdout:
            pkg = result.stdout.split(":", 1)[0].strip()
            return {"path": path, "owned": True, "package": pkg, "manager": "dpkg"}
        return {"path": path, "owned": False, "manager": "dpkg"}
    if shutil.which("rpm"):
        result = subprocess.run(["rpm", "-qf", path], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return {"path": path, "owned": True, "package": result.stdout.strip(), "manager": "rpm"}
        return {"path": path, "owned": False, "manager": "rpm"}
    raise ToolNotFoundError("neither dpkg nor rpm found — cannot resolve package ownership")


def _parse_failed_units(text: str) -> list[dict]:
    """Parse ``systemctl --failed --no-legend`` output into unit rows."""
    rows: list[dict] = []
    for line in text.strip().splitlines():
        # A leading bullet ("● ") may appear; strip non-unit prefix tokens.
        parts = line.replace("●", " ").split(None, 4)
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


def failed_units() -> dict:
    """Return systemd units currently in the failed state (``systemctl --failed``).

    Raises ToolNotFoundError if systemctl is absent, or CommandError on failure.
    """
    if not shutil.which("systemctl"):
        raise ToolNotFoundError("systemctl not available — not a systemd system")

    result = subprocess.run(
        ["systemctl", "--failed", "--type=service", "--no-legend", "--no-pager", "--plain"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or "systemctl --failed failed")
    rows = _parse_failed_units(result.stdout)
    return {"count": len(rows), "units": rows}
