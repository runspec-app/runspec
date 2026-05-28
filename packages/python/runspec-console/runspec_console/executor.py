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
    timeout: int | None = None,
    cancel_event: threading.Event | None = None,
    agent: bool = False,
) -> None:
    """Execute a local runnable binary, streaming output via callbacks."""
    bin_dir = Path(runspec_path).parent
    # Windows entry points are installed as <name>.exe launchers
    candidates = ([f"{runnable}.exe", runnable] if sys.platform == "win32" else [runnable])
    binary = next((bin_dir / c for c in candidates if (bin_dir / c).exists()), bin_dir / runnable)
    argv = args_to_argv(args)
    cmd = [str(binary), *command_path, *argv]
    _stream(cmd, on_line, on_done, timeout=timeout, cancel_event=cancel_event, agent=agent)


def ssh_flags(identity_file: str | None, binary: str = "ssh") -> list[str]:
    """Common SSH/plink flags, with optional identity file.

    Detects plink by binary name and uses -batch instead of -o BatchMode=yes.
    """
    is_plink = "plink" in Path(binary).stem.lower()
    flags = ["-batch"] if is_plink else ["-o", "BatchMode=yes"]
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
    timeout: int | None = None,
    cancel_event: threading.Event | None = None,
    agent: bool = False,
    ssh_binary: str = "ssh",
) -> None:
    """Execute a remote runnable via SSH, streaming output via callbacks."""
    bin_dir = Path(runspec_path).parent.as_posix()
    remote_bin = f"{bin_dir}/{runnable}"
    argv = args_to_argv(args)
    cmd = [ssh_binary, *ssh_flags(identity_file, ssh_binary), ssh_target, remote_bin, *command_path, *argv]
    _stream(cmd, on_line, on_done, timeout=timeout, cancel_event=cancel_event, agent=agent)


def _stream(
    cmd: list[str],
    on_line: Callable[[str, str], None],
    on_done: Callable[[int, int], None],
    timeout: int | None = None,
    cancel_event: threading.Event | None = None,
    agent: bool = False,
) -> None:
    env = {**_UTF8_ENV, "RUNSPEC_AGENT": "1"} if agent else _UTF8_ENV
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,   # never block on stdin reads
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
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

    timed_out = threading.Event()
    user_cancelled = threading.Event()
    proc_done = threading.Event()

    def _kill_timeout() -> None:
        timed_out.set()
        try:
            proc.kill()
        except Exception:
            pass

    def _watch_cancel() -> None:
        cancel_event.wait()  # type: ignore[union-attr]
        if not proc_done.is_set():
            user_cancelled.set()
            try:
                proc.kill()
            except Exception:
                pass

    timer = threading.Timer(timeout, _kill_timeout) if timeout is not None else None
    if timer:
        timer.start()
    if cancel_event is not None:
        threading.Thread(target=_watch_cancel, daemon=True).start()

    # After a kill() the pipes close and reader threads exit naturally;
    # give 5 s grace to drain after a kill.
    drain_timeout = (timeout or 0) + 5 if timeout else None
    t_out.join(timeout=drain_timeout)
    t_err.join(timeout=drain_timeout)
    proc.wait()
    proc_done.set()

    if timer i