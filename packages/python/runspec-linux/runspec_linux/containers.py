import json
import shutil
import subprocess
import sys

import runspec as rs


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def main_list_containers() -> None:
    spec = rs.parse("list-containers")
    include_all = bool(spec.all)

    if not _docker_available():
        print(json.dumps({"error": "docker not installed or not in PATH"}))
        sys.exit(1)

    try:
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
            print(json.dumps({"error": result.stderr.strip()}))
            sys.exit(1)

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
        print(json.dumps(rows))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_container_logs() -> None:
    spec = rs.parse("container-logs")
    container = str(spec.container)
    lines = int(spec.lines)

    if not _docker_available():
        print(json.dumps({"error": "docker not installed or not in PATH"}))
        sys.exit(1)

    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), container],
            capture_output=True,
            text=True,
        )
        # docker logs writes to stderr by default
        output = result.stderr if result.stderr else result.stdout
        output_lines = output.strip().splitlines()
        print(
            json.dumps(
                {
                    "container": container,
                    "lines": output_lines,
                    "count": len(output_lines),
                }
            )
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_restart_container() -> None:
    spec = rs.parse("restart-container")
    container = str(spec.container)

    if not _docker_available():
        print(json.dumps({"error": "docker not installed or not in PATH"}))
        sys.exit(1)

    try:
        result = subprocess.run(
            ["docker", "restart", container],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                json.dumps(
                    {
                        "container": container,
                        "restarted": False,
                        "error": result.stderr.strip(),
                    }
                )
            )
            sys.exit(1)
        print(json.dumps({"container": container, "restarted": True}))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
