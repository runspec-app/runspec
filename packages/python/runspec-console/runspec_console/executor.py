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
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

# Ensure child Python processes use UTF-8 for print()/sys.stdout
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

# SSH connect-phase timeouts (seconds). The TCP connect uses ``timeout``;
# ``banner_timeout`` covers the wait for the server's SSH protocol banner, and
# ``auth_timeout`` the authentication exchange. paramiko's defaults for the
# latter two (15s) are often too tight for slow servers, reverse-DNS-on-connect
# (sshd UseDNS), or a server throttling new connections (sshd MaxStartups) — the
# common cause of "Error reading SSH protocol banner". All three are overridable
# per deployment via ``[ssh]`` in config.toml.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_BANNER_TIMEOUT = 30.0
DEFAULT_AUTH_TIMEOUT = 30.0

# SSH host-key verification mode, mirroring OpenSSH's StrictHostKeyChecking.
# Default is "accept-new" (trust-on-first-use): a never-before-seen host is
# accepted and remembered, but a *changed* key for a known host is rejected —
# so a later man-in-the-middle is caught. This replaces blind AutoAddPolicy
# (accept any key, every time), which disables host-key verification entirely.
# Overridable via ``[ssh] host_key_checking`` in config.toml:
#   "accept-new"           — TOFU; remember new hosts, reject changed keys (default)
#   "yes"/"true"/"strict"  — only connect to hosts already in known_hosts
# There is deliberately no "accept anything, including changed keys" mode — that
# is the man-in-the-middle hole this change closes, and accept-new already covers
# unattended first-contact trust.
DEFAULT_HOST_KEY_CHECKING = "accept-new"
_HOST_KEY_STRICT = {"yes", "true", "strict", "on", "1"}


def _known_hosts_path(cfg: dict[str, Any]) -> Path:
    """App-managed known_hosts file for persisting accept-new keys.

    Overridable via ``[ssh] known_hosts``. Defaults under the app-data dir so
    we never rewrite the user's ``~/.ssh/known_hosts`` (which is still read for
    verification via ``load_system_host_keys``). Computed without creating any
    directories — the accept-new policy makes the parent lazily, only when it
    actually records a key.
    """
    override = cfg.get("known_hosts")
    if override:
        return Path(str(override)).expanduser()
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "runspec-console" / "known_hosts"


def _accept_new_policy(known_hosts: Path) -> Any:
    """Build a paramiko missing-host-key policy implementing accept-new (TOFU).

    Defined lazily so importing this module doesn't pull in paramiko. A changed
    key for an *already known* host never reaches this policy — paramiko raises
    ``BadHostKeyException`` before calling it — so this only ever fires for a
    genuinely new host, which it records and persists to the app-managed file.
    """
    import paramiko

    class _AcceptNewPolicy(paramiko.MissingHostKeyPolicy):  # type: ignore[misc]
        def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
            client._host_keys.add(hostname, key.get_name(), key)
            try:
                known_hosts.parent.mkdir(parents=True, exist_ok=True)
                client.save_host_keys(str(known_hosts))
            except OSError:
                # Persisting failed (e.g. read-only home) — the in-memory trust
                # still holds for this session; we just won't remember it.
                pass

    return _AcceptNewPolicy()


def _apply_host_key_policy(client: Any, cfg: dict[str, Any]) -> None:
    """Configure host-key verification on a fresh SSHClient per ``cfg``."""
    import paramiko

    mode = str(cfg.get("host_key_checking", DEFAULT_HOST_KEY_CHECKING)).strip().lower()

    # Load known hosts so paramiko can detect a changed key (MITM) for any host
    # we've seen before — both the user's ~/.ssh/known_hosts (read-only) and our
    # app-managed file feed that check.
    try:
        client.load_system_host_keys()
    except Exception:
        pass
    known_hosts = _known_hosts_path(cfg)
    if known_hosts.is_file():
        try:
            client.load_host_keys(str(known_hosts))
        except OSError:
            pass

    if mode in _HOST_KEY_STRICT:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:  # "accept-new" and any unrecognised value → safe TOFU default
        client.set_missing_host_key_policy(_accept_new_policy(known_hosts))


