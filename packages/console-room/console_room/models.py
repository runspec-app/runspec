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
CATALOG = "catalog"  # agent → server: the queue's self-service tool schemas
REQUEST = "request"  # visitor → server: a structured catalog request (tool + args)
ASSIGNMENT = "assignment"  # server → operator: a portal request routed to you
REQUEST_STATUS = "request_status"  # server → thread: a request's lifecycle state
PAGE = "page"  # server → every connected console of a queue: come serve this

# Inbound portal control-frame tags (operator console → server): drive the
# claim-free request lifecycle for a request assigned to this console.
REQUEST_START = "request_start"  # the operator began enacting the runnable
REQUEST_COMPLETE = "request_complete"  # the operator finished (ok, or with error)

# Email-verify queue-signup frames (see docs/design/email-queue-signup.md §7).
ADVERTISE = "advertise"  # agent → server: a queue this console staffs (mailbox + label)
SEND_VERIFY = (
    "send_verify"  # server → agent: send this verify mail as the queue mailbox
)
VERIFY_SENT = "verify_sent"  # agent → server: ack a send_verify (ok, or with error)

# Message kinds — how a transcript entry was produced. "chat" is a typed message;
# "request" is a structured catalog submission (text = human summary, data = the
# machine payload the agent console enacts).
KIND_CHAT = "chat"
KIND_REQUEST = "request"

# Caps for agent-published catalogs and visitor request payloads, so a hostile or
# broken client can't bloat the cache / the transcript.
CATALOG_TOOLS_CAP = 200
REQUEST_ARGS_CAP = 8_000  # serialized-JSON character budget for request args

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
    """One sent message — the unit the transcript stores and broadcasts.

    ``kind`` distinguishes a typed chat line (``"chat"``, the default) from a
    structured catalog request (``"request"``); ``data`` carries the request's
    machine payload (tool + args) and is ``None`` for plain chat. Both default so
    every pre-existing call site builds plain chat messages unchanged."""

    room: str
    sender: str
    text: str
    id: str
    ts: float
    kind: str = KIND_CHAT
    data: dict[str, Any] | None = None

    @classmethod
    def new(
        cls,
        room: str,
        sender: str,
        text: str,
        *,
        kind: str = KIND_CHAT,
        data: dict[str, Any] | None = None,
    ) -> Message:
        """Build a message, stamping a fresh ``id`` + ``ts`` (the server's job)."""
        return cls(
            room=_str(room),
            sender=_str(sender),
            text=_cap(_str(text)),
            id=new_id(),
            ts=now_ts(),
            kind=kind,
            data=data,
        )

    def to_frame(self) -> dict[str, Any]:
        """The server → client ``message`` frame for this message. ``data`` is
        included only when present, so plain chat frames are unchanged on the
        wire (consoles ignore unknown fields either way)."""
        frame: dict[str, Any] = {
            "type": MESSAGE,
            "room": self.room,
            "sender": self.sender,
            "text": self.text,
            "id": self.id,
            "ts": self.ts,
            "kind": self.kind,
        }
        if self.data is not None:
            frame["data"] = self.data
        return frame


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
    """A parsed ``hello`` frame — a console declaring its identity on connect.

    Two shapes, distinguished by ``namespace``:

    - **room member** (group-chat console, the original form): ``room`` set,
      ``namespace`` blank — the connection joins that one room.
    - **agent** (helpdesk console): ``namespace`` set, ``room`` blank — the
      connection subscribes to a whole queue namespace and the hub fans every
      conversation under it to that connection.
    """

    user: str
    room: str
    namespace: str = ""

    @property
    def is_agent(self) -> bool:
        return bool(self.namespace)


def parse_catalog(obj: Any) -> list[dict[str, Any]] | None:
    """Parse an agent's ``catalog`` frame into a validated tool list, or ``None``
    if it isn't one.

    Light validation by design — the room is a dumb pipe, not a schema authority.
    Each entry must be a dict with a non-empty string ``name``; everything else
    (``description``, ``inputSchema``, ``x-autonomy``, …) passes through as the
    consoles published it. The list is capped at ``CATALOG_TOOLS_CAP`` and an
    empty list is valid (it *clears* the queue's catalog — e.g. the last console
    going off shift)."""
    if not isinstance(obj, dict) or _str(obj.get("type")) != CATALOG:
        return None
    tools = obj.get("tools")
    if not isinstance(tools, list):
        return None
    result: list[dict[str, Any]] = []
    for entry in tools[:CATALOG_TOOLS_CAP]:
        if isinstance(entry, dict) and _str(entry.get("name")).strip():
            result.append(entry)
    return result


def assignment_frame(
    request_id: str, room: str, runnable: str, args: dict[str, Any], visitor: str
) -> dict[str, Any]:
    """A server → operator ``assignment`` frame: a portal request routed to one
    operator's console (its inbox item). Carries everything needed to enact it —
    the runnable, the visitor's args, and the thread ``room`` to reply on."""
    return {
        "type": ASSIGNMENT,
        "id": _str(request_id),
        "room": _str(room),
        "runnable": _str(runnable),
        "args": args,
        "visitor": _str(visitor),
    }


