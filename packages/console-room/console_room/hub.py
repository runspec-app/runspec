"""
hub.py — the in-process room broker (transport-agnostic).

The hub owns rooms → participants and does the three jobs of the server: it lets
participants join/leave, broadcasts every message and presence change, and
persists messages to the transcript. It knows nothing about sockets: a
*participant* is anything with ``.user`` / ``.room`` / ``.wants_backscroll`` and an
async ``send(frame)``, so the NDJSON console connection (S1) and the WS browser
connection (S2) plug into the same hub. See docs/design/console-room-server.md §4.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

from .models import Message, presence_frame

logger = logging.getLogger("console_room.hub")


@runtime_checkable
class Participant(Protocol):
    """A connected member of a room — the hub's view of any connection.

    ``wants_backscroll`` is the load-bearing asymmetry: browsers want the recent
    transcript on join, consoles must never get it (forward-only)."""

    user: str
    room: str
    wants_backscroll: bool

    async def send(self, frame: dict[str, Any]) -> None: ...


@runtime_checkable
class Store(Protocol):
    """The slice of the transcript store the hub needs (synchronous; the hub
    calls it via ``asyncio.to_thread``)."""

    def append(self, msg: Message) -> None: ...
    def recent(self, room: str, limit: int) -> list[Message]: ...


async def _safe_send(p: Participant, frame: dict[str, Any]) -> None:
    """Send to one participant, swallowing failures so a dead/slow connection
    can't break a broadcast to the rest of the room."""
    try:
        await p.send(frame)
    except Exception:
        logger.warning("room: send to %r failed; dropping from this round", p.user)


class Hub:
    """Owns the live rooms and routes messages/presence between participants."""

    def __init__(self, store: Store, *, backscroll_limit: int = 50) -> None:
        self._store = store
        self._backscroll = backscroll_limit
        self._rooms: dict[str, set[Participant]] = {}
        self._lock = asyncio.Lock()  # guards _rooms membership

    async def join(self, p: Participant) -> None:
        """Add ``p`` to its room: tell it who's already here, replay backscroll if
        it wants it (browsers only), then announce it to the others."""
        async with self._lock:
            members = self._rooms.setdefault(p.room, set())
            existing = list(members)
            members.add(p)
        # Catch the newcomer up on who is already present.
        for other in existing:
            await _safe_send(p, presence_frame(p.room, other.user, "available"))
        # Backscroll: browsers see recent history; consoles never do.
        if p.wants_backscroll:
            for msg in await self._recent(p.room):
                await _safe_send(p, msg.to_frame())
        # Announce the newcomer to everyone already in the room.
        await self._broadcast(
            p.room, presence_frame(p.room, p.user, "available"), exclude=p
        )

    async def leave(self, p: Participant) -> None:
        """Remove ``p`` and tell the room it's gone (best-effort; idempotent)."""
        async with self._lock:
            members = self._rooms.get(p.room)
            present = bool(members and p in members)
            if members and p in members:
                members.remove(p)
                if not members:
                    self._rooms.pop(p.room, None)
        if present:
            await self._broadcast(
                p.room, presence_frame(p.room, p.user, "away"), exclude=p
            )

    async def on_message(self, p: Participant, text: str) -> None:
        """Stamp, persist, and broadcast a message — echoed to the sender too, so
        every client (including the author's other views) converges on the same
        server-assigned id/ts."""
        msg = Message.new(p.room, p.user, text)
        await asyncio.to_thread(self._store.append, msg)
        await self._broadcast(p.room, msg.to_frame())

    async def on_presence(self, p: Participant, status: str) -> None:
        """Relay a participant's presence change (e.g. a console's rota flip) to
        the rest of the room."""
        await self._broadcast(p.room, presence_frame(p.room, p.user, status), exclude=p)

    async def _recent(self, room: str) -> list[Message]:
        return await asyncio.to_thread(self._store.recent, room, self._backscroll)

    async def _broadcast(
        self,
        room: str,
        frame: dict[str, Any],
        *,
        exclude: Participant | None = None,
    ) -> None:
        async with self._lock:
            members = list(self._rooms.get(room, ()))
        for m in members:
            if m is exclude:
                continue
            await _safe_send(m, frame)

    def room_size(self, room: str) -> int:
        """Number of participants currently in ``room`` (for tests / a health
        endpoint). Read-only snapshot."""
        return len(self._rooms.get(room, ()))
