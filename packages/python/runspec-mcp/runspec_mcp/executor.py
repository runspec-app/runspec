"""
executor.py — run runnables as subprocesses, streaming stdout/stderr line by line.

For local hosts: run the binary directly from the venv Scripts dir.
For remote hosts: paramiko SSH channel, same streaming callback interface.

The caller supplies two callbacks:
  on_line(line, stream)       — called for each stdout/stderr line
  on_done(exit_code, duration_ms) — called once when the process exits
"""

from __future__ import annotations

import json

import os
import shlex
import shutil
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

# TCP socket keepalive on the SSH transport's underlying socket. This is a
# *complement* to paramiko's application-layer keepalive (``set_keepalive`` in
# ssh_pool — ``[ssh] keepalive``): that one sends ``keepalive@openssh.com`` over
# the SSH connection; these set ``SO_KEEPALIVE`` + the OS probe schedule on the
# raw socket, so the kernel also reaps a silently-dead peer (NAT/VPN/firewall
# idle-drop, half-open after a peer reboot) and keeps stateful middleboxes from
# evicting an idle mapping. Defaults are deliberately conservative; the SSH
# keepalive (30s) usually fires first, so these are a backstop. Overridable via
# ``[ssh]``: ``tcp_keepalive`` (off switch), ``tcp_keepidle`` / ``tcp_keepintvl``
# / ``tcp_keepcnt``. The idle/intvl/cnt knobs only take effect where the OS
# exposes them (Linux/macOS via setsockopt; Windows via the SIO_KEEPALIVE_VALS
# ioctl — count is fixed by the OS there); ``SO_KEEPALIVE`` is set everywhere.
DEFAULT_TCP_KEEPIDLE = 60  # s idle before the first keepalive probe
DEFAULT_TCP_KEEPINTVL = 15  # s between probes
DEFAULT_TCP_KEEPCNT = 3  # unanswered probes before the socket is declared dead


def _apply_socket_keepalive(transport: Any, cfg: dict[str, Any]) -> None:
    """Enable TCP keepalive on ``transport``'s underlying socket per ``[ssh]``.

    Best-effort and never raises — keepalive is a resilience nicety, not a
    correctness requirement, so a platform that lacks a given option simply
    skips it. ``cfg`` is the ``[ssh]`` config dict; ``tcp_keepalive = false``
    disables the whole thing (``SO_KEEPALIVE`` is left untouched).
    """
    if cfg.get("tcp_keepalive", True) is False:
        return
    sock = getattr(transport, "sock", None)
    if sock is None:
        return

    def _int(key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    idle = _int("tcp_keepidle", DEFAULT_TCP_KEEPIDLE)
    intvl = _int("tcp_keepintvl", DEFAULT_TCP_KEEPINTVL)
    cnt = _int("tcp_keepcnt", DEFAULT_TCP_KEEPCNT)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    # Per-platform probe schedule. The idle-time constant is ``TCP_KEEPIDLE`` on
    # Linux and ``TCP_KEEPALIVE`` on macOS; ``TCP_KEEPINTVL`` / ``TCP_KEEPCNT``
    # exist on both but not on Windows. Guard each with hasattr so an absent
    # option is skipped rather than crashing the connect.
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None)
    if idle_opt is None:
        idle_opt = getattr(socket, "TCP_KEEPALIVE", None)  # macOS spelling
    for opt, value in (
        (idle_opt, idle),
        (getattr(socket, "TCP_KEEPINTVL", None), intvl),
        (getattr(socket, "TCP_KEEPCNT", None), cnt),
    ):
        if opt is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:
            pass
    # Windows exposes no per-option constants; the SIO_KEEPALIVE_VALS ioctl sets
    # idle + interval together (probe count is OS-fixed). Best-effort.
    if idle_opt is None and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, intvl * 1000))
        except OSError:
            pass


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
    if "banner" in low and ("codec can't decode" in low or "invalid start byte" in low or "utf-8" in low):
        return f"{msg}\n   ↳ the remote sent non-SSH data on connect — a proxy, TLS middlebox, or load balancer may be intercepting the connection, or the port is not an SSH server."
    if "banner" in low:
        return f"{msg}\n   ↳ no SSH banner before the timeout — the server may be slow, behind a proxy, or throttling new connections (sshd MaxStartups). Raising [ssh] banner_timeout can help."
    return msg


