"""Network helpers: ping, TCP port check, socket connection listing, plus deep
diagnostics (traceroute, route table, socket summary, DNS config) for tracing
where an app's connectivity breaks."""

import json
import os
import re
import shutil
import socket
import subprocess
import time

from runspec_linux_core._paths import which
from runspec_linux_core.errors import CommandError, ToolNotFoundError

#: Where the kernel exposes per-interface attributes (speed, driver, device link).
SYS_CLASS_NET = "/sys/class/net"


def ping_host(host: str, count: int = 4) -> dict:
    """Ping a host ``count`` times and return reachability + packet stats."""
    result = subprocess.run(
        ["ping", "-c", str(count), host],
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr

    sent = received = 0
    m = re.search(r"(\d+) packets transmitted,\s*(\d+) received", output)
    if m:
        sent = int(m.group(1))
        received = int(m.group(2))

    loss_pct = 0.0
    m2 = re.search(r"([\d.]+)% packet loss", output)
    if m2:
        loss_pct = float(m2.group(1))

    return {
        "host": host,
        "reachable": received > 0,
        "packets_sent": sent,
        "packets_received": received,
        "loss_pct": loss_pct,
    }


def check_port(host: str, port: int, timeout: float = 3.0) -> dict:
    """Check whether a TCP port accepts a connection.

    A refused/timed-out connection is a valid result (``open: False``), not an
    error — this function does not raise on connection failure.
    """
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            return {"host": host, "port": port, "open": True, "response_ms": elapsed_ms}
    except (OSError, TimeoutError):
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        return {"host": host, "port": port, "open": False, "response_ms": elapsed_ms}


def show_connections(state_filter: str = "all") -> list[dict]:
    """Return active socket connections via ``ss`` (preferred) or ``netstat``.

    Raises ToolNotFoundError if neither tool is on PATH.
    """
    if shutil.which("ss"):
        result = subprocess.run(["ss", "-tupn"], capture_output=True, text=True, check=True)
        return _parse_ss(result.stdout, state_filter)
    if shutil.which("netstat"):
        result = subprocess.run(["netstat", "-tupn"], capture_output=True, text=True)
        return _parse_netstat(result.stdout, state_filter)
    raise ToolNotFoundError("Neither ss nor netstat found in PATH")


def _parse_ss(output: str, state_filter: str) -> list[dict]:
    rows = []
    for line in output.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        proto, local, peer = parts[0], parts[3], parts[4]
        state = parts[5] if len(parts) > 5 and not parts[5].startswith("users:") else ""
        process = ""
        for p in parts:
            if p.startswith("users:"):
                m = re.search(r'"([^"]+)"', p)
                if m:
                    process = m.group(1)

        row_state = state.lower() if state else ("listen" if peer == "*:*" else "established")
        if state_filter == "established" and "estab" not in row_state:
            continue
        if state_filter == "listening" and "listen" not in row_state:
            continue

        rows.append(
            {
                "proto": proto,
                "local": local,
                "peer": peer,
                "state": state,
                "process": process,
            }
        )
    return rows


def _parse_netstat(output: str, state_filter: str) -> list[dict]:
    rows = []
    for line in output.strip().splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("tcp", "tcp6", "udp", "udp6"):
            continue
        if len(parts) < 6:
            continue
        proto, local, foreign = parts[0], parts[3], parts[4]
        state = parts[5] if len(parts) > 5 else ""
        process = parts[-1] if len(parts) > 6 else ""

        if state_filter == "established" and state.upper() != "ESTABLISHED":
            continue
        if state_filter == "listening" and state.upper() != "LISTEN":
            continue

        rows.append(
            {
                "proto": proto,
                "local": local,
                "peer": foreign,
                "state": state,
                "process": process,
            }
        )
    return rows


# ── Deep diagnostics ──────────────────────────────────────────────────────────


def _parse_traceroute(output: str) -> list[dict]:
    """Parse ``traceroute`` / ``tracepath`` output into hop rows.

    Each hop line starts with the hop number, then host/ip and RTT samples. A
    ``*`` sample means no reply. Bounded, best-effort — the raw lines are also
    returned by ``trace_route`` for anything the parse misses.
    """
    hops: list[dict] = []
    for line in output.splitlines():
        stripped = line.strip()
        m = re.match(r"^(\d+)\s+(.*)$", stripped)
        if not m:
            continue
        hop_no = int(m.group(1))
        rest = m.group(2)
        host = ""
        ip = ""
        hm = re.search(r"([\w.-]+)\s+\(([\d.:a-fA-F]+)\)", rest)
        if hm:
            host, ip = hm.group(1), hm.group(2)
        elif rest.strip().startswith("*"):
            host = "*"
        times = [float(t) for t in re.findall(r"([\d.]+)\s*ms", rest)]
        hops.append({"hop": hop_no, "host": host, "ip": ip, "rtt_ms": times})
    return hops


def trace_route(host: str, max_hops: int = 30) -> dict:
    """Trace the network path to ``host`` (``traceroute`` or ``tracepath``).

    Raises ToolNotFoundError if neither tool is on PATH.
    """
    if shutil.which("traceroute"):
        cmd = ["traceroute", "-m", str(max_hops), host]
    elif shutil.which("tracepath"):
        cmd = ["tracepath", "-m", str(max_hops), host]
    else:
        raise ToolNotFoundError("neither traceroute nor tracepath found in PATH")

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    hops = _parse_traceroute(output)
    return {"host": host, "max_hops": max_hops, "hops": hops, "raw": output.strip().splitlines()}


def _parse_ip_route(output: str) -> list[dict]:
    """Parse ``ip route`` output into route rows (destination + key/value attrs)."""
    routes: list[dict] = []
    for line in output.strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        route: dict = {"destination": parts[0]}
        i = 1
        while i < len(parts) - 1:
            route[parts[i]] = parts[i + 1]
            i += 2
        routes.append(route)
    return routes


def route_table() -> dict:
    """Return the kernel IPv4 routing table (``ip route``).

    Raises ToolNotFoundError if ``ip`` is not on PATH.
    """
    if not shutil.which("ip"):
        raise ToolNotFoundError("ip not available (iproute2)")
    result = subprocess.run(["ip", "route"], capture_output=True, text=True)
    return {"routes": _parse_ip_route(result.stdout)}


def network_interfaces(physical_only: bool = False) -> dict:
    """List network interfaces (``ip -json address``): state, MAC address, MTU,
    bridge/bond ``master`` and IPv4/IPv6 addresses, plus link speed and driver
    for physical cards (from ``/sys/class/net``).

    ``physical`` is true for an interface backed by a hardware device; loopback,
    bridges, bonds, VLANs, veth pairs and tunnels are virtual. ``physical_only``
    drops the virtual ones — container and Kubernetes hosts carry many veth links.
    An address has ``dynamic: true`` when it came from DHCP or SLAAC.

    Raises ToolNotFoundError if ``ip`` is not installed, CommandError if it fails.
    """
    ip = which("ip")
    if not ip:
        raise ToolNotFoundError("ip not available (iproute2)")
    result = subprocess.run([ip, "-json", "address", "show"], capture_output=True, text=True)
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or f"ip exited {result.returncode}")
    try:
        links = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(f"ip printed output that isn't JSON (iproute2 may be too old for -json): {exc}") from exc
    # Some iproute2 versions emit empty objects in the list; skip anything unnamed.
    interfaces = [_interface_row(link) for link in links if link.get("ifname")]
    if physical_only:
        interfaces = [row for row in interfaces if row["physical"]]
    return {"interfaces": interfaces}


