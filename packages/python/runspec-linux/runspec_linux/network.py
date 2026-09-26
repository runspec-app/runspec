import json
import sys

import runspec as rs
from runspec_linux_core import (
    check_port,
    dns_config,
    network_interfaces,
    ping_host,
    route_table,
    show_connections,
    socket_stats,
    trace_route,
)


def main_ping_host() -> None:
    spec = rs.parse("ping-host")
    host = spec.host.value
    count = spec.count.value
    try:
        print(json.dumps(ping_host(host, count)))
    except Exception as e:
        print(json.dumps({"error": str(e), "host": host}))
        sys.exit(1)


def main_check_port() -> None:
    spec = rs.parse("check-port")
    host = spec.host.value
    port = spec.port.value
    timeout = spec.timeout.value
    print(json.dumps(check_port(host, port, timeout)))


def main_show_connections() -> None:
    spec = rs.parse("show-connections")
    state_filter = spec.state.value
    try:
        print(json.dumps(show_connections(state_filter)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_trace_route() -> None:
    spec = rs.parse("trace-route")
    host = spec.host.value
    max_hops = spec.max_hops.value
    try:
        print(json.dumps(trace_route(host, max_hops)))
    except Exception as e:
        print(json.dumps({"error": str(e), "host": host}))
        sys.exit(1)


def main_route_table() -> None:
    rs.parse("route-table")
    try:
        print(json.dumps(route_table()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_socket_stats() -> None:
    rs.parse("socket-stats")
    try:
        print(json.dumps(socket_stats()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_dns_config() -> None:
    rs.parse("dns-config")
    try:
        print(json.dumps(dns_config()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_network_interfaces() -> None:
    spec = rs.parse("network-interfaces")
    physical_only = spec.physical.value
    try:
        print(json.dumps(network_interfaces(physical_only=physical_only)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
