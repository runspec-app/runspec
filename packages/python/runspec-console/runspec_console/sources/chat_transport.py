"""
sources/chat_transport.py — the SSH-tunnel transport behind the chat source.

``ChatSource`` (sources/chat.py) is written against an injected ``ChatTransport``
(``send`` / ``recv`` / ``close``). This module provides the production one: a
**newline-delimited-JSON stream over a paramiko ``direct-tcpip`` channel** to the
room server's localhost port on the SSH host the console already pools a
connection to.

Why NDJSON-over-channel and not a websocket: a console's right to be in the room
*is* its SSH access (decided in the design — §4), so the link rides the existing
pooled SSH transport via ``SSHConnectionPool.open_direct_tcpip``. A paramiko
``Channel`` is a byte stream, not a full socket (no ``setsockopt``), so running a
websocket client straight over it needs a fragile local-port forwarder; a
line-delimited JSON framing over the channel is simpler, dependency-free, and
unit-testable with a fake channel. Browsers still speak websockets to the room
server; consoles speak this — same message schema, two front doors. See
docs/design/console-chat-room-source.md §4.

The framing/buffering is pure and tested with a fake channel; the only untested
seam is the pool call in :func:`make_tunnel_transport_factory`, which is thin.
"""

from __future__ import annotations

import getpass
import json
import logging
import threading
from typing import Any, Callable, Protocol

from .chat import ChatTransport, TransportFactory

logger = logging.getLogger("runspec.console.triggers.chat")

_RECV_BYTES = 32_768


class DirectTcpipOpener(Protocol):
    """The slice of ``SSHConnectionPool`` this module needs: open a direct-tcpip
    channel to ``dest_host:dest_port`` via a pooled jump transport, returning
    ``(channel, release)`` where ``release()`` frees the pooled session slot."""

    def open_direct_tcpip(
        self,
        ssh_target: str,
        dest_host: str,
        dest_port: int,
        *,
        identity_file: str | None = ...,
        global_ssh_config: dict[str, Any] | None = ...,
    ) -> tuple[Any, Callable[[], None]]: ...


class ExecOpener(Protocol):
    """The slice of ``SSHConnectionPool`` the exec transport needs: ``exec`` a
    command on a pooled transport, returning ``(channel, release)``. The channel's
    stdout/stdin behave like the direct-tcpip channel's recv/sendall, so the same
    NDJSON framing (``ChannelChatTransport``) drives both."""

    def open_exec(
        self,
        ssh_target: str,
        command: str,
        *,
        identity_file: str | None = ...,
        global_ssh_config: dict[str, Any] | None = ...,
    ) -> tuple[Any, Callable[[], None]]: ...


def _decode(line: bytes) -> dict[str, Any] | None:
    """Parse one framed line to a dict, or ``None`` for blank / malformed / non-object."""
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        logger.warning("chat: dropping non-JSON frame")
        return None
    if isinstance(obj, dict):
        return obj
    logger.warning("chat: dropping non-object frame")
    return None


class ChannelChatTransport:
    """``ChatTransport`` over a paramiko ``direct-tcpip`` channel, NDJSON-framed.

    ``recv`` blocks reading the channel a line at a time and returns the next
    JSON object, skipping blank / malformed lines, and ``None`` once the channel
    reaches EOF or is closed. ``close`` shuts the channel and frees the pooled
    session slot (``release``); it is idempotent.
    """

    def __init__(self, channel: Any, release: Callable[[], None] | None = None) -> None:
        self._chan = channel
        self._release = release
        self._buf = b""
        self._lock = threading.Lock()  # serialise concurrent sends
        self._closed = False

    def send(self, obj: dict[str, Any]) -> None:
        if self._closed or self._chan is None:
            return
        frame = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            with self._lock:
                self._chan.sendall(frame)
        except Exception:
            logger.exception("chat: channel send failed")

    def _fill(self) -> bool:
        """Read one chunk into the buffer. ``False`` on EOF / closed / error."""
        try:
            chunk = self._chan.recv(_RECV_BYTES)
        except Exception:
            return False
        if not chunk:
            return False
        self._buf += chunk
        return True

    def recv(self) -> dict[str, Any] | None:
        while True:
            nl = self._buf.find(b"\n")
            while nl == -1:
                if not self._fill():
                    # EOF — try a final unterminated frame, then signal closed.
                    if self._buf.strip():
                        line, self._buf = self._buf, b""
                        obj = _decode(line)
                        if obj is not None:
                            return obj
                    return None
                nl = self._buf.find(b"\n")
            line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            obj = _decode(line)
            if obj is not None:
                return obj
            # blank / malformed line: keep reading for the next frame

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        chan, self._chan = self._chan, None
        if chan is not None:
            try:
                chan.close()
            except Exception:
                pass
        release, self._release = self._release, None
        if release is not None:
            try:
                release()
            except Exception:
                logger.exception("chat: channel release failed")