# Substrings identifying an SSH failure that is *transient* — a load/throttle
# artefact of the moment (bastion ``MaxStartups``, a banner that arrived slowly,
# a transport torn down mid-auth) rather than a durable, host-specific fault
# (wrong key, auth refused, no route). A transient failure is worth one or two
# quick retries; a durable one is not (retrying just wastes handshakes). The
# non-UTF-8 banner case ("codec can't decode") is deliberately excluded — that
# is a proxy/middlebox answering the port, which a retry won't clear.
_TRANSIENT_SSH_MARKERS = (
    "error reading ssh protocol banner",
    "no existing session",
    "session not active",
    "ssh session not active",
    "connection reset",
    "reset by peer",
    "timed out",
    "timeout",
    "connection closed",
    "eof during negotiation",
    "administratively prohibited",
    "channel closed",
)


def is_transient_ssh_error(exc: BaseException | str) -> bool:
    """True if ``exc`` looks like a transient SSH connect/exec failure.

    Used to decide whether a failed handshake or command is worth retrying —
    the same class of error (``MaxStartups`` throttle, slow banner, a transport
    dropped mid-auth under a connection burst) that shows up on a *different*
    small subset of hosts on every fleet-wide pass and clears on a quick retry.
    A non-UTF-8 banner (proxy/middlebox) and auth failures are *not* transient.
    """
    low = str(exc).lower()
    # A non-UTF-8 banner is a proxy/middlebox, not a throttle — don't retry it.
    if "banner" in low and ("codec can't decode" in low or "invalid start byte" in low):
        return False
    return any(marker in low for marker in _TRANSIENT_SSH_MARKERS)


def shell_join(parts: list[str]) -> str:
    """Join an argv token list into a single shell command line, quoting each
    token so a user-supplied arg value cannot inject shell syntax.

    ``run_remote`` runs the command through the remote login shell
    (``exec_command`` → ``/bin/sh -c``), so an unquoted value containing ``;``,
    ``|``, ``$(...)``, backticks, ``&&`` or whitespace would be interpreted by
    the shell — a command-injection hole, especially under ``sudo -u``. Quoting
    every token makes each one a single literal argument. (Local execution never
    hits a shell — it execs the argv list directly — so this is the only path
    that needs it.)
    """
    return " ".join(shlex.quote(p) for p in parts)


def remote_exec_command(
    cmd_argv: list[str],
    run_as: str = "",
    become_method: str = "sudo",
    become_flags: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, bytes | None]:
    """The shell command line for an SSH exec channel, and the stdin to feed it.

    Per the SPEC "Command building" table: with ``env`` the command is wrapped in
    the env prelude (inside the ``run_as`` escalation) and the values travel as
    the env frame on the channel's stdin — never in the command line, which every
    user on the host can read (``ps``) and ``sudo`` logs. Without ``env`` the
    frame is ``None``. Every token is quoted (:func:`shell_join`)."""
    from runspec.become import build_exec_argv

    parts, frame = build_exec_argv(cmd_argv, run_as, become_method, become_flags, env=env or None)
    return shell_join(parts), frame


def _argv_value(v: Any) -> str:
    """One argv token for an arg value. A dict (or a nested list) is sent as
    JSON: an agent often passes a JSON-object arg like ``values`` as a real
    object rather than a JSON string, and ``str()`` would send Python's repr
    (single quotes, ``True``), which the runnable's JSON parse rejects."""
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def args_to_argv(args: dict[str, Any], positionals: dict[str, int] | None = None) -> list[str]:
    """Convert a {name: value} args dict to a CLI argv list.

    Args named in ``positionals`` ({name: position}) are emitted as bare values
    in position order, *after* the flags — core's parser only accepts a
    positional by position, never as ``--name`` (it would reject the flag as an
    unknown argument). Everything else is a flag: ``--name value`` (``--name``
    for a true flag; repeated per item for a list).
    """
    positionals = positionals or {}
    flags: list[str] = []
    positional_vals: list[tuple[int, str]] = []
    for k, v in args.items():
        if v is None:
            continue
        # Skip blank strings (e.g. an optional inherited field left empty in the
        # form) so we don't emit `--flag ""`, which would fail a pattern check.
        if isinstance(v, str) and v == "":
            continue
        if k in positionals:
            items = v if isinstance(v, list) else [v]
            positional_vals.extend((positionals[k], _argv_value(item)) for item in items)
        elif isinstance(v, bool):
            if v:
                flags.append(f"--{k}")
        elif isinstance(v, list):
            for item in v:
                flags.extend([f"--{k}", _argv_value(item)])
        else:
            flags.extend([f"--{k}", _argv_value(v)])
    positional_vals.sort(key=lambda p: p[0])
    return flags + [val for _, val in positional_vals]


