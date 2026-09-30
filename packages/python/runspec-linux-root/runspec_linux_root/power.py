import json
import sys

import runspec as rs
from runspec_linux_core import reboot_host
from runspec_linux_core.errors import CommandError


def main_reboot_host() -> None:
    spec = rs.parse("reboot-host")
    delay_minutes = spec.delay_minutes.value
    try:
        print(json.dumps(reboot_host(delay_minutes)))
    except CommandError as e:
        print(json.dumps({"scheduled": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ToolNotFoundError (no shutdown/systemctl).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
