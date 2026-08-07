"""Docker container helpers: list, logs, restart.

The Docker daemon socket is typically root-owned (or restricted to the ``docker``
group), so each helper takes an optional ``run_as`` user (with a
``become_method``). When set, the ``docker`` command is escalated via
``sudo -n -u`` / ``su`` (:func:`runspec_linux_core.become.build_become`) — a
``--sudo`` flag on the runnable maps to ``run_as="root"``. With no ``run_as`` the
command runs as the calling user (unchanged).
"""

import shutil
import subprocess

from runspec_linux_core.become import build_become, check_become_error
from runspec_linux_core.errors import ToolNotFoundError


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def list_containers(include_all: bool = False, *, run_as: str | None = None, become_method: str = "sudo") -> list[dict]:
    """Return Docker containers (running, or all if ``include_all``).

    Raises ToolNotFoundError if docker is not installed, or CommandError if the
    ``docker ps`` command returns a non-zero exit (with a passwordless-sudo hint
    when ``run_as`` is set).
    """
    if not _docker_available():
        raise ToolNotFoundError("docker not installed or not in PATH")

    cmd = [
        "docker",
        "ps",
        "--format",
        "{{.ID}}\t{{.Image}}\t{{.Command}}\t{{.CreatedAt}}\t{{.Status}}\t{{.Names}}",
    ]
    if include_all:
        cmd.append("--all")

    result = subprocess.run(build_become(cmd, run_as, become_method), capture_output=True, text=True)
    if result.returncode != 0:
        check_become_error("list containers", result.stderr, run_as)

    rows = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        rows.append(
            {
                "id": parts[0],
                "image": parts[1],
                "command": parts[2].strip('"'),
                "created": parts[3],
                "status": parts[4],
                "name": parts[5],
            }
        )
    return rows


def container_logs(container: str, lines: int = 50, *, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """Return the last ``lines`` log lines for a container.

    Raises ToolNotFoundError if docker is not installed. When ``run_as`` is set
    and the escalated command fails, raises CommandError with a passwordless-sudo
    hint rather than returning the sudo error as log lines.
    """
    if not _docker_available():
        raise ToolNotFoundError("docker not installed or not in PATH")

    argv = build_become(["docker", "logs", "--tail", str(lines), container], run_as, become_method)
    result = subprocess.run(argv, capture_output=True, text=True)
    if run_as and result.returncode != 0:
        check_become_error("container logs", result.stderr, run_as)
    # docker logs writes to stderr by default
    output = result.stderr if result.stderr else result.stdout
    output_lines = output.strip().splitlines()
    out = {"container": container, "lines": output_lines, "count": len(output_lines)}
    if run_as:
        out["run_as"] = run_as
    return out


def restart_container(container: str, *, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """Restart a container. Returns ``{container, restarted: True}`` on success.

    Raises ToolNotFoundError if docker is not installed, or CommandError if the
    restart returns a non-zero exit (with a passwordless-sudo hint when
    ``run_as`` is set).
    """
    if not _docker_available():
        raise ToolNotFoundError("docker not installed or not in PATH")

    result = subprocess.run(build_become(["docker", "restart", container], run_as, become_method), capture_output=True, text=True)
    if result.returncode != 0:
        check_become_error("restart container", result.stderr, run_as)
    out = {"container": container, "restarted": True}
    if run_as:
        out["run_as"] = run_as
    return out
