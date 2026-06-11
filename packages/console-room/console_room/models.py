"""
models.py — the room wire schema: a Message and the frame builders / validators.

The server is the authority on a message's ``id`` / ``ts`` / ``sender`` — clients
send only ``room`` + ``text`` and the server stamps the rest (see
docs/design/console-room-server.md §3). These helpers are pure and have no I/O, so
the framing rules are unit-tested on their own.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

# Cap a message body so a pathological post can't bloat the transcript / a frame,
# mirroring the console source's _TEXT_CAP.
TEXT_CAP = 20_000

# Frame type tags.
HELLO = "hello"
MESSAGE = "message"
PRESENCE = "presence"

_VALID_STATUS = ("available", "away")


def new_id() -> str:
    """A fresh message id (server-assigned)."""
    return uuid.uuid4().hex


def now_ts() -> float:
    """The current wall-clock timestamp (server-assigned)."""
    return time.time()


def _str(val: Any) -> str:
    return "" if val is None else str(val)


def _cap(text: str) -> str:
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP] + "…"


@dataclass(frozen=True)
class Message:
    """One sent message — the unit the transcript stores and broadcasts."""

    room: str
    sender: str
    text: str
    id: str
    ts: float

    @classmethod
    def new(cls, room: str, sender: str, text: str) -> Message:
        """Build a message, stamping a fresh ``id`` + ``ts`` (the server's job)."""
        return cls(
            room=_str(room),
            sender=_str(sender),
            text=_cap(_str(text)),
            id=new_id(),
            ts=now_ts(),
        )

    def to_frame(self) -> dict[str, Any]:
        """The server → client ``message`` frame for this message."""
        return {
            "type": MESSAGE,
            "room": self.room,
            "sender": self.sender,
            "text": self.text,
            "id": self.id,
            "ts": self.ts,
        }


def presence_frame(room: str, user: str, status: str) -> dict[str, Any]:
    """A server → client ``presence`` frame. ``status`` is normalised to one of
    ``available`` / ``away`` (anything not ``available`` becomes ``away``)."""
    return {
        "type": PRESENCE,
        "room": _str(room),
        "user": _str(user),
        "status": normalize_status(status),
    }


def normalize_status(status: Any) -> str:
    """Coerce a status to ``available`` / ``away`` (default ``away``)."""
    return "available" if _str(status) == "available" else "away"


@dataclass(frozen=True)
class Hello:
    """A parsed ``hello`` frame — a console declaring its identity on connect."""

    user: str
    room: str


def parse_hello(obj: Any) -> Hello | None:
    """Parse a ``hello`` frame, or ``None`` if it isn't a valid one.

    A blank ``room`` is rejected (a console must say which room it's joining); a
    blank ``user`` falls back to ``"unknown"`` so a mis-configured console is still
    visibly present rather than silently dropped."""
    if not isinstance(obj, dict) or _str(obj.get("type")) != HELLO:
        return None
    room = _str(obj.get("room")).strip()
    if not room:
        return None
    user = _str(obj.get("user")).strip() or "unknown"
    return Hello(user=user, room=room)
