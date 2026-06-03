"""Network runnables: ping-host, check-port, show-connections, list-adapters, flush-dns."""

from __future__ import annotations

import json
import re
import socket
import time

import runspec as rs

from runspec_windows import _platform


def main_ping_host() -> None:
    spec = rs.parse("ping-host")
    _platform.ensure_windows()
    host = str(spec.host)
    count = int(spec.count)
    try:
        result = _platform.run_cmd(["ping", "-n", str(count), host])
        output = result.stdout + result.stderr
        sent = received = 0
        m = re.search(r"Sent = (\d+), Received = (\d+)", output)
        if m:
            sent, received = int(m.group(1)), int(m.group(2))
        loss_pct = round((1 - received / sent) * 100, 1) if sent else 100.0
        print(
            json.dumps(
                {
                    "host": host,
                    "reachable": received > 0,
                    "packets_sent": sent,
                    "packets_received": received,
                    "loss_pct": loss_pct,
                }
            )
        )
    except Exception as e:
        _platform.fail(str(e), host=host)


def main_check_port() -> None:
    spec = rs.parse("check-port")
    _platform.ensure_windows()
    host = str(spec.host)
    port = int(spec.port)
    timeout = float(spec.timeout)
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed = round((time.monotonic() - start) * 1000, 1)
            print(json.dumps({"host": host, "port": port, "open": True, "response_ms": elapsed}))
    except (OSError, TimeoutError):
        elapsed = round((time.monotonic() - start) * 1000, 1)
        print(json.dumps({"host": host, "port": port, "open": False, "response_ms": elapsed}))


def parse_netstat(text: str, state_filter: str = "all") -> list[dict]:
    """Parse ``netstat -ano`` output into structured rows.

    TCP rows carry a state and PID; UDP rows have no state. ``state_filter`` is
    one of ``all`` / ``established`` / ``listening``.
    """
    rows: list[dict] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("TCP", "UDP"):
            continue
        proto, local, foreign = parts[0], parts[1], parts[2]
        if proto == "TCP" and len(parts) >= 5:
            state, pid = parts[3], parts[4]
        else:  # UDP: no state column
            state, pid = "", parts[-1]

        if state_filter == "established" and state.upper() != "ESTABLISHED":
            continue
        if state_filter == "listening" and state.upper() != "LISTENING":
            continue

        try:
            pid_int: int | None = int(pid)
        except ValueError:
            pid_int = None
        rows.append({"proto": proto, "local": local, "peer": foreign, "state": state, "pid": pid_int})
    return rows


def main_show_connections() -> None:
    spec = rs.parse("show-connections")
    _platform.ensure_windows()
    state_filter = str(spec.state)
    try:
        result = _platform.run_cmd(["netstat", "-ano"])
        print(json.dumps(parse_netstat(result.stdout, state_filter)))
    except Exception as e:
        _platform.fail(str(e))


def _clean_key(raw: str) -> str:
    return re.sub(r"[.\s]+$", "", raw).strip()


def parse_ipconfig(text: str) -> list[dict]:
    """Parse ``ipconfig /all`` output into a list of adapters with IPv4 info."""
    adapters: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            # Adapter header lines look like "Ethernet adapter Ethernet:".
            if "adapter" in line.lower() and line.rstrip().endswith(":"):
                if current is not None:
                    adapters.append(current)
                current = {"adapter": line.rstrip().rstrip(":").strip(), "description": None, "mac": None, "ipv4": None, "gateway": None}
            else:
                # A non-adapter section (e.g. "Windows IP Configuration") ends the block.
                if current is not None:
                    adapters.append(current)
                current = None
            continue
        if current is None or ":" not in line:
            continue
        key_part, value = line.split(":", 1)
        key = _clean_key(key_part)
        value = value.strip()
        if key == "Description":
            current["description"] = value
        elif key == "Physical Address":
            current["mac"] = value
        elif key.startswith("IPv4 Address"):
            current["ipv4"] = value.replace("(Preferred)", "").strip()
        elif key == "Default Gateway" and value:
            current["gateway"] = value
    if current is not None:
        adapters.append(current)
    return adapters


def main_list_adapters() -> None:
    rs.parse("list-adapters")
    _platform.ensure_windows()
    try:
        result = _platform.run_cmd(["ipconfig", "/all"])
        print(json.dumps(parse_ipconfig(result.stdout)))
    except Exception as e:
        _platform.fail(str(e))


def main_flush_dns() -> None:
    rs.parse("flush-dns")
    _platform.ensure_windows()
    try:
        result = _platform.run_cmd(["ipconfig", "/flushdns"])
        if result.returncode != 0:
            _platform.fail(result.stderr.strip() or "ipconfig /flushdns failed")
        print(json.dumps({"flushed": True, "message": result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "DNS cache flushed"}))
    except Exception as e:
        _platform.fail(str(e))
