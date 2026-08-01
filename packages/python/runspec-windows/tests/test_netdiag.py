"""Pure parsers for the corporate-networking runnables (unit-tested off-Windows).

The ``collect_*`` functions shell out / read the registry and are exercised on a
real Windows host by the smoke job; here we test the parsing that turns command
output into structured JSON.
"""

from __future__ import annotations

from runspec_windows import netdiag

# ── proxy ─────────────────────────────────────────────────────────────────────


def test_parse_winhttp_proxy_direct() -> None:
    out = netdiag.parse_winhttp_proxy("Current WinHTTP proxy settings:\n\n    Direct access (no proxy server).\n")
    assert out["direct"] is True
    assert out["proxy_server"] is None


def test_parse_winhttp_proxy_configured() -> None:
    text = "Current WinHTTP proxy settings:\n\n    Proxy Server(s) :  proxy.corp:8080\n    Bypass List     :  *.corp.local\n"
    out = netdiag.parse_winhttp_proxy(text)
    assert out["direct"] is False
    assert out["proxy_server"] == "proxy.corp:8080"
    assert out["bypass_list"] == "*.corp.local"


# ── vpn ───────────────────────────────────────────────────────────────────────


def test_parse_vpn_single_object() -> None:
    # ConvertTo-Json emits a bare object for a single connection.
    text = '{"Name":"Corp VPN","ServerAddress":"vpn.corp.com","ConnectionStatus":"Connected","TunnelType":"Automatic","SplitTunneling":false}'
    rows = netdiag.parse_vpn(text)
    assert rows == [{"name": "Corp VPN", "server": "vpn.corp.com", "status": "Connected", "tunnel": "Automatic", "auth": None, "split_tunnel": False}]


def test_parse_vpn_empty() -> None:
    assert netdiag.parse_vpn("") == []


# ── dns ───────────────────────────────────────────────────────────────────────


def test_parse_dns_servers() -> None:
    text = '[{"InterfaceAlias":"Ethernet","ServerAddresses":["10.0.0.1","10.0.0.2"]},{"InterfaceAlias":"Wi-Fi","ServerAddresses":["8.8.8.8"]}]'
    rows = netdiag.parse_dns_servers(text)
    assert rows[0] == {"interface": "Ethernet", "servers": ["10.0.0.1", "10.0.0.2"]}
    assert rows[1]["servers"] == ["8.8.8.8"]


# ── test-connection ───────────────────────────────────────────────────────────


def test_parse_test_connection() -> None:
    text = '{"ComputerName":"host","RemoteAddress":"10.1.1.1","RemotePort":443,"TcpTestSucceeded":true,"PingSucceeded":true,"PingReplyDetails":{"RoundtripTime":12}}'
    out = netdiag.parse_test_connection(text)
    assert out["remote_address"] == "10.1.1.1"
    assert out["tcp_succeeded"] is True
    assert out["rtt_ms"] == 12


# ── tracert ───────────────────────────────────────────────────────────────────


def test_parse_tracert() -> None:
    text = "Tracing route to host [10.1.1.1]\n  1     1 ms     1 ms     1 ms  gateway [192.168.1.1]\n  2     *        *        *     Request timed out.\n  3    10 ms    11 ms    10 ms  10.0.0.1\n"
    hops = netdiag.parse_tracert(text)
    assert hops[0]["hop"] == 1
    assert hops[0]["ip"] == "192.168.1.1"
    assert hops[0]["rtt_ms"] == [1, 1, 1]
    assert hops[1]["host"] == "*"
    assert hops[2]["ip"] == "10.0.0.1"


# ── route print ───────────────────────────────────────────────────────────────


def test_parse_route_print() -> None:
    text = (
        "===========================================================================\n"
        "IPv4 Route Table\n"
        "===========================================================================\n"
        "Network Destination        Netmask          Gateway       Interface  Metric\n"
        "          0.0.0.0          0.0.0.0      192.168.1.1    192.168.1.50     25\n"
        "        10.0.0.0        255.0.0.0         On-link       192.168.1.50    281\n"
        "===========================================================================\n"
        "IPv6 Route Table\n"
        "  1    331 ::1/128\n"
    )
    routes = netdiag.parse_route_print(text)
    assert routes[0] == {"destination": "0.0.0.0", "netmask": "0.0.0.0", "gateway": "192.168.1.1", "interface": "192.168.1.50", "metric": "25"}
    assert len(routes) == 2  # IPv6 section excluded


# ── gpresult ──────────────────────────────────────────────────────────────────


def test_parse_gpresult() -> None:
    text = (
        "COMPUTER SETTINGS\n"
        "------------------\n"
        "    Applied Group Policy Objects\n"
        "    -----------------------------\n"
        "        Default Domain Policy\n"
        "        Corp Security Baseline\n"
        "\n"
        "    The computer is a part of the following security groups:\n"
        "USER SETTINGS\n"
        "--------------\n"
        "    Applied Group Policy Objects\n"
        "    -----------------------------\n"
        "        Corp User Policy\n"
    )
    out = netdiag.parse_gpresult(text)
    assert "Default Domain Policy" in out["computer_policies"]
    assert "Corp Security Baseline" in out["computer_policies"]
    assert out["user_policies"] == ["Corp User Policy"]


# ── certs ─────────────────────────────────────────────────────────────────────


def test_parse_certs() -> None:
    text = '[{"Subject":"CN=Corp Root CA","Issuer":"CN=Corp Root CA","Thumbprint":"ABC123","NotAfter":"/Date(1893456000000)/","FriendlyName":"Corp Root"}]'
    rows = netdiag.parse_certs(text)
    assert rows[0]["subject"] == "CN=Corp Root CA"
    assert rows[0]["thumbprint"] == "ABC123"