def secret_env(runnable: str, secrets: dict[str, Any] | None) -> dict[str, str]:
    """Map ``password``-typed form values to their ``RUNSPEC_*_ARG_*`` env vars.

    Secrets never go into argv (that would leak into shell history / ``ps``).
    Instead they ride the same environment channel core uses to resolve them:
    ``RUNSPEC_<RUNNABLE>_ARG_<NAME>`` — the runnable name is the top-level name
    even for subcommand args, matching core's parser._apply_env. Blank values are
    dropped so an empty optional field doesn't shadow a real env value.
    """
    if not secrets:
        return {}
    prefix = runnable.upper().replace("-", "_")
    out: dict[str, str] = {}
    for name, value in secrets.items():
        if value is None or value == "":
            continue
        key = f"RUNSPEC_{prefix}_ARG_{name.upper().replace('-', '_')}"
        out[key] = str(value)
    return out


# Windows caps a process command line at 32,767 characters (CreateProcess), and
# cmd.exe — the interpreter a .cmd/.bat shim runs under — at 8,191. A longer line
# fails with WinError 206, which Python reports as FileNotFoundError, so it used
# to surface as a misleading "Command not found" for a binary that exists.
WIN_CMDLINE_MAX = 32_767
WIN_CMD_SHIM_MAX = 8_191


def fit_command_line(
    runnable: str,
    prefix: list[str],
    args: dict[str, Any],
    positionals: dict[str, int] | None = None,
    budget: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    """``(argv, env)`` for ``args`` such that ``prefix + argv`` fits ``budget``
    characters (as Windows measures it, via ``subprocess.list2cmdline``).

    When the line is too long, the longest scalar values move off argv onto the
    ``RUNSPEC_<RUNNABLE>_ARG_<NAME>`` env channel core reads for every arg (cli →
    env → default), one at a time until the line fits. Flags and lists stay on
    argv (a list's env form is newline-split); a positional moves only when it is
    the last one still on argv, so the rest keep their positions. ``budget=None``
    → no limit, the plain ``args_to_argv`` result and no env."""
    positionals = positionals or {}
    remaining = dict(args)
    env: dict[str, str] = {}

    def too_long() -> bool:
        line = subprocess.list2cmdline([*prefix, *args_to_argv(remaining, positionals)])
        return budget is not None and len(line) > budget

    while too_long():
        present = [k for k in remaining if k in positionals and remaining[k] not in (None, "")]
        last_pos = max((positionals[k] for k in present), default=None)
        movable = [k for k, v in remaining.items() if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v) != "" and (k not in positionals or positionals[k] == last_pos)]
        if not movable:
            break
        name = max(movable, key=lambda k: len(str(remaining[k])))
        env.update(secret_env(runnable, {name: remaining.pop(name)}))
    return args_to_argv(remaining, positionals), env


