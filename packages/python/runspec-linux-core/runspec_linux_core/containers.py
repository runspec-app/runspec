"""Docker container helpers: list, logs, restart."""

import shutil
import subprocess

from runspec_linux_core.errors import CommandError, ToolNotFoundError


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def list_containers(include_all: bool = False) -> list[dict]:
    """Return Docker containers (running, or all if ``include_all``).

    Raises ToolNotFoundError if docker is not installed, or CommandError if the
    ``docker ps`` command returns a non-zero exit.
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

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise CommandError(result.stderr.strip())

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


def container_logs(container: str, lines: int = 50) -> dict:
    """Return the last ``lines`` log lines for a container.

    Raises ToolNotFoundError if docker is not installed.
    """
    if not _docker_available():
        raise ToolNotFoundError("docker not installed or not in PATH")

    result = subprocess.run(
        ["docker", "logs", "--tail", str(lines), container],
        capture_output=True,
        text=True,
    )
    # docker logs writes to stderr by default
    output = result.stderr if result.stderr else result.stdout
    output_lines = output.strip().splitlines()
    return {"container": container, "lines": output_lines, "count": len(output_lines)}


def restart_container(container: str) -> dict:
    """Restart a container. Returns ``{container, restarted: True}`` on success.

    Raises ToolNotFoundError if docker is not installed, or CommandError if the
    restart returns a non-zero exit (message is the captured stderr).
    """
    if not _docker_available():
        raise ToolNotFoundError("docker not installed or not in PATH")

    result = subprocess.run(
        ["docker", "restart", container],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CommandError(result.stderr.strip())
    return {"container": container, "restarted": True}
