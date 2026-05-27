"""
executor.py — run runnables as subprocesses, streaming stdout/stderr line by line.

For local hosts: run the binary directly from the venv Scripts dir.
For remote hosts: ssh <target> <remote_bin_path> [args...]

The caller supplies two callbacks:
  on_line(line, stream)  — called for each stdout/stderr line
  on_done(exit_code, duration_ms) — called once when the process exits
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

# Ensure child Python processes use UTF-8 for print()/sys.stdout
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def args_to_argv(args: dict[str, Any]) -> list[str]:
    """Convert a {name: value} args dict to a CLI argv list."""
    argv: list[str] = []
    for k, v in args.items():
        if v is None:
            continue
        if isinstance(v, bool):
            if v:
                argv.append(f"--{k}")
        elif isinstance(v, list):
            for item in v:
                argv.extend([f"--{k}", str(item)])
        else:
            argv.extend([f"--{k}", str(v)])
    return argv


def run_local(
    runspec_path: str,
    runnable: str,
    args: dict[str, Any],
    command_path: list[str],
    on_line: Callable[[str, str], None],
    on_done: Callable[[int, int], None],
) -> None:
    """Execute a local runnable binary, streaming output via callbacks."""
    bin_dir = Path(runspec_path).parent
    # Windows entry points are installed as <name>.exe launchers
    candidates = ([f"{runnable}.exe", runnable] if sys.platform == "win32" else [runnable])
    binary = next((bin_dir / c for c in candidates if (bin_dir / c).exists()), bin_dir / runnable)
    argv = args_to_argv(args)
    cmd = [str(binary), *command_path, *argv]
    _stream(cmd, on_line, on_done)


def ssh_flags(identity_file: str | None) -> list[str]:
    """Common SSH flags, with optional identity file."""
    flags = ["-o", "BatchMode=yes"]
    if identity_file:
        flags += ["-i", str(Path(identity_file).expanduser())]
    return flags


def run_remote(
    ssh_target: str,
    runspec_path: str,
    runnable: str,
    args: dict[str, Any],
    command_path: list[str],
    on_line: Callable[[str, str], None],
    on_done: Callable[[int, int], None],
    identity_file: str | None = None,
) -> None:
    """Execute a remote runnable via SSH, streaming output via callbacks."""
    bin_dir = Path(runspec_path).parent.as_posix()
    remote_bin = f"{bin_dir}/{runnable}"
    argv = args_to_argv(args)
    cmd = ["ssh", *ssh_flags(identity_file), ssh_target, remote_bin, *command_path, *argv]
    _stream(cmd, on_line, on_done)


def _stream(
    cmd: list[str],
    on_line: Callable[[str, str], None],
    on_done: Callable[[int, int], None],
) -> None:
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_UTF8_ENV,
        )
    except FileNotFoundError:
        on_line(f"✗  Command not found: {cmd[0]}", "stderr")
        on_done(-1, 0)
        return

    def _read(stream: Any, name: str) -> None:
        for line in stream:
            on_line(line.rstrip("\n"), name)

    t_out = threading.Thread(target=_read, args=(proc.stdout, "stdout"), daemon=True)
    t_err = threading.Thread(target=_read, args=(proc.stderr, "stderr"), daemon=True)
    t_out.start()
    t_err.start()
    t_out.join()
    t_err.join()
    proc.wait()

    duration_ms = int((time.monotonic() - start) * 1000)
    on_done(proc.returncode, duration_ms)