def make_tunnel_transport_factory(
    pool: DirectTcpipOpener,
    ssh_target: str,
    dest_port: int,
    *,
    dest_host: str = "127.0.0.1",
    identity_file: str | None = None,
    global_ssh_config: dict[str, Any] | None = None,
) -> TransportFactory:
    """A zero-arg factory that opens a fresh tunneled transport on each call.

    Passed to ``ChatSource(transport_factory=…)``; the source invokes it on
    ``start`` (and a later slice can re-invoke it to reconnect). Each call opens a
    direct-tcpip channel to ``dest_host:dest_port`` (the room server's localhost
    port) through the pooled SSH connection to ``ssh_target``.
    """

    def factory() -> ChatTransport:
        channel, release = pool.open_direct_tcpip(
            ssh_target,
            dest_host,
            dest_port,
            identity_file=identity_file,
            global_ssh_config=global_ssh_config,
        )
        return ChannelChatTransport(channel, release)

    return factory


def default_user_from_ssh(ssh_target: str, fallback_user: str) -> str:
    """Derive a room display name from the SSH target. The tunnel hides the OS user
    from the server, so the console declares one; the natural default is the SSH
    *login* it connects as. Use a ``user@host`` target as-is; otherwise prefix
    ``fallback_user`` (→ ``alice@web01``). Empty target → ``fallback_user`` or
    ``"console"`` so it's never blank."""
    target = (ssh_target or "").strip()
    fb = (fallback_user or "").strip()
    if not target:
        return fb or "console"
    if "@" in target:
        return target
    return f"{fb}@{target}" if fb else target


def default_room_user(ssh_target: str, ssh_user: str = "") -> str:
    """The SSH-defaulted room display name for a host — its ssh login. Prefers the
    configured ``[ssh]`` username (``ssh_user``, the login the tunnel actually
    authenticates as) over the local OS user, so a Windows console connecting to a
    Linux host shows the Linux identity, not the Windows one."""
    fallback = (ssh_user or "").strip()
    if not fallback:
        try:
            fallback = getpass.getuser()
        except Exception:
            fallback = ""
    return default_user_from_ssh(ssh_target, fallback)


#: Room namespace prefix for helpdesk queues — must match the room server's
#: console_room.conversation.HELPDESK_PREFIX.
HELPDESK_PREFIX = "helpdesk"


def _valid_queue(queue: str) -> bool:
    """A queue token must not contain the namespace separator or whitespace —
    mirrors the room server's ``valid_token`` (a bad token could escape its
    namespace)."""
    return bool(queue) and not any(c == "/" or c.isspace() for c in queue)


