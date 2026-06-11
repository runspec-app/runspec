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

from .. import config
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


def default_user_from_ssh(ssh_target: str, os_user: str) -> str:
    """Derive a room display name from the SSH target when ``[chat] user`` is
    unset. The tunnel hides the OS user from the server, so the console declares
    one; the natural default is the SSH login it connects as. Use a ``user@host``
    target as-is; otherwise prefix the local OS user (→ ``alice@web01``). Falls
    back to the OS user, then ``"console"``, so it's never blank."""
    target = (ssh_target or "").strip()
    os_user = (os_user or "").strip()
    if not target:
        return os_user or "console"
    if "@" in target:
        return target
    return f"{os_user}@{target}" if os_user else target


def default_chat_user(cfg: dict[str, Any]) -> str:
    """The SSH-defaulted room user for a ``[chat]`` config table (``host`` + the
    local OS user). Convenience for the bridge — see ``default_user_from_ssh``."""
    try:
        os_user = getpass.getuser()
    except Exception:
        os_user = ""
    return default_user_from_ssh(str(cfg.get("host") or ""), os_user)


def transport_factory_from_config(
    pool: DirectTcpipOpener,
) -> TransportFactory | None:
    """Build the tunnel factory from the ``[chat]`` config, or ``None`` if the
    room isn't configured (``host`` + ``port`` required). Convenience for the
    bridge wiring; ``room``/``user`` are read by ``ChatSource`` itself."""
    cfg = config.read_chat()
    host = str(cfg.get("host") or "").strip()
    port = cfg.get("port")
    if not host or not isinstance(port, int):
        return None
    return make_tunnel_transport_factory(
        pool,
        host,
        port,
        dest_host=str(cfg.get("dest_host") or "127.0.0.1"),
        identity_file=cfg.get("identity_file") or None,
    )
