import json
import sys

import runspec as rs
from runspec_linux_core import check_service, list_services, restart_service
from runspec_linux_core.errors import CommandError

from runspec_linux._escalation import resolve_escalation


def main_check_service() -> None:
    spec = rs.parse("check-service")
    service = spec.service.value
    try:
        print(json.dumps(check_service(service)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_list_services() -> None:
    spec = rs.parse("list-services")
    state_filter = spec.state.value
    try:
        print(json.dumps(list_services(state_filter)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_restart_service() -> None:
    spec = rs.parse("restart-service")
    service = spec.service.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(restart_service(service, run_as=run_as, become_method=method)))
    except CommandError as e:
        print(json.dumps({"service": service, "restarted": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ToolNotFoundError (systemctl absent).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
