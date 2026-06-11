"""
test_connect.py — the `console-room connect` stdio↔TCP relay (exec transport).

Drives relay() with BytesIO stdin/stdout against a real loopback echo server, so
the bridging is exercised end to end without SSH.
"""

from __future__ import annotations

import io
import socket
import threading

from console_room.connect import relay


def _echo_server() -> tuple[int, threading.Thread]:
    """A one-shot loopback server that echoes everything it receives back."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run() -> None:
        conn, _ = srv.accept()
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return port, t


def test_relay_pipes_stdin_to_socket_and_back():
    port, server = _echo_server()
    stdin = io.BytesIO(b'{"type":"hello","room":"ops"}\n')
    stdout = io.BytesIO()

    rc = relay("127.0.0.1", port, stdin, stdout)

    assert rc == 0
    # stdin reached the server and the echo came back to stdout.
    assert stdout.getvalue() == b'{"type":"hello","room":"ops"}\n'
    server.join(timeout=2)


def test_relay_reports_unreachable_port():
    stdin = io.BytesIO(b"")
    stdout = io.BytesIO()
    # Nothing listening on this port → clean failure, exit code 1.
    rc = relay("127.0.0.1", 1, stdin, stdout)
    assert rc == 1
    assert stdout.getvalue() == b""
