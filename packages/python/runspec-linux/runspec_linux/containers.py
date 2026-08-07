import json
import sys

import runspec as rs
from runspec_linux_core import container_logs, list_containers, restart_container
from runspec_linux_core.errors import CommandError

from runspec_linux._escalation import resolve_escalation


def main_list_containers() -> None:
    spec = rs.parse("list-containers")
    include_all = spec.all.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(list_containers(include_all, run_as=run_as, become_method=method)))
    except Exception as e:
        # Includes ToolNotFoundError (docker absent) and CommandError (docker ps failed).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_container_logs() -> None:
    spec = rs.parse("container-logs")
    container = spec.container.value
    lines = spec.lines.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(container_logs(container, lines, run_as=run_as, become_method=method)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_restart_container() -> None:
    spec = rs.parse("restart-container")
    container = spec.container.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(restart_container(container, run_as=run_as, become_method=method)))
    except CommandError as e:
        print(json.dumps({"container": container, "restarted": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ToolNotFoundError (docker absent).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