def start_error(cmd: list[str], exc: OSError) -> str:
    """The line shown when ``cmd`` can't be started. "Command not found" only
    when the program really is missing; otherwise the OS error itself, plus the
    command-line length when Windows refused it as too long."""
    program = cmd[0] if cmd else ""
    if isinstance(exc, FileNotFoundError) and getattr(exc, "winerror", None) != 206 and not Path(program).exists() and shutil.which(program) is None:
        return f"✗  Command not found: {program}"
    msg = f"✗  Could not start {program}: {exc}"
    length = len(subprocess.list2cmdline(cmd))
    if getattr(exc, "winerror", None) == 206 or (sys.platform == "win32" and length > WIN_CMDLINE_MAX):
        msg += f" — the command line is {length:,} characters; Windows allows at most {WIN_CMDLINE_MAX:,}"
    return msg


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
    positionals: dict[str, int] | None = None,
    secrets: dict[str, Any] | None = None,
    cred_env: dict[str, str] | None = None,
    trigger: str = "",
    runbook: str = "",
) -> None:
    """Execute a local runnable binary, streaming output via callbacks.

    ``cred_env`` are plain environment variables derived from named credentials
    (e.g. ``JIRA_USERNAME``/``JIRA_PASSWORD``) — final var names, unlike the
    ``RUNSPEC_*_ARG_*`` channel ``secrets`` ride. They are merged into the child
    environment the same way secrets are (and carried across the ``run_as``
    escalation as the SPEC env frame on stdin)."""
    bin_dir = Path(runspec_path).parent
    # On Windows a Node folder (`runspec bin`) ships .cmd shims alongside the
    # POSIX ones; a Python venv has .exe entry points. Prefer .exe, then .cmd/.bat.
    candidates = [f"{runnable}.exe", f"{runnable}.cmd", f"{runnable}.bat", runnable] if sys.platform == "win32" else [runnable]
    binary = next((bin_dir / c for c in candidates if (bin_dir / c).exists()), bin_dir / runnable)
    # A .cmd/.bat shim isn't directly executable by CreateProcess — run it
    # through the command interpreter so the Node-folder shims work on Windows.
    shim = sys.platform == "win32" and binary.suffix.lower() in (".cmd", ".bat")
    prefix = (["cmd", "/c"] if shim else []) + [str(binary), *command_path]
    # On Windows a long value (e.g. a whole document body) can push the command
    # line past the OS limit — move the longest values onto the env channel.
    budget = (WIN_CMD_SHIM_MAX if shim else WIN_CMDLINE_MAX) - 64 if sys.platform == "win32" else None
    argv, long_env = fit_command_line(runnable, prefix, args, positionals, budget)
    cmd = [*prefix, *argv]
    secrets_env = {**long_env, **secret_env(runnable, secrets)}
    cred_env = cred_env or {}
    # Provenance of what caused this run → recorded in the runnable's run_summary
    # (extra.trigger = event source, extra.runbook = the saved runbook that drove it).
    # Both orthogonal to RUNSPEC_AGENT.
    prov_env = {"RUNSPEC_TRIGGER": trigger} if trigger else {}
    if runbook:
        prov_env["RUNSPEC_RUNBOOK"] = runbook
    extra_env: dict[str, str] = {}
    frame: bytes | None = None
    if run_as:
        # sudo/su strip the environment — carry RUNSPEC_AGENT and any secret/cred
        # env vars across as the SPEC env frame on the child's stdin, never on the
        # command line (visible to every user in ps, and logged by sudo).
        from runspec.become import build_exec_argv

        env = {"RUNSPEC_AGENT": "1"} if agent else {}
        env.update(prov_env)
        env.update(secrets_env)
        env.update(cred_env)
        cmd, frame = build_exec_argv(cmd, run_as, become_method, become_flags, env=env or None)
    else:
        # No escalation — secrets/cred env vars go straight into the child process.
        extra_env = {**secrets_env, **cred_env, **prov_env}
    _stream(
        cmd,
        on_line,
        on_done,
        timeout=timeout,
        cancel_event=cancel_event,
        agent=agent,
        extra_env=extra_env,
        stdin_data=frame,
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


def _http_connect_sock(proxy: str, host: str, port: int, timeout: float = 10.0) -> socket.socket:
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
            raise OSError(f"HTTP proxy CONNECT to {host}:{port} failed: {status_line!r}")
    except Exception:
        sock.close()
        raise
    sock.settimeout(None)
    return sock


def _make_ssh_client(
    ssh_target: str,
    identity_file: str | None,
    global_ssh_config: dict[str, Any] | None = None,
    password: str | None = None,
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

    ``password``, when given, switches to password auth (e.g. the ssh-copy-id
    bootstrap): the password alone is used and agent/on-disk-key fallback is
    disabled, so the password is what's actually tested — while still going
    through the same proxy / host-key / ssh_config handling as key auth.
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
    if password:
        # Password auth: use the password alone, no agent/on-disk-key fallback,
        # so a wrong password fails cleanly instead of silently succeeding via
        # some unrelated key (and no key gets installed).
        connect_kwargs["password"] = password
        connect_kwargs["allow_agent"] = False
        connect_kwargs["look_for_keys"] = False
    elif key_path:
        connect_kwargs["key_filename"] = str(Path(key_path).expanduser())
    if sock is not None:
        connect_kwargs["sock"] = sock

    client.connect(**connect_kwargs)

    # Tag this transport so steady-state paramiko records (idle-disconnect, EOF,
    # keepalive failures) carry the host: ``set_log_channel`` makes the
    # transport log under ``paramiko.transport.<host>`` instead of the shared
    # ``paramiko.transport``, and the Dev-tab handler parses the host back out.
    # Also turn on socket-level TCP keepalive as a backstop to the SSH keepalive.
    # This is all diagnostics/resilience nicety — wrapped so it can never turn a
    # successful connect into a failure (e.g. a stub client without get_transport).
    try:
        transport = client.get_transport()
    except Exception:
        transport = None
    if transport is not None:
        try:
            transport.set_log_channel(f"paramiko.transport.{hostname or ssh_target}")
        except Exception:
            pass
        _apply_socket_keepalive(transport, cfg)
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
    positionals: dict[str, int] | None = None,
    secrets: dict[str, Any] | None = None,
    cred_env: dict[str, str] | None = None,
    trigger: str = "",
    runbook: str = "",
    jump: str = "",
) -> None:
    """Execute a remote runnable via SSH, streaming output via callbacks.

    ``session_factory``, when supplied, returns ``(channel, release)`` from a
    pooled transport — the connection is reused, only the channel is opened here,
    and ``release()`` (not a transport close) frees the channel slot afterward.
    Without it, a fresh client is connected and closed per invocation (legacy).

    ``jump`` selects the **system-ssh transport**: when non-empty the invocation
    runs through the system ``ssh`` binary with ``-J <jump>`` (ProxyJump) instead
    of paramiko, so ``~/.ssh/config`` legacy-algorithm settings apply and the
    console can reach hosts it cannot negotiate in paramiko (CentOS 6 offering
    only ``ssh-rsa``/``ssh-dss``). ``session_factory`` is ignored on this path
    (no pooled transport — a per-call ``ssh`` subprocess). See
    :func:`_system_ssh_argv`.

    ``cred_env`` are plain environment variables derived from named credentials
    (final var names like ``JIRA_PASSWORD``, distinct from the ``RUNSPEC_*_ARG_*``
    channel ``secrets`` ride); they are merged into the command's environment the
    same way secrets are — on the channel's stdin as the SPEC env frame.
    """
    bin_dir = Path(runspec_path).parent.as_posix()
    remote_bin = f"{bin_dir}/{runnable}"
    argv = args_to_argv(args, positionals)
    base_env = {"RUNSPEC_AGENT": "1"} if agent else {}
    # Provenance of what caused this run → recorded in the runnable's run_summary
    # (extra.trigger = event source, extra.runbook = the saved runbook that drove it).
    # Both orthogonal to RUNSPEC_AGENT.
    if trigger:
        base_env["RUNSPEC_TRIGGER"] = trigger
    if runbook:
        base_env["RUNSPEC_RUNBOOK"] = runbook
    # password-typed args ride the environment, never argv — see secret_env().
    secrets_env = secret_env(runnable, secrets)
    # Credential-derived plain env vars (final names, e.g. JIRA_PASSWORD).
    all_env = {**base_env, **secrets_env, **(cred_env or {})}
    cmd_argv = [remote_bin, *command_path, *argv]
    # The env (secrets included) never goes in the command line: with any env the
    # command is wrapped in the SPEC env prelude — inside the escalation when
    # run_as is set — and the values ride the channel's stdin as the env frame.
    # Every token is quoted, so an arg value can't inject shell syntax.
    remote_cmd, frame = remote_exec_command(cmd_argv, run_as, become_method, become_flags, env=all_env)

    if jump:
        # System-ssh transport: the local OpenSSH client performs the CentOS 6
        # handshake (through -J), so ~/.ssh/config legacy algorithms apply. The
        # remote command string is passed as a single argv element and executed
        # by the remote login shell — exactly as paramiko's exec_command does —
        # so ``remote_cmd`` is already correctly quoted for the *remote* shell.
        # ``agent=False`` here: RUNSPEC_AGENT (if set) already rides the env
        # frame; it must not leak into the *local* ssh process instead.
        ssh_argv = _system_ssh_argv(ssh_target, jump, identity_file, global_ssh_config)
        _stream(
            [*ssh_argv, remote_cmd],
            on_line,
            on_done,
            timeout=timeout,
            cancel_event=cancel_event,
            agent=False,
            stdin_data=frame,  # ssh forwards it to the remote command's stdin
        )
        return

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
        if frame:
            # EOF after the frame: the runnable reads no more. If the command has
            # already exited (e.g. sudo refused) the write fails — its stderr and
            # exit status below still tell the story, so don't abort the run here.
            try:
                channel.sendall(frame)
                channel.shutdown_write()
            except Exception:
                pass

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
                    for line in remainder.decode("utf-8", errors="replace").splitlines():
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
    stdin: bytes | None = None,
) -> tuple[int, str, str]:
    """Run a single command over SSH, return (exit_code, stdout, stderr).

    Used for discovery, connectivity checks, and log fetching. ``stdin`` (e.g. an
    env frame from :func:`remote_exec_command`) is written to the command's
    standard input, which is then closed.
    """
    try:
        client = _make_ssh_client(ssh_target, identity_file, global_ssh_config)
    except Exception as exc:
        return -1, "", str(exc)

    try:
        sin, stdout, stderr = client.exec_command(command, timeout=timeout)
        feed_channel_stdin(sin, stdin)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err
    except Exception as exc:
        return -1, "", str(exc)
    finally:
        client.close()


