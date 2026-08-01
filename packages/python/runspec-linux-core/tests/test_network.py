"""check_port treats a refused/timed-out connection as a result, not an error."""

import socket

from runspec_linux_core import check_port


def test_check_port_open() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        result = check_port("127.0.0.1", port, timeout=1.0)
    assert result["open"] is True
    assert result["port"] == port


def test_check_port_closed_does_not_raise() -> None:
    result = check_port("127.0.0.1", 1, timeout=0.2)
    assert result["open"] is False
    assert "response_ms" in result


# ── Deep-diagnostics parsers ──────────────────────────────────────────────────

from runspec_linux_core import network  # noqa: E402


def test_parse_traceroute() -> None:
    text = " 1  gateway (192.168.1.1)  0.5 ms  0.4 ms  0.6 ms\n 2  10.0.0.1 (10.0.0.1)  5.1 ms  4.9 ms  5.0 ms\n 3  * * *\n"
    hops = network._parse_traceroute(text)
    assert hops[0] == {"hop": 1, "host": "gateway", "ip": "192.168.1.1", "rtt_ms": [0.5, 0.4, 0.6]}
    assert hops[2]["host"] == "*"
    assert hops[2]["rtt_ms"] == []


def test_parse_ip_route() -> None:
    text = "default via 192.168.1.1 dev eth0 proto dhcp metric 100\n192.168.1.0/24 dev eth0 proto kernel scope link\n"
    routes = network._parse_ip_route(text)
    assert routes[0]["destination"] == "default"
    assert routes[0]["via"] == "192.168.1.1"
    assert routes[0]["dev"] == "eth0"
    assert routes[1]["destination"] == "192.168.1.0/24"


def test_parse_ss_summary() -> None:
    text = "Total: 250\nTCP:   12 (estab 5, closed 3)\nUDP:   4\n"
    summary = network._parse_ss_summary(text)
    assert summary["total"] == 250
    assert summary["tcp"]["estab"] == 5
    assert summary["tcp"]["closed"] == 3


def test_parse_resolv_conf() -> None:
    text = "# comment\nnameserver 8.8.8.8\nnameserver 1.1.1.1\nsearch corp.local example.com\noptions timeout:2 ndots:1\n"
    conf = network._parse_resolv_conf(text)
    assert conf["nameservers"] == ["8.8.8.8", "1.1.1.1"]
    assert conf["search"] == ["corp.local", "example.com"]
    assert conf["options"] == ["timeout:2", "ndots:1"]
