"""
web/connection.py — the browser front door's per-connection logic.

A person in a browser reaches the room over a WebSocket (wss when TLS is configured). This
is the browser counterpart of ndjson_server.NdjsonConsoleConnection, and like it,
it satisfies the hub's ``Participant`` protocol. The one behavioural difference is
``wants_backscroll = True``: a person loading the page sees recent history, where
a console (forward-only) never does.

It speaks only to a tiny ``WebSocketLike`` seam (``send_json`` / ``receive`` /
``close``), so the routing/transport (Starlette) is a thin adapter in web/app.py
and this logic is unit-tested with a fake socket — the same split that keeps the
hub transport-agnostic. See docs/design/console-room-server.md §2/§6.

Identity is connection-scoped: ``user`` / ``room`` are fixed at construction (from
the WS query params in dev mode, from the email-verify session otherwise), so inbound frames never
carry — and can never spoof — a sender.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from ..hub import Hub
from ..models import MESSAGE, PAGE, PRESENCE, REQUEST, normalize_status

logger = logging.getLogger("console_room.web")


class WebSocketLike(Protocol):
    """The slice of a WebSocket this module needs. ``receive`` returns the next
    inbound JSON object, ``{}`` for a frame to skip, or ``None`` once the socket
    closes."""

    async def send_json(self, obj: dict[str, Any]) -> None: ...
    async def receive(self) -> dict[str, Any] | None: ...
    async def close(self) -> None: ...


class BrowserConnection:
    """One browser participant. Backscroll-eligible (a person wants history)."""

    wants_backscroll = True

    def __init__(
        self,
        ws: WebSocketLike,
        *,
        user: str,
        room: str,
        display_name: str = "",
        team: str = "",
    ) -> None:
        self._ws = ws
        self.user = user
        self.room = room
        # The verified visitor's full name + team (email-verify signup), for the
        # operator card. Display-only; ``user`` (the email) stays the identity.
        # Empty otherwise.
        self.display_name = display_name
        self.team = team

    async def send(self, frame: dict[str, Any]) -> None:
        await self._ws.send_json(frame)

    async def run(self, hub: Hub) -> None:
        """Join, pump inbound frames into ``hub`` until the socket closes, then
        leave (always — so presence clears on disconnect)."""
        await hub.join(self)
        logger.info("room: browser %r joined %r", self.user, self.room)
        try:
            while True:
                frame = await self._ws.receive()
                if frame is None:
                    break  # socket closed
                await self._dispatch(hub, frame)
        finally:
            await hub.leave(self)
            logger.info("room: browser %r left %r", self.user, self.room)

    async def _dispatch(self, hub: Hub, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == MESSAGE:
            text = str(frame.get("text") or "")
            if text:
                await hub.on_message(self, text)
        elif kind == PRESENCE:
            await hub.on_presence(self, normalize_status(frame.get("status")))
        elif kind == REQUEST:
            # A catalogue submission from this visitor's form. The hub validates the
            # runnable against the live catalogue, auto-assigns it to an on-shift
            # holder (or holds it unassigned + pages), and is a no-op outside a
            # conversation room — so a group-chat browser can't smuggle requests.
            tool = str(frame.get("tool") or "").strip()
            args = frame.get("args")
            if tool:
                await hub.submit_request(
                    self, tool, args if isinstance(args, dict) else {}
                )
        elif kind == PAGE:
            # The visitor's "Page the team" action on a still-unassigned request of
            # their own thread (the hub guards ownership + state).
            request_id = str(frame.get("id") or "").strip()
            if request_id:
                await hub.page_request(self, request_id)
        # unknown types are ignored
