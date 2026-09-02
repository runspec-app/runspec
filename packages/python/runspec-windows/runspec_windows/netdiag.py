"""Corporate networking diagnostics for the locked-down Windows desktop:
proxy (WinHTTP / WinINET / PAC / env), VPN, DNS, connectivity, route, applied
group policy, and the machine root-CA store.

Every runnable is read-only (``autonomous``). The OS-specific work (``netsh`` /
PowerShell / ``winreg`` / ``tracert`` / ``route``) lives in ``collect_*`` /
``_read_*`` helpers behind :func:`_platform.ensure_windows`; the pure ``parse_*``
functions are unit-tested on any platform. ``collect_*`` return plain dicts so
``collect-diagnostics`` can compose them into one report.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import runspec as rs
from runspec_webops_core import resolve_proxy

from runspec_windows import _platform


def _json_rows(text: str) -> list[dict]:
    """Normalise ``ConvertTo-Json`` output (a single object *or* an array, or
    empty) into a list of dicts."""
    text = text.strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


# ── proxy-settings ────────────────────────────────────────────────────────────


def parse_winhttp_proxy(text: str) -> dict:
    """Parse ``netsh winhttp show proxy`` output into direct / proxy / bypass."""
    out: dict[str, Any] = {"direct": False, "proxy_server": None, "bypass_list": None}
    low = text.lower()
    if "direct access" in low or "no proxy server" in low:
        out["direct"] = True
    m = re.search(r"Proxy Server\(s\)\s*:\s*(.+)", text)
    if m:
        out["proxy_server"] = m.group(1).strip()
        out["direct"] = False
    m2 = re.search(r"Bypass List\s*:\s*(.+)", text)
    if m2 and m2.group(1).strip() not in ("", "(none)"):
        out["bypass_list"] = m2.group(1).strip()
    return out


def _read_wininet_proxy() -> dict:
    """Read the per-user WinINET proxy config from the registry (Windows only)."""
    import winreg  # local import — Windows only

    out: dict[str, Any] = {"proxy_enable": False, "proxy_server": None, "auto_config_url": None}
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
    except OSError:
        return out
    try:
        for name, attr in (("ProxyEnable", "proxy_enable"), ("ProxyServer", "proxy_server"), ("AutoConfigURL", "auto_config_url")):
            try:
                value, _ = winreg.QueryValueEx(key, name)
                out[attr] = bool(value) if name == "ProxyEnable" else str(value)
            except OSError:
                continue
    finally:
        key.Close()
    return out


def _proxy_env() -> dict:
    """The proxy-related environment variables (upper- and lower-case)."""
    keys = ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"]
    env: dict[str, str] = {}
    for k in keys:
        val = os.environ.get(k) or os.environ.get(k.lower())
        if val:
            env[k] = val
    return env


def collect_proxy() -> dict:
    """Gather the full proxy picture: WinHTTP (system), WinINET (per-user browser),
    PAC (AutoConfigURL), and process environment proxy vars."""
    result = _platform.run_cmd(["netsh", "winhttp", "show", "proxy"])
    winhttp = parse_winhttp_proxy(result.stdout)
    wininet = _read_wininet_proxy()
    return {
        "winhttp": winhttp,
        "wininet": wininet,
        "pac_url": wininet.get("auto_config_url"),
        "env": _proxy_env(),
    }


def effective_proxy(url: str) -> str:
    """Resolve the *effective* proxy a specific URL would use — the real decision
    the web runnables make (proxy env vars, then the Windows system config with
    WPAD/PAC evaluated for this URL). Returns ``"direct"`` when none applies.

    This is the actionable counterpart to the static config that
    :func:`collect_proxy` reports: it says which proxy a given URL actually goes
    through, PAC and bypass rules included.
    """
    return resolve_proxy(url) or "direct"


def main_proxy_settings() -> None:
    spec = rs.parse("proxy-settings")
    _platform.ensure_windows()
    try:
        result = collect_proxy()
        url = spec.url.value
        if url:
            result["effective_for_url"] = {"url": url, "proxy": effective_proxy(url)}
        print(json.dumps(result))
    except Exception as e:
        _platform.fail(str(e))


# ── vpn-status ────────────────────────────────────────────────────────────────


def parse_vpn(json_text: str) -> list[dict]:
    """Parse ``Get-VpnConnection | ConvertTo-Json`` into connection rows."""
    rows: list[dict] = []
    for item in _json_rows(json_text):
        rows.append(
            {
                "name": item.get("Name"),
                "server": item.get("ServerAddress"),
                "status": item.get("ConnectionStatus"),
                "tunnel": item.get("TunnelType"),
                "auth": item.get("AuthenticationMethod"),
                "split_tunnel": item.get("SplitTunneling"),
            }
        )
    return rows


def collect_vpn() -> dict:
    """List configured VPN connections and their connection status."""
    script = "Get-VpnConnection -AllUserConnection -ErrorAction SilentlyContinue | Select-Object Name,ServerAddress,ConnectionStatus,TunnelType,AuthenticationMethod,SplitTunneling | ConvertTo-Json"
    try:
        out = _platform.run_powershell(script)
    except RuntimeError:
        out = ""
    connections = parse_vpn(out)
    connected = [c for c in connections if str(c.get("status")).lower() == "connected"]
    return {"count": len(connections), "connected": len(connected), "connections": connections}


def main_vpn_status() -> None:
    rs.parse("vpn-status")
    _platform.ensure_windows()
    try:
        print(json.dumps(collect_vpn()))
    except Exception as e:
        _platform.fail(str(e))


# ── dns-config ────────────────────────────────────────────────────────────────


def parse_dns_servers(json_text: str) -> list[dict]:
    """Parse ``Get-DnsClientServerAddress | ConvertTo-Json`` into per-interface
    server lists."""
    rows: list[dict] = []
    for item in _json_rows(json_text):
        servers = item.get("ServerAddresses") or []
        if isinstance(servers, str):
            servers = [servers]
        rows.append({"interface": item.get("InterfaceAlias"), "servers": list(servers)})
    return rows


def collect_dns() -> dict:
    """Report configured DNS servers per interface (IPv4)."""
    script = "Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.ServerAddresses } | Select-Object InterfaceAlias,ServerAddresses | ConvertTo-Json"
    try:
        out = _platform.run_powershell(script)
    except RuntimeError:
        out = ""
    return {"interfaces": parse_dns_servers(out)}


def main_dns_config() -> None:
    rs.parse("dns-config")
    _platform.ensure_windows()
    try:
        print(json.dumps(collect_dns()))
    except Exception as e:
        _platform.fail(str(e))


# ── test-connection ───────────────────────────────────────────────────────────


def parse_test_connection(json_text: str) -> dict:
    """Parse a ``Test-NetConnection | ConvertTo-Json`` object into a flat result."""
    rows = _json_rows(json_text)
    if not rows:
        return {}
    item = rows[0]
    ping = item.get("PingReplyDetails") or {}
    rtt = ping.get("RoundtripTime") if isinstance(ping, dict) else None
    return {
        "computer": item.get("ComputerName"),
        "remote_address": item.get("RemoteAddress"),
        "remote_port": item.get("RemotePort"),
        "tcp_succeeded": item.get("TcpTestSucceeded"),
        "ping_succeeded": item.get("PingSucceeded"),
        "rtt_ms": rtt,
        "source_address": (item.get("SourceAddress") or {}).get("IPAddress") if isinstance(item.get("SourceAddress"), dict) else item.get("SourceAddress"),
        "interface_alias": item.get("InterfaceAlias"),
    }


def collect_test_connection(target: str, port: int | None = None) -> dict:
    """Run ``Test-NetConnection`` against ``target`` (optionally a TCP ``port``)."""
    safe = target.replace('"', "")
    if port:
        script = f'Test-NetConnection -ComputerName "{safe}" -Port {int(port)} -WarningAction SilentlyContinue | ConvertTo-Json -Depth 3'
    else:
        script = f'Test-NetConnection -ComputerName "{safe}" -WarningAction SilentlyContinue | ConvertTo-Json -Depth 3'
    try:
        out = _platform.run_powershell(script)
    except RuntimeError as e:
        return {"target": target, "error": str(e)}
    result = parse_test_connection(out)
    result["target"] = target
    return result


def main_test_connection() -> None:
    spec = rs.parse("test-connection")
    _platform.ensure_windows()
    target = str(spec.target.value)
    port = spec.port.value
    try:
        print(json.dumps(collect_test_connection(target, port)))
    except Exception as e:
        _platform.fail(str(e), target=target)


# ── trace-route ───────────────────────────────────────────────────────────────


def parse_tracert(text: str) -> list[dict]:
    """Parse ``tracert`` output into hop rows (hop number, RTT samples, host/ip)."""
    hops: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^(\d+)\s+(.*)$", stripped)
        if not m:
            continue
        hop_no = int(m.group(1))
        rest = m.group(2)
        times = [int(t) for t in re.findall(r"(\d+)\s*ms", rest)]
        if "Request timed out" in rest or rest.count("*") >= 3:
            host = "*"
            ip = ""
        else:
            ipm = re.search(r"([\d]+\.[\d]+\.[\d]+\.[\d]+)", rest)
            ip = ipm.group(1) if ipm else ""
            hostm = re.search(r"^([\w.-]+)\s+\[", rest)
            host = hostm.group(1) if hostm else (ip or "")
        hops.append({"hop": hop_no, "rtt_ms": times, "host": host, "ip": ip})
    return hops


def collect_trace_route(host: str, max_hops: int = 30) -> dict:
    result = _platform.run_cmd(["tracert", "-h", str(max_hops), "-w", "1000", host], timeout=120)
    hops = parse_tracert(result.stdout)
    return {"host": host, "max_hops": max_hops, "hops": hops, "raw": result.stdout.strip().splitlines()}


def main_trace_route() -> None:
    spec = rs.parse("trace-route")
    _platform.ensure_windows()
    host = str(spec.host.value)
    max_hops = spec.max_hops.value
    try:
        print(json.dumps(collect_trace_route(host, max_hops)))
    except Exception as e:
        _platform.fail(str(e), host=host)


# ── route-table ───────────────────────────────────────────────────────────────


def parse_route_print(text: str) -> list[dict]:
    """Parse the IPv4 route table out of ``route print`` output."""
    routes: list[dict] = []
    in_ipv4 = False
    for line in text.splitlines():
        stripped = line.strip()
        if "IPv4 Route Table" in stripped:
            in_ipv4 = True
            continue
        if in_ipv4 and "IPv6 Route Table" in stripped:
            break
        if not in_ipv4:
            continue
        parts = stripped.split()
        # data rows: dest mask gateway interface metric  (5 columns, dest is an IP)
        if len(parts) == 5 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
            routes.append(
                {
                    "destination": parts[0],
                    "netmask": parts[1],
                    "gateway": parts[2],
                    "interface": parts[3],
                    "metric": parts[4],
                }
            )
    return routes


def collect_route() -> dict:
    result = _platform.run_cmd(["route", "print", "-4"])
    return {"routes": parse_route_print(result.stdout)}


def main_route_table() -> None:
    rs.parse("route-table")
    _platform.ensure_windows()
    try:
        print(json.dumps(collect_route()))
    except Exception as e:
        _platform.fail(str(e))


# ── gpresult ──────────────────────────────────────────────────────────────────


def parse_gpresult(text: str) -> dict:
    """Parse ``gpresult /r`` into the applied-policy summary (computer + user GPOs)."""
    scopes: dict[str, list[str]] = {"computer": [], "user": []}
    current: str | None = None
    collecting = False
    for line in text.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("computer settings"):
            current = "computer"
            collecting = False
            continue
        if low.startswith("user settings"):
            current = "user"
            collecting = False
            continue
        if "applied group policy objects" in low:
            collecting = True
            continue
        if collecting and current:
            if not stripped or set(stripped) <= {"-"}:
                continue
            if stripped.startswith(("The following", "N/A")) or low.endswith(":"):
                collecting = False
                continue
            scopes[current].append(stripped)
    return {"computer_policies": scopes["computer"], "user_policies": scopes["user"]}


def collect_gpo() -> dict:
    result = _platform.run_cmd(["gpresult", "/r"], timeout=60)
    return parse_gpresult(result.stdout)


def main_gpresult() -> None:
    rs.parse("gpresult")
    _platform.ensure_windows()
    try:
        print(json.dumps(collect_gpo()))
    except Exception as e:
        _platform.fail(str(e))


# ── list-certs (machine root store) ───────────────────────────────────────────


def parse_certs(json_text: str) -> list[dict]:
    """Parse a ``Get-ChildItem Cert:\\... | ConvertTo-Json`` list into cert rows."""
    rows: list[dict] = []
    for item in _json_rows(json_text):
        rows.append(
            {
                "subject": item.get("Subject"),
                "issuer": item.get("Issuer"),
                "thumbprint": item.get("Thumbprint"),
                "not_after": item.get("NotAfter"),
                "friendly_name": item.get("FriendlyName"),
            }
        )
    return rows


def collect_certs(name_filter: str | None = None) -> dict:
    """List certificates in the machine root store (corporate root-CA check)."""
    script = "Get-ChildItem Cert:\\LocalMachine\\Root | Select-Object Subject,Issuer,Thumbprint,NotAfter,FriendlyName | ConvertTo-Json"
    try:
        out = _platform.run_powershell(script)
    except RuntimeError:
        out = ""
    certs = parse_certs(out)
    if name_filter:
        needle = name_filter.lower()
        certs = [c for c in certs if needle in (c.get("subject") or "").lower() or needle in (c.get("friendly_name") or "").lower()]
    return {"store": "LocalMachine\\Root", "count": len(certs), "certificates": certs}


def main_list_certs() -> None:
    spec = rs.parse("list-certs")
    _platform.ensure_windows()
    name_filter = spec.filter.value or None
    try:
        print(json.dumps(collect_certs(name_filter)))
    except Exception as e:
        _platform.fail(str(e))
