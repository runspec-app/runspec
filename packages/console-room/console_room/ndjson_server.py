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
from .models import (
    ADVERTISE,
    CATALOG,
    MESSAGE,
    PRESENCE,
    REQUEST_COMPLETE,
    REQUEST_START,
    VERIFY_SENT,
    normalize_status,
    parse_advertise,
    parse_catalog,
    parse_hello,
    parse_verify_sent,
)

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
        self.room = ""  # set in room-member mode
        self.namespace = ""  # set in agent mode (subscribes to a whole queue)
        self.is_agent = False
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
        # One email = one room: when the room is tied to a mailbox, only consoles
        # configured for that same mailbox may connect. A mismatch (or a console
        # that sent no mailbox) is refused so a console can't join the wrong
        # helpdesk's room.
        if hub.mailbox and hello.mailbox.strip().lower() != hub.mailbox:
            logger.warning(
                "room: console %r refused — mailbox %r does not match this room's %r",
                hello.user,
                hello.mailbox or "(none)",
                hub.mailbox,
            )
            await self._close()
            return
        self.user = hello.user
        if hello.is_agent:
            await self._run_agent(hub, hello.namespace)
        else:
            await self._run_member(hub, hello.room)

    async def _run_member(self, hub: Hub, room: str) -> None:
        """Group-chat / visitor mode: join one room, pump frames, leave."""
        self.room = room
        await hub.join(self)
        logger.info("room: %r joined %r", self.user, self.room)
        try:
            await self._pump(hub)
        finally:
            await hub.leave(self)
            await self._close()
            logger.info("room: %r left %r", self.user, self.room)

    async def _run_agent(self, hub: Hub, namespace: str) -> None:
        """Helpdesk agent mode: subscribe to a queue namespace, pump frames, leave."""
        self.namespace = namespace
        self.is_agent = True
        await hub.join_agent(self)
        logger.info("room: agent %r subscribed to %r", self.user, self.namespace)
        try:
            await self._pump(hub)
        finally:
            await hub.leave_agent(self)
            await self._close()
            logger.info(
                "room: agent %r unsubscribed from %r", self.user, self.namespace
            )

    async def _pump(self, hub: Hub) -> None:
        while True:
            frame = await self._read_frame()
            if frame is None:
                break  # EOF / channel closed
            await self._dispatch(hub, frame)

    async def _dispatch(self, hub: Hub, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == MESSAGE:
            text = str(frame.get("text") or "")
            if not text:
                return
            if self.is_agent:
                # An agent must name the conversation it is posting into; the hub
                # refuses a target outside the agent's namespace.
                room = str(frame.get("room") or "").strip()
                if room:
                    await hub.agent_message(self, room, text)
            else:
                await hub.on_message(self, text)
        elif kind == PRESENCE and not self.is_agent:
            await hub.on_presence(self, normalize_status(frame.get("status")))
        elif kind == PRESENCE and self.is_agent:
            # An agent's rota flip (on/off shift). It stays connected either way;
            # this moves its runnables in/out of the live catalogue and, coming on
            # shift, drains any backlog it can now serve.
            await hub.set_agent_shift(
                self, normalize_status(frame.get("status")) == "available"
            )
        elif self.is_agent and kind == REQUEST_START:
            # The assigned operator began enacting a portal request.
            request_id = str(frame.get("id") or "").strip()
            if request_id:
                await hub.start_request(self, request_id)
        elif self.is_agent and kind == REQUEST_COMPLETE:
            # The assigned operator finished — ``error`` (if any) marks a failure.
            request_id = str(frame.get("id") or "").strip()
            error = frame.get("error")
            if request_id:
                await hub.complete_request(
                    self, request_id, str(error) if error else None
                )
        elif self.is_agent and kind == CATALOG:
            # The console publishing its queue's self-service catalog (filtered to
            # x-discoverable ∋ "self-service" on its side). [] clears it.
            tools = parse_catalog(frame)
            if tools is not None:
                # Record this console's runnables in the presence-aware live
                # catalogue (portal routing + affinity). [] clears its contribution.
                hub.publish_catalog(self, tools)
        elif self.is_agent and kind == ADVERTISE:
            # The console advertising a queue it staffs (mailbox + label) for the
            # email-verify sign-up dropdown; this also flushes any pending verify
            # mail for that queue to this console.
            ad = parse_advertise(frame)
            if ad is not None:
                await hub.on_advertise(self, ad.queue, ad.queue_email, ad.display_name)
        elif self.is_agent and kind == VERIFY_SENT:
            # The console acking a send_verify: ok → the outbox row is sent; a
            # failure returns it to pending to retry on another console.
            parsed = parse_verify_sent(frame)
            if parsed is not None:
                await hub.on_verify_sent(*parsed)
        # hello-after-hello, agent presence, and unknown types are ignored.

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