def feed_channel_stdin(sin: Any, data: bytes | None) -> None:
    """Write ``data`` to a paramiko ``exec_command`` stdin file and send EOF.
    No-op when ``data`` is empty (the channel is left as it was).

    Best-effort: if the command has already exited (e.g. ``sudo`` refused) the
    write fails, but its output and exit status are still read by the caller —
    and raising here would make the connection pool treat the transport as dead
    and run the command a second time."""
    if not data:
        return
    try:
        sin.write(data)
        sin.flush()
        sin.channel.shutdown_write()
    except Exception:
        pass


def _system_ssh_argv(
    ssh_target: str,
    jump: str,
    identity_file: str | None = None,
    global_ssh_config: dict[str, Any] | None = None,
) -> list[str]:
    """Build the ``ssh`` argv for the system-ssh (jump) transport.

    The final SSH handshake is performed by the system OpenSSH client, not
    paramiko, so ``~/.ssh/config`` applies — HostKeyAlgorithms, KexAlgorithms,
    User, Port, and any further ProxyJump chain. This is what lets the console
    reach legacy hosts (CentOS 6 offering only ``ssh-rsa``/``ssh-dss``) that
    paramiko 4+/5 can no longer negotiate: put the legacy algorithms on the
    target's ``Host`` block in ``~/.ssh/config``.

    ``-J <jump>`` sets the ProxyJump intermediary. Explicit host-entry
    ``identityFile``/port (and the global ``[ssh] user``/``identityFile``) are
    passed as flags and override ssh_config. ``BatchMode=yes`` forbids
    interactive prompts — a password prompt would hang a headless run — so auth
    is key/agent only, matching ``runspec jump``.
    """
    cfg = global_ssh_config or {}
    try:
        connect_timeout = int(float(cfg.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)))
    except (TypeError, ValueError):
        connect_timeout = int(DEFAULT_CONNECT_TIMEOUT)
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, connect_timeout)}",
    ]
    user, host, port = _parse_ssh_target(ssh_target)
    if not user and cfg.get("user"):
        user = str(cfg["user"])
    key = identity_file or cfg.get("identityFile") or ""
    if key:
        argv += ["-i", str(Path(key).expanduser())]
    if port and port != 22:
        argv += ["-p", str(port)]
    if jump:
        argv += ["-J", str(jump)]
    argv.append(f"{user}@{host}" if user else host)
    return argv


