"""Plain-TCP send/receive helper (the ``nc-command`` core)."""

import socket
import time


def nc_send(
    host: str,
    port: int,
    command: str,
    wait: float = 0.3,
    read_timeout: float = 0.1,
) -> str:
    """Send a command over a plain TCP socket and return the response.

    Appends a newline to command if not present, waits `wait` seconds for the
    server to respond, then reads in chunks until the socket goes quiet.
    """
    with socket.create_connection((host, port), timeout=10) as sock:
        payload = command if command.endswith("\n") else command + "\n"
        sock.sendall(payload.encode())

        time.sleep(wait)

        sock.settimeout(read_timeout)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            except TimeoutError:
                break

    return b"".join(chunks).decode(errors="replace")
