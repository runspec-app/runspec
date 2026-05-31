"""
executor.py — run runnables as subprocesses, streaming stdout/stderr line by line.

For local hosts: run the binary directly from the venv Scripts dir.
For remote hosts: paramiko SSH channel, same streaming callback interface.

The caller supplies two callbacks:
  on_line(line, stream)       — called for each stdout/stderr line
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
    candidates = (
        [f"{runnable}.exe", runnable] if sys.platform == "win32" else [runnable]
    )
    binary = next(
        (bin_dir / c for c in candidates if (bin_dir / c).exists()), bin_dir / runnable
    )
    argv = args_to_argv(args)
    cmd = [str(binary), *command_path, *argv]
    _stream(
        cmd, on_line, on_done, timeout=timeout, cancel_event=cancel_event, agent=agent
    )


def _parse_ssh_target(target: str) -> tuple[str, str, int]:
    """Parse 'user@host:port' → (user, host, port). Missing parts → '' / 22."""
    user = ""
    port = 22
    s = target
    if "@" in s:
        user, s = s.split("@", 1)
    if ":" in s:
        host, port_str = s.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            host = s
    else:
        host = s
    return user, host, port


def _make_ssh_client(
    ssh_target: str,
    identity_file: str | None,
    global_ssh_config: dict[str, Any] | None = None,
) -> Any:
    """Create and connect a paramiko SSHClient.

    Falls back to global config for username and identity file when the host
    entry doesn't specify them.
    """
    import paramiko

    cfg = global_ssh_config or {}
    user, hostname, port = _parse_ssh_target(ssh_target)

    if not user:
        user = cfg.get("user", "")
    key_path = identity_file or cfg.get("identityFile", "")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict[str, Any] = {"hostname": hostname, "port": port, "timeout": 10}
    if user:
        connect_kwargs["username"] = user
    if key_path:
        expanded = str(Path(key_path).expanduser())
        connect_kwargs["key_filename"] = expanded

    client.connect(**connect_kwargs)
    return client


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
    global_ssh_config: dict[str, Any] | None = None,
    # Legacy kwarg kept so existing call sites don't break immediately
    ssh_binary: str = "",
) -> None:
    """Execute a remote runnable via paramiko SSH, streaming output via callbacks."""
    bin_dir = Path(runspec_path).parent.as_posix()
    remote_bin = f"{bin_dir}/{runnable}"
    argv = args_to_argv(args)
    parts = [remote_bin, *command_path, *argv]
    if agent:
        parts = ["RUNSPEC_AGENT=1", *parts]
    remote_cmd = " ".join(parts)

    start = time.monotonic()
    try:
        client = _make_ssh_client(ssh_target, identity_file, global_ssh_config)
    except Exception as exc:
        on_line(f"✗  SSH connection failed: {exc}", "stderr")
        on_done(-1, 0)
        return

    try:
        transport = client.get_transport()
        channel = transport.open_session()  # type: ignore[union-attr]
        channel.set_combine_stderr(False)
        channel.exec_command(remote_cmd)

        cancelled = threading.Event()
        done = threading.Event()

        def _watch_cancel() -> None:
            if cancel_event:
                cancel_event.wait()
                if not done.is_set():
                    cancelled.set()
                    channel.close()

        if cancel_event:
            threading.Thread(target=_watch_cancel, daemon=True).start()

        buf_out = b""
        buf_err = b""
        deadline = time.monotonic() + timeout if timeout else None

        while True:
            if deadline and time.monotonic() > deadline:
                channel.close()
                on_line(f"⏱  Process killed: exceeded {timeout}s timeout", "stderr")
                on_done(-1, int((time.monotonic() - start) * 1000))
                return

            if channel.recv_ready():
                buf_out += channel.recv(4096)
                while b"\n" in buf_out:
                    line, buf_out = buf_out.split(b"\n", 1)
                    on_line(line.decode("utf-8", errors="replace"), "stdout")

            if channel.recv_stderr_ready():
                buf_err += channel.recv_stderr(4096)
                while b"\n" in buf_err:
                    line, buf_err = buf_err.split(b"\n", 1)
                    on_line(line.decode("utf-8", errors="replace"), "stderr")

            if channel.exit_status_ready():
                # Drain remaining output
                while True:
                    chunk = channel.recv(4096)
                    if not chunk:
                        break
                    buf_out += chunk
                chunk_err = b""
                while True:
                    chunk_err = channel.recv_stderr(4096)
                    if not chunk_err:
                        break
                    buf_err += chunk_err
                for remainder, stream in ((buf_out, "stdout"), (buf_err, "stderr")):
                    for line in remainder.decode(
                        "utf-8", errors="replace"
                    ).splitlines():
                        on_line(line, stream)
                break

            time.sleep(0.01)

        done.set()
        if cancelled.is_set():
            on_line("✗  Cancelled", "stderr")
            on_done(-2, int((time.monotonic() - start) * 1000))
        else:
            on_done(channel.recv_exit_status(), int((time.monotonic() - start) * 1000))
    finally:
        client.close()


def ssh_run(
    ssh_target: str,
    command: str,
    identity_file: str | None = None,
    global_ssh_config: dict[str, Any] | None = None,
    timeout: int = 15,
) -> tuple[int, str, str]:
    """Run a single command over SSH, return (exit_code, stdout, stderr).

    Used for discovery, connectivity checks, and log fetching.
    """
    try:
        client = _make_ssh_client(ssh_target, identity_file, global_ssh_config)
    except Exception as exc:
        return -1, "", str(exc)

    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err
    except Exception as exc:
        return -1, "", str(exc)
    finally:
        client.close()


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
            stdin=subprocess.DEVNULL,
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

    drain_timeout = (timeout or 0) + 5 if timeout else None
    t_out.join(timeout=drain_timeout)
    t_err.join(timeout=drain_timeout)
    proc.wait()
    proc_done.set()

    if timer is not None:
        timer.cancel()

    duration_ms = int((time.monotonic() - start) * 1000)
    if user_cancelled.is_set():
        on_line("✗  Cancelled", "stderr")
        on_done(-2, duration_ms)
    elif timed_out.is_set():
        on_line(f"⏱  Process killed: exceeded {timeout}s timeout", "stderr")
        on_done(-1, duration_ms)
    else:
        on_done(proc.returncode, duration_ms)
