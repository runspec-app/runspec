"""ping-host — check network connectivity to a host."""

import subprocess
import sys

import runspec


def main() -> None:
    args = runspec.parse("ping-host")
    host: str = str(args.host.value)
    count: int = int(args.count.value)

    # Stream ping output live — each reply appears as it arrives
    proc = subprocess.Popen(
        ["ping", "-n", str(count), host],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    sys.exit(proc.wait())
