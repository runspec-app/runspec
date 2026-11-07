"""``system-service``: start/stop/restart/reload/enable/disable a systemd service as root."""

from __future__ import annotations

import json
import sys

import runspec as rs
from runspec_linux_core.errors import LinuxCoreError
from runspec_linux_core.services import control_service


def main() -> None:
    spec = rs.parse("system-service")
    action = spec.runspec_command
    now = spec.now.value if "now" in spec else False
    try:
        result = control_service(spec.service.value, action, now=bool(now))
    except (LinuxCoreError, ValueError) as exc:
        print(json.dumps({"service": spec.service.value, "action": action, "error": str(exc), "kind": type(exc).__name__}))
        sys.exit(1)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
