"""check-port — test whether a TCP port is open on a host."""

import logging
import socket
import sys

import runspec

logger = logging.getLogger(__name__)


def main() -> None:
    args = runspec.parse("check-port")
    host: str = str(args.host.value)
    port: int = int(args.port.value)
    timeout: float = float(args.timeout.value)

    logger.info("Checking %s:%d (timeout %.1fs) …", host, port, timeout)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            logger.info("✓  %s:%d is open", host, port)
    except (OSError, socket.timeout) as exc:
        logger.warning("✗  %s:%d is not reachable: %s", host, port, exc)
        sys.exit(1)
