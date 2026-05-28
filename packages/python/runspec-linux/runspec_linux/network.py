import json
import re
import shutil
import socket
import subprocess
import sys
import time

import runspec as rs


def main_ping_host() -> None:
    spec = rs.parse("ping-host")
    host = str(spec.host)
    count = int(spec.count)

    try:
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
        print(json.dumps({"error": str(e), "host": host}))
        sys.exit(1)


def main_check_port() -> None:
    spec = rs.parse("check-port")
    host = str(spec.host)
    port = int(spec.port)
    timeout = float(spec.timeout)

    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            print(
                json.dumps(
                    {
                        "host": host,
                        "port": port,
                        "open": True,
                        "response_ms": elapsed_ms,
                    }
                )
            )
    except (OSError, TimeoutError):
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        print(json.dumps({"host": host, "port": port, "open": False, "response_ms": elapsed_ms}))


def main_show_connections() -> None:
    spec = rs.parse("show-connections")
    state_filter = str(spec.state)

    try:
        if shutil.which("ss"):
            cmd = ["ss", "-tupn"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            rows = _parse_ss(result.stdout, state_filter)
        elif shutil.which("netstat"):
            cmd = ["netstat", "-tupn"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            rows = _parse_netstat(result.stdout, state_filter)
        else:
            print(json.dumps({"error": "Neither ss nor netstat found in PATH"}))
            sys.exit(1)

        print(json.dumps(rows))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


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