def ssh_jump_run(
    ssh_target: str,
    command: str,
    *,
    jump: str,
    identity_file: str | None = None,
    global_ssh_config: dict[str, Any] | None = None,
    timeout: int = 15,
) -> tuple[int, str, str]:
    """Run one command on a host via the system-ssh jump transport.

    Mirrors :func:`ssh_run`'s ``(code, out, err)`` contract so it drops straight
    into the discovery / connectivity runner slot. Uses the system ``ssh`` binary
    (not paramiko) so ``~/.ssh/config`` legacy-algorithm settings apply — see
    :func:`_system_ssh_argv`. Used for discovery (``runspec local``) and the
    ``connectable`` probe of a ``jump`` host.
    """
    argv = _system_ssh_argv(ssh_target, jump, identity_file, global_ssh_config)
    argv.append(command)
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"ssh to {ssh_target} timed out after {timeout}s"
    except FileNotFoundError:
        return (
            -1,
            "",
            "ssh not found — install an OpenSSH client on the console machine",
        )
    return proc.returncode, proc.stdout, proc.stderr


def has_sshpass() -> bool:
    """True if ``sshpass`` is on PATH — required to feed a password to the system
    ``ssh`` client non-interactively (the ``ssh-copy-id`` bootstrap for a ``jump``
    host). Absent on stock Windows, so callers fall back to a manual command."""
    return shutil.which("sshpass") is not None


