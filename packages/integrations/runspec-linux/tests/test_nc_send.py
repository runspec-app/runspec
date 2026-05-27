"""Tests for nc_send — uses a real local TCP listener, no mocks."""

import socket
import threading

import pytest

from runspec_linux.nc_command import nc_send


def _echo_server(host: str, port: int, response: bytes, ready: threading.Event) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
        ready.set()
        srv.settimeout(5.0)
        try:
            conn, _ = srv.accept()
            with conn:
                conn.recv(1024)
                conn.sendall(response)
        except TimeoutError:
            pass


def _start_echo_server(response: bytes) -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    ready = threading.Event()
    t = threading.Thread(target=_echo_server, args=("127.0.0.1", port, response, ready), daemon=True)
    t.start()
    ready.wait(timeout=2.0)
    return port


def test_nc_send_returns_response() -> None:
    port = _start_echo_server(b"PONG\r\n")
    result = nc_send("127.0.0.1", port, "PING", wait=0.05, read_timeout=0.05)
    assert "PONG" in result


def test_nc_send_appends_newline() -> None:
    received: list[bytes] = []

    def capture_server(ready: threading.Event) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            port_inner = srv.getsockname()[1]
            srv.listen(1)
            ready.port = port_inner  # type: ignore[attr-defined]
            ready.set()
            srv.settimeout(5.0)
            try:
                conn, _ = srv.accept()
                with conn:
                    data = conn.recv(1024)
                    received.append(data)
                    conn.sendall(b"OK\n")
            except TimeoutError:
                pass

    ready = threading.Event()
    t = threading.Thread(target=capture_server, args=(ready,), daemon=True)
    t.start()
    ready.wait(timeout=2.0)
    port = ready.port  # type: ignore[attr-defined]

    nc_send("127.0.0.1", port, "CMD", wait=0.05, read_timeout=0.05)
    assert received and received[0].endswith(b"\n")


def test_nc_send_connection_refused_raises() -> None:
    with pytest.raises(OSError):
        nc_send("127.0.0.1", 1, "CMD", wait=0.0, read_timeout=0.05)


def test_nc_command_exported_from_package() -> None:
    from runspec_linux import nc_send as exported

    assert exported is nc_send
