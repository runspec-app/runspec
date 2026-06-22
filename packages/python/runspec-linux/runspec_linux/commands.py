import json
import sys

import runspec as rs
from runspec_linux_core import which
from runspec_linux_core.errors import LinuxCoreError


def main_which() -> None:
    spec = rs.parse("which")
    try:
        result = which(
            spec.name.value,
            all_matches=spec.all.value,
            path=spec.path.value or None,
        )
    except (ValueError, LinuxCoreError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    print(json.dumps(result))
    # Mirror the `which` command: non-zero exit when any name was not found.
    if not result["all_found"]:
        sys.exit(1)