def ssh_jump_copy_id(
    ssh_target: str,
    jump: str,
    remote_cmd: str,
    password: str,
    *,
    identity_file: str | None = None,
    global_ssh_config: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run ``remote_cmd`` on a ``jump`` host over system ssh with **password**
    auth (via ``sshpass``) — the copy-key bootstrap for a legacy host.

    The regular jump transport uses ``BatchMode=yes`` (key/agent only), but the
    key-install bootstrap must authenticate with a password *before* any key
    exists — a chicken-and-egg BatchMode can't do. ``sshpass -e`` reads the
    password from the ``SSHPASS`` env var (never argv, so it can't leak into the
    process list). ``PubkeyAuthentication=no`` forces the password to be what's
    actually tested (so a stray agent key can't mask a wrong password), and
    ``StrictHostKeyChecking=accept-new`` mirrors the console's TOFU default so an
    unknown host key doesn't hang the prompt. Legacy algorithms + any further
    ProxyJump chain come from ``~/.ssh/config`` (same contract as the run path).

    Caller must check :func:`has_sshpass` first. Returns ``(code, out, err)``.
    """
    cfg = global_ssh_config or {}
    try:
        connect_timeout = int(float(cfg.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)))
    except (TypeError, ValueError):
        connect_timeout = int(DEFAULT_CONNECT_TIMEOUT)
    user, host, port = _parse_ssh_target(ssh_target)
    if not user and cfg.get("user"):
        user = str(cfg["user"])
    target = f"{user}@{host}" if user else host
    argv = [
        "sshpass",
        "-e",
        "ssh",
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "PreferredAuthentications=password",
        "-o",
        f"ConnectTimeout={max(1, connect_timeout)}",
    ]
    if port and port != 22:
        argv += ["-p", str(port)]
    if jump:
        argv += ["-J", str(jump)]
    argv += [target, remote_cmd]
    env = {**os.environ, "SSHPASS": password}
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"ssh to {ssh_target} timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", "sshpass or ssh not found on the console machine"
    return proc.returncode, proc.stdout, proc.stderr


def jump_copy_id_command(ssh_target: str, jump: str, identity_file: str | None = None, port: int = 22) -> str:
    """A copy-paste terminal command that authorises this machine on a ``jump``
    host — shown when ``sshpass`` isn't available (e.g. stock Windows) so the
    copy-key button has an actionable fallback instead of a paramiko error.

    Uses ``ssh-copy-id -o ProxyJump=…`` so the target's legacy algorithms and any
    further ProxyJump come from the operator's ``~/.ssh/config`` (the same config
    the run transport already relies on). ``-i`` names the console's key so the
    right one is copied.
    """
    user, host, parsed_port = _parse_ssh_target(ssh_target)
    target = f"{user}@{host}" if user else host
    effective_port = parsed_port if parsed_port and parsed_port != 22 else port
    parts = ["ssh-copy-id", "-o", f"ProxyJump={jump}"]
    if effective_port and effective_port != 22:
        parts += ["-p", str(effective_port)]
    if identity_file:
        parts += ["-i", str(Path(identity_file).expanduser())]
    parts.append(target)
    return " ".join(shlex.quote(p) for p in parts)


def _stream(
    cmd: list[str],
    on_line: Callable[[str, str], None],
    on_done: Callable[[int, int], None],
    timeout: int | None = None,
    cancel_event: threading.Event | None = None,
    agent: bool = False,
    extra_env: dict[str, str] | None = None,
    stdin_data: bytes | None = None,
) -> None:
    env = dict(_UTF8_ENV)
    if agent:
        env["RUNSPEC_AGENT"] = "1"
    if extra_env:
        env.update(extra_env)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        on_line(start_error(cmd, exc), "stderr")
        on_done(-1, 0)
        return
    if stdin_data and proc.stdin is not None:
        # The SPEC env frame (plain ASCII), then EOF.
        try:
            proc.stdin.write(stdin_data.decode("ascii"))
            proc.stdin.close()
        except OSError:
            pass  # the process already exited; its exit code tells the story

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