def request_status_frame(
    request_id: str,
    room: str,
    state: str,
    assignee: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """A server → thread ``request_status`` frame: a portal request's lifecycle
    state (``unassigned`` / ``assigned`` / ``in_progress`` / ``done`` / ``failed``)
    and its assignee. ``error`` is included only on a failure."""
    frame: dict[str, Any] = {
        "type": REQUEST_STATUS,
        "id": _str(request_id),
        "room": _str(room),
        "state": _str(state),
        "assignee": assignee,
    }
    if error is not None:
        frame["error"] = _str(error)
    return frame


def page_frame(
    request_id: str, room: str, runnable: str, queue: str, visitor: str
) -> dict[str, Any]:
    """A server → console ``page`` frame: a chime to *every connected* console of a
    queue (on shift and off), naming the runnable an unassigned request is waiting
    on. A console that holds it can come on shift to pick it up (the pending
    request then auto-assigns), or respond out of band. Carries the thread ``room``
    so a responder can find it. See docs/design/runnables-portal.md §3.4."""
    return {
        "type": PAGE,
        "id": _str(request_id),
        "room": _str(room),
        "runnable": _str(runnable),
        "queue": _str(queue),
        "visitor": _str(visitor),
    }


@dataclass(frozen=True)
class Advertise:
    """A parsed ``advertise`` frame — a console declaring a queue it staffs, with
    the mailbox that is both the verify-from address and the allowlist domain."""

    queue: str
    queue_email: str
    display_name: str = ""


def parse_advertise(obj: Any) -> Advertise | None:
    """Parse an ``advertise`` frame, or ``None`` if it isn't a valid one. Requires
    a non-empty ``queue`` and ``queue_email``; ``display_name`` is optional."""
    if not isinstance(obj, dict) or _str(obj.get("type")) != ADVERTISE:
        return None
    queue = _str(obj.get("queue")).strip()
    queue_email = _str(obj.get("queue_email")).strip()
    if not queue or not queue_email:
        return None
    return Advertise(
        queue=queue,
        queue_email=queue_email,
        display_name=_str(obj.get("display_name")).strip(),
    )


def send_verify_frame(
    nonce: str,
    to: str,
    name: str,
    queue: str,
    queue_email: str,
    verify_url: str,
) -> dict[str, Any]:
    """A server → agent ``send_verify`` frame: "send this verify mail *as the queue
    mailbox*." The console composes a fresh message from ``queue_email`` to ``to``
    with a link to ``verify_url`` and replies with a ``verify_sent`` ack carrying
    the same ``nonce`` (which correlates it to the outbox row)."""
    return {
        "type": SEND_VERIFY,
        "nonce": _str(nonce),
        "to": _str(to),
        "name": _str(name),
        "queue": _str(queue),
        "queue_email": _str(queue_email),
        "verify_url": _str(verify_url),
    }


def parse_verify_sent(obj: Any) -> tuple[str, bool] | None:
    """Parse a ``verify_sent`` ack into ``(nonce, ok)``, or ``None`` if it isn't a
    valid one (no ``nonce``). ``ok`` is the truthiness of the frame's ``ok`` field —
    a failed send (``ok`` false / absent) returns the row to the outbox to retry."""
    if not isinstance(obj, dict) or _str(obj.get("type")) != VERIFY_SENT:
        return None
    nonce = _str(obj.get("nonce")).strip()
    if not nonce:
        return None
    return nonce, bool(obj.get("ok"))


def request_text(tool: str, args: dict[str, Any]) -> str:
    """The human-readable transcript line for a catalog request — what a person
    (or a console's pre-filter) reads; the machine payload travels in ``data``."""
    rendered = ", ".join(f"{k}={v}" for k, v in args.items())
    return f"Requested: {tool}" + (f" — {rendered}" if rendered else "")


def parse_hello(obj: Any) -> Hello | None:
    """Parse a ``hello`` frame, or ``None`` if it isn't a valid one.

    An agent declares a ``namespace`` (and needs no ``room``); a plain room member
    declares a ``room`` (the original form). A frame with neither is rejected — a
    console must say *what* it is joining. A blank ``user`` falls back to
    ``"unknown"`` so a mis-configured console is still visibly present rather than
    silently dropped. ``namespace`` wins if both are somehow present."""
    if not isinstance(obj, dict) or _str(obj.get("type")) != HELLO:
        return None
    user = _str(obj.get("user")).strip() or "unknown"
    namespace = _str(obj.get("namespace")).strip()
    if namespace:
        return Hello(user=user, room="", namespace=namespace)
    room = _str(obj.get("room")).strip()
    if not room:
        return None
    return Hello(user=user, room=room)
