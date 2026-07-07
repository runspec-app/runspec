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
    TEAM_ADD,
    TEAM_LIST,
    TEAM_REMOVE,
    VERIFY_SENT,
    normalize_status,
    parse_advertise,
    parse_catalog,
    parse_hello,
    parse_verify_sent,
    teams_frame,
)

logger = logging.getLogger("console_room.ndjson")

# Max size of a single NDJSON frame (one line) the reader will buffer. asyncio's
# StreamReader defaults to 64 KiB, which a console's **catalog** frame — one line
# carrying every runnable's schema — blows past on any non-trivial fleet, raising
# LimitOverrunError from readline() and (before this) tearing the connection down
# right after subscribe: the deterministic "flap" symptom. 16 MiB gives ample
# headroom for a large catalogue while still bounding a pathological frame. It's
# a per-connection *ceiling*, not a preallocation.
_NDJSON_READER_LIMIT = 16 * 1024 * 1024


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
        peer = self._peername()
        logger.debug("room: connection opened from %s — awaiting hello", peer)
        hello = parse_hello(await self._read_frame())
        if hello is None:
            logger.info("room: connection from %s sent no/invalid hello; closing", peer)
            await self._close()
            return
        # Log exactly what the console declared *before* the checks, so a rejected
        # or short-lived connection is diagnosable at --debug (e.g. a mailbox the
        # console didn't send, or a namespace/room you didn't expect).
        logger.debug(
            "room: hello from %s — user=%r mode=%s target=%r mailbox=%r",
            peer,
            hello.user,
            "agent" if hello.is_agent else "member",
            hello.namespace if hello.is_agent else hello.room,
            hello.mailbox or "(none)",
        )
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
        who = self.user or self._peername()
        while True:
            frame = await self._read_frame()
            if frame is None:
                logger.debug("room: frame pump for %r ended (channel closed)", who)
                break  # EOF / channel closed
            await self._dispatch(hub, frame)

    async def _dispatch(self, hub: Hub, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        logger.debug("room: ← %r frame from %r", kind, self.user or self._peername())
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
        elif self.is_agent and kind == TEAM_LIST:
            # The console managing the customer-team list (the sign-up dropdown):
            # request it, add one (by display name), or remove one (by slug). Each
            # is answered with the current list. Trusted by the SSH-tunnel door.
            await self.send(teams_frame(hub.list_teams()))
        elif self.is_agent and kind == TEAM_ADD:
            name = str(frame.get("name") or "").strip()
            await self.send(
                teams_frame(hub.add_team(name) if name else hub.list_teams())
            )
        elif self.is_agent and kind == TEAM_REMOVE:
            slug = str(frame.get("slug") or "").strip()
            teams = hub.remove_team(slug) if slug else hub.list_teams()
            await self.send(teams_frame(teams))
        # hello-after-hello, agent presence, and unknown types are ignored.

    def _peername(self) -> str:
        """Best-effort remote address for logging (the tunnel end, usually a
        localhost ephemeral port). Never raises."""
        try:
            peer = self._writer.get_extra_info("peername")
            if isinstance(peer, tuple) and len(peer) >= 2:
                return f"{peer[0]}:{peer[1]}"
            return str(peer) if peer else "?"
        except Exception:
            return "?"

    async def _read_frame(self) -> dict[str, Any] | None:
        """Read one JSON object, skipping blank / malformed lines; ``None`` at
        EOF. Mirrors the console transport's decoder.

        Distinguishes a **clean EOF** (the peer closed the channel — ``readline``
        returns b"") from a **read error** (the transport died mid-stream —
        ``readline`` raised) in the log, so a dropped console connection shows
        *why* it ended at ``--debug`` rather than vanishing silently. Both still
        return ``None`` (the pump treats either as closed).

        An **oversized frame** (one line larger than the reader's
        ``_NDJSON_READER_LIMIT``) is skipped, not fatal: ``readline`` raises
        ``LimitOverrunError`` (or a ``ValueError`` re-raise) after asyncio has
        already advanced/cleared the buffer, so we log a clear warning and keep
        reading rather than dropping the connection. This was the cause of the
        connect→subscribe→instant-drop flap when a console's catalog frame
        exceeded the old 64 KiB stdlib default — now the limit is 16 MiB *and* an
        over-limit frame degrades gracefully instead of killing the session."""
        while True:
            try:
                line = await self._reader.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                # One frame exceeded the buffer limit. asyncio has already
                # discarded/advanced the buffer, so skip this frame and keep the
                # connection alive (the usual culprit is an oversized catalog —
                # raise the limit in serve_ndjson if a legitimate one is bigger).
                logger.warning(
                    "room: oversized frame from %s skipped (exceeds the %d-byte "
                    "reader limit) — connection kept: %s",
                    self._peername(),
                    _NDJSON_READER_LIMIT,
                    exc,
                )
                continue
            except Exception:
                logger.debug(
                    "room: read error on %s — treating as closed",
                    self._peername(),
                    exc_info=True,
                )
                return None
            if not line:
                logger.debug("room: clean EOF from %s (peer closed)", self._peername())
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
    hub: Hub,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    limit: int = _NDJSON_READER_LIMIT,
) -> asyncio.Server:
    """Start the NDJSON listener and return the running ``asyncio.Server`` (the
    caller awaits ``serve_forever`` / closes it). Binds localhost by default —
    consoles only ever reach it over the SSH tunnel.

    ``limit`` is the StreamReader's per-line buffer ceiling (default
    :data:`_NDJSON_READER_LIMIT`, 16 MiB). It must exceed the largest single
    frame — in practice the console's catalog — or ``readline`` raises
    ``LimitOverrunError`` on that frame; the stdlib default of 64 KiB is too
    small for a real catalogue."""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await NdjsonConsoleConnection(reader, writer).run(hub)

    server = await asyncio.start_server(handle, host, port, limit=limit)
    sockets = server.sockets or ()
    for sock in sockets:
        logger.info("room: NDJSON listener on %s", sock.getsockname())
    return server
