import json
import sys

import runspec as rs

# Re-exported for back-compat: callers historically did
# `from runspec_linux.nc_command import nc_send`. The implementation now lives
# in the dependency-free runspec-linux-core package.
from runspec_linux_core import nc_send

__all__ = ["main", "nc_send"]


def main() -> None:
    spec = rs.parse("nc-command")
    host = spec.host.value
    port = spec.port.value
    command = spec.command.value
    wait = spec.wait.value
    read_timeout = spec.read_timeout.value

    try:
        response = nc_send(host, port, command, wait=wait, read_timeout=read_timeout)
        lines = response.splitlines()
        print(
            json.dumps(
                {
                    "host": host,
                    "port": port,
                    "command": command,
                    "stdout": response,
                    "lines": lines,
                }
            )
        )
    except OSError as e:
        print(json.dumps({"error": str(e), "host": host, "port": port}))
        sys.exit(1)