def _sysfs_attr(name: str, attr: str) -> str | None:
    try:
        with open(os.path.join(SYS_CLASS_NET, name, attr)) as f:
            return f.read().strip()
    except OSError:
        return None  # absent, or unreadable (speed raises EINVAL on a down link)


def _interface_row(link: dict) -> dict:
    name = link["ifname"]
    physical = os.path.exists(os.path.join(SYS_CLASS_NET, name, "device"))
    addresses = []
    for addr in link.get("addr_info", []):
        if not addr.get("local"):
            continue
        entry = {"family": addr.get("family"), "address": addr["local"], "prefix": addr.get("prefixlen"), "scope": addr.get("scope")}
        if addr.get("dynamic"):
            entry["dynamic"] = True
        addresses.append(entry)
    row: dict = {
        "name": name,
        "state": link.get("operstate"),
        "physical": physical,
        "type": link.get("link_type"),
        "mac": link.get("address") if link.get("link_type") == "ether" else None,
        "mtu": link.get("mtu"),
        "master": link.get("master"),
        "addresses": addresses,
    }
    if physical:
        speed = _sysfs_attr(name, "speed")
        mbps = int(speed) if speed and speed.lstrip("-").isdigit() else -1
        row["speed_mbps"] = mbps if mbps > 0 else None  # the kernel reports -1 with no link
        try:
            row["driver"] = os.path.basename(os.readlink(os.path.join(SYS_CLASS_NET, name, "device", "driver")))
        except OSError:
            row["driver"] = None
    return {key: value for key, value in row.items() if value is not None}