def friendly_ssh_error(exc: BaseException | str) -> str:
    """Map a low-level SSH connect failure to a clear, actionable one-liner.

    The corporate failure mode (issue #104) is a proxy / TLS middlebox / load
    balancer answering the SSH port with non-SSH bytes — paramiko surfaces that
    as a ``UnicodeDecodeError`` wrapped in "Error reading SSH protocol banner",
    an opaque message for an operator. Translate the common cases.
    """
    msg = str(exc).strip()
    low = msg.lower()
    if "banner" in low and (
        "codec can't decode" in low or "invalid start byte" in low or "utf-8" in low
    ):
        return (
            f"{msg}\n   ↳ the remote sent non-SSH data on connect — a proxy, TLS "
            "middlebox, or load balancer may be intercepting the connection, or "
            "the port is not an SSH server."
        )
    if "banner" in low:
        return (
            f"{msg}\n   ↳ no SSH banner before the timeout — the server may be "
            "slow, behind a proxy, or throttling new connections (sshd "
            "MaxStartups). Raising [ssh] banner_timeout can help."
        )
    return msg


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
    run_as: str = "",
    become_method: str = "sudo",
    become_flags: str | None = None,
) -> None:
    """Execute a local runnable binary, streaming output via callbacks."""
    bin_dir = Path(runspec_path).parent
    # On Windows a Node folder (`runspec bin`) ships .cmd shims alongside the
    # POSIX ones; a Python venv has .exe entry points. Prefer .exe, then .cmd/.bat.
    candidates = (
        [f"{runnable}.exe", f"{runnable}.cmd", f"{runnable}.bat", runnable]
        if sys.platform == "win32"
        else [runnable]
    )
    binary = next(
        (bin_dir / c for c in candidates if (bin_dir / c).exists()), bin_dir / runnable
    )
    argv = args_to_argv(args)
    cmd = [str(binary), *command_path, *argv]
    # A .cmd/.bat shim isn't directly executable by CreateProcess — run it
    # through the command interpreter so the Node-folder shims work on Windows.
    if sys.platform == "win32" and binary.suffix.lower() in (".cmd", ".bat"):
        cmd = ["cmd", "/c", *cmd]
    if run_as:
        # sudo/su strip the environment — carry RUNSPEC_AGENT through env(1).
        from runspec.become import build_become_argv

        env = {"RUNSPEC_AGENT": "1"} if agent else {}
        cmd = build_become_argv(cmd, run_as, become_method, become_flags, env=env)
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


