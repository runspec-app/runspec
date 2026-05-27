"""flush-dns — flush the Windows DNS resolver cache."""

import logging
import subprocess
import sys

import runspec

logger = logging.getLogger(__name__)


def main() -> None:
    runspec.parse("flush-dns")   # validates args (none), sets up logging

    result = subprocess.run(
        ["ipconfig", "/flushdns"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in result.stdout.splitlines():
        if line.strip():
            logger.info("%s", line)
    if result.returncode != 0:
        for line in result.stderr.splitlines():
            if line.strip():
                logger.warning("%s", line)
        sys.exit(result.returncode)