def _parse_ss_summary(output: str) -> dict:
    """Parse ``ss -s`` summary output into ``{total, tcp: {...}, ...}``."""
    summary: dict = {}
    for line in output.splitlines():
        line = line.strip()
        tm = re.match(r"^Total:\s*(\d+)", line)
        if tm:
            summary["total"] = int(tm.group(1))
            continue
        pm = re.match(r"^(TCP|UDP|RAW|FRAG|INET):\s+(.*)$", line, re.IGNORECASE)
        if pm:
            proto = pm.group(1).lower()
            nums = re.findall(r"(\w+)\s+(\d+)", pm.group(2))
            summary[proto] = {k: int(v) for k, v in nums}
    return summary


def socket_stats() -> dict:
    """Return a summary of socket usage by protocol (``ss -s``).

    Raises ToolNotFoundError if ``ss`` is not on PATH.
    """
    if not shutil.which("ss"):
        raise ToolNotFoundError("ss not available (iproute2)")
    result = subprocess.run(["ss", "-s"], capture_output=True, text=True)
    return {"summary": _parse_ss_summary(result.stdout), "raw": result.stdout.strip().splitlines()}


def _parse_resolv_conf(text: str) -> dict:
    """Parse ``/etc/resolv.conf`` into nameservers / search domains / options."""
    nameservers: list[str] = []
    search: list[str] = []
    options: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        parts = line.split()
        if parts[0] == "nameserver" and len(parts) > 1:
            nameservers.append(parts[1])
        elif parts[0] in ("search", "domain"):
            search.extend(parts[1:])
        elif parts[0] == "options":
            options.extend(parts[1:])
    return {"nameservers": nameservers, "search": search, "options": options}


def dns_config() -> dict:
    """Return the host's DNS configuration: nameservers, search domains, and
    options from ``/etc/resolv.conf`` (plus ``resolvectl status`` when present).

    Raises OSError only if ``/etc/resolv.conf`` cannot be read.
    """
    with open("/etc/resolv.conf", errors="replace") as f:
        conf = _parse_resolv_conf(f.read())

    resolvectl = ""
    if shutil.which("resolvectl"):
        result = subprocess.run(["resolvectl", "status"], capture_output=True, text=True)
        if result.returncode == 0:
            resolvectl = result.stdout.strip()

    out = dict(conf)
    out["resolv_conf"] = "/etc/resolv.conf"
    if resolvectl:
        out["resolvectl_status"] = resolvectl.splitlines()
    return out
