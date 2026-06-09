import json
import sys

import runspec as rs
from runspec_linux_core import check_port, ping_host, show_connections


def main_ping_host() -> None:
    spec = rs.parse("ping-host")
    host = str(spec.host)
    count = int(spec.count)
    try:
        print(json.dumps(ping_host(host, count)))
    except Exception as e:
        print(json.dumps({"error": str(e), "host": host}))
        sys.exit(1)


def main_check_port() -> None:
    spec = rs.parse("check-port")
    host = str(spec.host)
    port = int(spec.port)
    timeout = float(spec.timeout)
    print(json.dumps(check_port(host, port, timeout)))


def main_show_connections() -> None:
    spec = rs.parse("show-connections")
    state_filter = str(spec.state)
    try:
        print(json.dumps(show_connections(state_filter)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
