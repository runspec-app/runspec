"""
connect.py — `console-room connect`: a stdio↔TCP relay for the exec transport.

When a console can't open an SSH port-forward to the room — a hardened ``sshd``
(``AllowTcpForwarding no`` / ``PermitOpen``) or a network appliance that resets
tunnels — it instead runs ``console-room connect`` *on the host* over a normal SSH
**exec** channel (the same mechanism the console fleet already uses for commands).
This connects to the room server's local port on that host and pipes the NDJSON
stream over the exec channel's stdin/stdout. No forwarding is involved anywhere,
so there's nothing for a forwarding policy to deny or an appliance to reset.

Stdout carries *only* the relayed bytes — never log to it; diagnostics go to
stderr. See docs/design/console-room-server.md.
"""

from __future__ import annotations

import socket
import sys
import threading
from typing import BinaryIO


def relay(host: str, port: int, stdin: BinaryIO, stdout: BinaryIO) -> int:
    """Bridge ``stdin``/``stdout`` to a TCP socket at ``host:port`` until either
    side closes. Returns 0 on a clean close, 1 if the room port is unreachable.

    Uses ``read1`` when available so a chat frame relays as soon as it arrives
    (rather than waiting for a full buffer) — low latency for an interactive
    stream."""
    try:
        sock = socket.create_connection((host, port))
    except OSError as exc:
        sys.stderr.write(f"console-room connect: cannot reach {host}:{port}: {exc}\n")
        return 1

    read = getattr(stdin, "read1", None) or stdin.read

    def stdin_to_sock() -> None:
        try:
            while True:
                data = read(65536)
                if not data:
                    break
                sock.sendall(data)
        except Exception:
            pass
        finally:
            # Half-close the write side so the server sees our EOF and closes,
            # rather than tearing down the socket while a reply is still in flight.
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t_in = threading.Thread(target=stdin_to_sock, name="connect-in", daemon=True)
    t_in.start()
    # Pump socket → stdout on this thread until the server closes the connection
    # (the session's real end). The daemon stdin thread dies with the process.
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            stdout.write(data)
            stdout.flush()
    except Exception:
        pass
    try:
        sock.close()
    except OSError:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    """``console-room connect --host 127.0.0.1 --port 8765`` — relay stdio to the
    room server's local port."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="console-room connect",
        description="Relay stdin/stdout to the room server's local NDJSON port "
        "(the exec-channel transport — no SSH port-forward needed).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address the room server listens on, on this host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, required=True, help="the room server's NDJSON port"
    )
    args = parser.parse_args(argv)
    return relay(args.host, args.port, sys.stdin.buffer, sys.stdout.buffer)
