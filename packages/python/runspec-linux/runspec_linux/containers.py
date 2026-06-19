import json
import sys

import runspec as rs
from runspec_linux_core import container_logs, list_containers, restart_container
from runspec_linux_core.errors import CommandError


def main_list_containers() -> None:
    spec = rs.parse("list-containers")
    include_all = spec.all.value
    try:
        print(json.dumps(list_containers(include_all)))
    except Exception as e:
        # Includes ToolNotFoundError (docker absent) and CommandError (docker ps failed).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_container_logs() -> None:
    spec = rs.parse("container-logs")
    container = spec.container.value
    lines = spec.lines.value
    try:
        print(json.dumps(container_logs(container, lines)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_restart_container() -> None:
    spec = rs.parse("restart-container")
    container = spec.container.value
    try:
        print(json.dumps(restart_container(container)))
    except CommandError as e:
        print(json.dumps({"container": container, "restarted": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ToolNotFoundError (docker absent).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