def _http_connect_sock(
    proxy: str, host: str, port: int, timeout: float = 10.0
) -> socket.socket:
    """Open a socket to ``host:port`` tunnelled through an HTTP CONNECT proxy.

    ``proxy`` is like ``"http://proxy.corp:8080"`` (scheme optional). No proxy
    authentication is sent — for an auth-required proxy, front it with a local
    auth bridge (cntlm/px) and point this at ``http://localhost:<port>``.

    The returned socket is connected straight through to the target and is
    suitable as paramiko's ``sock=`` argument.
    """
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    phost = parsed.hostname
    pport = parsed.port or 8080
    if not phost:
        raise ValueError(f"Invalid proxy URL: {proxy!r}")

    sock = socket.create_connection((phost, pport), timeout=timeout)
    try:
        request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
        sock.sendall(request.encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp += chunk
        status_line = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        fields = status_line.split(None, 2)
        if len(fields) < 2 or fields[1] != "200":
            raise OSError(
                f"HTTP proxy CONNECT to {host}:{port} failed: {status_line!r}"
            )
    except Exception:
        sock.close()
        raise
    sock.settimeout(None)
    return sock


def _make_ssh_client(
    ssh_target: str,
    identity_file: str | None,
    global_ssh_config: dict[str, Any] | None = None,
) -> Any:
    """Create and connect a paramiko SSHClient.

    Falls back to global config for username and identity file when the host
    entry doesn't specify them. Optional ``[ssh]`` settings shape the
    connection:

      proxy             — HTTP CONNECT proxy URL; the SSH transport is tunnelled
                          through it (corporate egress).
      use_ssh_config    — when true, ~/.ssh/config is consulted for HostName,
                          User, Port, IdentityFile, and ProxyCommand (the latter
                          only when no explicit ``proxy`` is set).
      host_key_checking — host-key verification mode (see DEFAULT_HOST_KEY_CHECKING):
                          "accept-new" (default, TOFU) or "yes" (strict).
      known_hosts       — path to the app-managed known_hosts file used for
                          accept-new persistence (default under the app-data dir).

    Precedence: per-host/global explicit fields > ~/.ssh/config. An explicit
    ``proxy`` takes precedence over a ProxyCommand from ~/.ssh/config.
    """
    import paramiko

    cfg = global_ssh_config or {}
    user, hostname, port = _parse_ssh_target(ssh_target)

    if not user:
        user = cfg.get("user", "")
    key_path = identity_file or cfg.get("identityFile", "")
    proxy = cfg.get("proxy") or ""
    sock: Any = None

    # ~/.ssh/config (opt-in) fills in anything not set explicitly.
    if cfg.get("use_ssh_config"):
        ssh_config_path = Path("~/.ssh/config").expanduser()
        if ssh_config_path.is_file():
            ssh_conf = paramiko.SSHConfig()
            with open(ssh_config_path) as fh:
                ssh_conf.parse(fh)
            host_conf = ssh_conf.lookup(hostname)
            hostname = host_conf.get("hostname", hostname)
            if not user and host_conf.get("user"):
                user = host_conf["user"]
            if port == 22 and host_conf.get("port"):
                port = int(host_conf["port"])
            if not key_path and host_conf.get("identityfile"):
                ids = host_conf["identityfile"]
                key_path = ids[0] if isinstance(ids, list) else ids
            if not proxy and host_conf.get("proxycommand"):
                sock = paramiko.ProxyCommand(host_conf["proxycommand"])

    # An explicit HTTP proxy wins over a ProxyCommand from ssh_config.
    if proxy:
        sock = _http_connect_sock(proxy, hostname, port)

    client = paramiko.SSHClient()
    _apply_host_key_policy(client, cfg)

    connect_kwargs: dict[str, Any] = {
        "hostname": hostname,
        "port": port,
        "timeout": float(cfg.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)),
        "banner_timeout": float(cfg.get("banner_timeout", DEFAULT_BANNER_TIMEOUT)),
        "auth_timeout": float(cfg.get("auth_timeout", DEFAULT_AUTH_TIMEOUT)),
    }
    if user:
        connect_kwargs["username"] = user
    if key_path:
        connect_kwargs["key_filename"] = str(Path(key_path).expanduser())
    if sock is not None:
        connect_kwargs["sock"] = sock

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
    run_as: str = "",
    become_method: str = "sudo",
    become_flags: str | None = None,
    session_factory: Callable[[], tuple[Any, Callable[[], None]]] | None = None,
) -> None:
    """Execute a remote runnable via paramiko SSH, streaming output via callbacks.

    ``session_factory``, when supplied, returns ``(channel, release)`` from a
    pooled transport — the connection is reused, only the channel is opened here,
    and ``release()`` (not a transport close) frees the channel slot afterward.
    Without it, a fresh client is connected and closed per invocation (legacy).
    """
    from runspec.become import build_become_argv

    bin_dir = Path(runspec_path).parent.as_posix()
    remote_bin = f"{bin_dir}/{runnable}"
    argv = args_to_argv(args)
    # build_become_argv handles both the plain (RUNSPEC_AGENT=1 cmd) and the
    # escalated (sudo -u user env RUNSPEC_AGENT=1 cmd) forms.
    env = {"RUNSPEC_AGENT": "1"} if agent else {}
    parts = build_become_argv(
        [remote_bin, *command_path, *argv],
        run_as,
        become_method,
        become_flags,
        env=env,
    )
    remote_cmd = " ".join(parts)

    start = time.monotonic()
    client = None  # set only on the legacy (non-pooled) path; closed in finally
    release: Callable[[], None] | None = None
    try:
        if session_factory is not None:
            channel, release = session_factory()
        else:
            client = _make_ssh_client(ssh_target, identity_file, global_ssh_config)
            channel = client.get_transport().open_session()  # type: ignore[union-attr]
    except Exception as exc:
        on_line(f"✗  SSH connection failed: {friendly_ssh_error(exc)}", "stderr")
        on_done(-1, 0)
        if release is not None:
            release()
        if client is not None:
            client.close()
        return

    try:
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
        # Always close the channel (frees the server-side session). On the pooled
        # path, release() returns the channel slot but keeps the transport warm;
        # on the legacy path, close the whole client.
        try:
            channel.close()
        except Exception:
            pass
        if release is not None:
            release()
        if client is not None:
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
