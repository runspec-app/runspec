"""
ndjson_server.py — the console front door: an NDJSON listener on localhost.

A console reaches the room by opening a ``direct-tcpip`` channel over its pooled
SSH connection to this localhost port (see docs/design/console-room-source.md §4),
then speaks newline-delimited JSON. This module accepts those connections, reads
their frames, and drives the hub. It is the only place that knows about sockets;
the hub stays transport-agnostic.

Identity is connection-scoped: the first frame must be ``hello`` (the console
declaring its name), which binds the name to the connection. Every later
``message`` / ``presence`` is stamped with that name — per-frame ``user`` fields
are ignored, so a frame can't spoof a different sender (§3).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .hub import Hub
from .models import MESSAGE, PRESENCE, normalize_status, parse_hello

logger = logging.getLogger("console_room.ndjson")


class NdjsonConsoleConnection:
    """One console connection. Satisfies the hub's ``Participant`` protocol.

    Consoles are forward-only: ``wants_backscroll`` is ``False`` so the hub never
    replays history to them."""

    wants_backscroll = False

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.user = ""
        self.room = ""
        self._send_lock = asyncio.Lock()  # serialise concurrent writes

    async def send(self, frame: dict[str, Any]) -> None:
        data = (json.dumps(frame) + "\n").encode("utf-8")
        async with self._send_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def run(self, hub: Hub) -> None:
        """Drive this connection: handshake, then pump frames into ``hub`` until
        the channel closes. Always leaves the hub and closes the writer."""
        hello = parse_hello(await self._read_frame())
        if hello is None:
            logger.info("room: connection sent no/invalid hello; closing")
            await self._close()
            return
        self.user, self.room = hello.user, hello.room
        await hub.join(self)
        logger.info("room: %r joined %r", self.user, self.room)
        try:
            while True:
                frame = await self._read_frame()
                if frame is None:
                    break  # EOF / channel closed
                await self._dispatch(hub, frame)
        finally:
            await hub.leave(self)
            await self._close()
            logger.info("room: %r left %r", self.user, self.room)

    async def _dispatch(self, hub: Hub, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == MESSAGE:
            text = str(frame.get("text") or "")
            if text:
                await hub.on_message(self, text)
        elif kind == PRESENCE:
            await hub.on_presence(self, normalize_status(frame.get("status")))
        # hello-after-hello and unknown types are ignored.

    async def _read_frame(self) -> dict[str, Any] | None:
        """Read one JSON object, skipping blank / malformed lines; ``None`` at
        EOF. Mirrors the console transport's decoder."""
        while True:
            try:
                line = await self._reader.readline()
            except Exception:
                return None
            if not line:
                return None  # EOF
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                logger.warning("room: dropping non-JSON frame")
                continue
            if isinstance(obj, dict):
                return obj
            logger.warning("room: dropping non-object frame")

    async def _close(self) -> None:
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


async def serve_ndjson(
    hub: Hub, host: str = "127.0.0.1", port: int = 8765
) -> asyncio.Server:
    """Start the NDJSON listener and return the running ``asyncio.Server`` (the
    caller awaits ``serve_forever`` / closes it). Binds localhost by default —
    consoles only ever reach it over the SSH tunnel."""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await NdjsonConsoleConnection(reader, writer).run(hub)

    server = await asyncio.start_server(handle, host, port)
    sockets = server.sockets or ()
    for sock in sockets:
        logger.info("room: NDJSON listener on %s", sock.getsockname())
    return server