def host_rooms(
    hosts: list[dict[str, Any]], *, ssh_user: str = ""
) -> list[dict[str, Any]]:
    """Extract the room specs declared on fleet host entries — those with a
    ``room`` name (group chat) and/or a ``room_queue`` (helpdesk agent mode),
    plus a ``room_port`` (the server's localhost port on that host) and an
    ``ssh`` target. Each spec carries everything the tunnel needs, *inherited
    from the host*: its ssh target + identityFile (so the tunnel reuses the pooled
    connection), the room, the port, and a display user (``room_user`` override,
    else the SSH login — ``ssh_user`` is the configured ``[ssh]`` default username,
    preferred over the local OS user).

    **Helpdesk agent mode:** ``room_queue = "it-support"`` declares this console
    an agent of that queue on the host's room server. The spec's ``room`` key is
    the queue *namespace* (``helpdesk/it-support``) — unique alongside any plain
    ``room`` the same host also declares — with ``queue`` carrying the bare name
    and ``catalog_allowlist`` the optional ``room_catalog`` narrowing list (bare
    runnable names; absent = publish every self-service-marked runnable)."""
    specs: list[dict[str, Any]] = []
    for h in hosts:
        ssh_target = str(h.get("ssh") or "").strip()
        room = str(h.get("room") or "").strip()
        queue = str(h.get("room_queue") or "").strip()
        port = h.get("room_port")
        # bool is an int subclass — a TOML true/false is not a valid port.
        if (
            not ssh_target
            or not (room or queue)
            or not isinstance(port, int)
            or isinstance(port, bool)
        ):
            continue
        user = str(h.get("room_user") or "").strip() or default_room_user(
            ssh_target, ssh_user
        )
        dest_host = str(h.get("room_dest_host") or "127.0.0.1")
        # `room_via = "exec"` opts a host into the exec-channel transport (for a
        # network that blocks SSH port-forwarding): the console runs a relay command
        # on the host instead of opening a direct-tcpip tunnel. The command is, in
        # order: `room_exec_command` verbatim if set (e.g. `socat - TCP:127.0.0.1:8765`);
        # else `room_command` verbatim if it already carries arguments (a space —
        # e.g. `nc 127.0.0.1 8765`), since the operator clearly gave a full command;
        # else `{room_command} connect --host … --port …` (room_command is the bare
        # console-room binary, which takes that subcommand).
        via = (
            "exec"
            if str(h.get("room_via") or "").strip().lower() == "exec"
            else "tunnel"
        )
        room_command = str(h.get("room_command") or "console-room").strip()
        explicit_exec = str(h.get("room_exec_command") or "").strip()
        if explicit_exec:
            exec_command = explicit_exec
        elif " " in room_command:
            # Already a full command (has args) → use as-is; don't append the
            # console-room subcommand (that's what broke `nc 127.0.0.1 8765`).
            exec_command = room_command
        else:
            exec_command = (
                f"{room_command} connect --host {dest_host} --port {int(port)}"
            )
        base = {
            "host": str(h.get("name") or ssh_target),
            "ssh_target": ssh_target,
            "identity_file": h.get("identityFile") or None,
            "port": int(port),
            "dest_host": dest_host,
            "user": user,
            "via": via,
            "exec_command": exec_command,
        }
        if room:
            specs.append({**base, "room": room, "queue": ""})
        if _valid_queue(queue):
            allowlist = h.get("room_catalog")
            specs.append(
                {
                    **base,
                    "room": f"{HELPDESK_PREFIX}/{queue}",
                    "queue": queue,
                    "catalog_allowlist": [str(n) for n in allowlist]
                    if isinstance(allowlist, list)
                    else None,
                    # Email-verify sign-up: the queue's mailbox (verify-from + the
                    # allowlist domain) and an optional display label. Absent
                    # ``room_queue_email`` → the queue isn't offered for sign-up.
                    "queue_email": str(h.get("room_queue_email") or "").strip(),
                    "queue_label": str(h.get("room_queue_label") or "").strip(),
                }
            )
    return specs


def make_host_room_factory(
    pool: DirectTcpipOpener,
    spec: dict[str, Any],
    *,
    global_ssh_config: dict[str, Any] | None = None,
) -> TransportFactory:
    """Tunnel factory for one room spec (from :func:`host_rooms`). Rides the host's
    own ssh target + identityFile + the global ``[ssh]`` config, so the room tunnel
    reuses the working pooled connection to that host instead of opening a fresh,
    mis-configured one."""
    return make_tunnel_transport_factory(
        pool,
        str(spec["ssh_target"]),
        int(spec["port"]),
        dest_host=str(spec.get("dest_host") or "127.0.0.1"),
        identity_file=spec.get("identity_file") or None,
        global_ssh_config=global_ssh_config,
    )


def make_host_room_exec_factory(
    pool: ExecOpener,
    spec: dict[str, Any],
    *,
    global_ssh_config: dict[str, Any] | None = None,
) -> TransportFactory:
    """Exec-channel factory for a room spec — runs ``spec['exec_command']`` (a
    ``console-room connect`` on the host) over an SSH exec channel and frames NDJSON
    over its stdio. For networks that block SSH port-forwarding; no tunnel."""

    def factory() -> ChatTransport:
        channel, release = pool.open_exec(
            str(spec["ssh_target"]),
            str(spec["exec_command"]),
            identity_file=spec.get("identity_file") or None,
            global_ssh_config=global_ssh_config,
        )
        return ChannelChatTransport(channel, release)

    return factory
