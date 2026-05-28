import json
import socket
import sys
import time

import runspec as rs


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


def main() -> None:
    spec = rs.parse("nc-command")
    host = str(spec.host)
    port = int(spec.port)
    command = str(spec.command)
    wait = float(spec.wait)
    read_timeout = float(spec.read_timeout)

    try:
        response = nc_send(host, port, command, wait=wait, read_timeout=read_timeout)
        lines = response.splitlines()
        print(
            json.dumps(
                {
                    "host": host,
                    "port": port,
                    "command": command,
                    "stdout": response,
                    "lines": lines,
                }
            )
        )
    except OSError as e:
        print(json.dumps({"error": str(e), "host": host, "port": port}))
        sys.exit(1)
