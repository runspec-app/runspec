import json
import sys

import runspec as rs
from runspec_linux_core import list_autoremovable, list_packages


def main_list_packages() -> None:
    spec = rs.parse("list-packages")
    name_filter = spec.filter.value
    limit = spec.limit.value
    try:
        print(json.dumps(list_packages(name_filter, limit, reason=spec.reason.value)))
    except Exception as e:
        # Includes ToolNotFoundError (no package manager) and CommandError.
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_list_autoremovable() -> None:
    spec = rs.parse("list-autoremovable")
    try:
        print(json.dumps(list_autoremovable(purge=spec.purge.value)))
    except Exception as e:
        # Includes CommandError (yum, failed listing) and ToolNotFoundError.
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
