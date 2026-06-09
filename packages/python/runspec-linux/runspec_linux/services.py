import json
import sys

import runspec as rs
from runspec_linux_core import check_service, list_services, restart_service
from runspec_linux_core.errors import CommandError


def main_check_service() -> None:
    spec = rs.parse("check-service")
    service = str(spec.service)
    try:
        print(json.dumps(check_service(service)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_list_services() -> None:
    spec = rs.parse("list-services")
    state_filter = str(spec.state)
    try:
        print(json.dumps(list_services(state_filter)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_restart_service() -> None:
    spec = rs.parse("restart-service")
    service = str(spec.service)
    try:
        print(json.dumps(restart_service(service)))
    except CommandError as e:
        print(json.dumps({"service": service, "restarted": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ToolNotFoundError (systemctl absent).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
