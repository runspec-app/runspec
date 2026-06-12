"""
sources/chat.py — the chat-room event source (cross-platform).

Source #3 for triggers. A small team chat room runs as a web app on a remote host
the console already reaches by SSH; each running console is a participant, and a
message in the room fires a ``message`` event the trigger machinery reacts to.
Unlike Outlook / the notification listener this is not an OS API — it's a network
client — so it carries no platform guard.

The room is a *dumb message pipe with presence*: no runnables, no args, no
curation live in the web app. All intelligence stays in runspec-console, and
nothing the console does in the room is automatic — a drafted reply lands in the
composer for a human to send (§2 of the design), and any runnable a reaction
proposes still goes through the autonomy gate.

**Forward-only, the chat way.** The trigger system never replays backlog; for a
chat room that's exactly right — an away participant doesn't answer old messages,
and the web app keeps its own transcript so nothing is lost. A console also never
re-emits *its own* messages (the server echoes them back): :func:`parse_inbound`
drops ``sender == self_user`` so a console can't react to its own acks.

The source is written against an injected :class:`ChatTransport` (``send`` /
``recv`` / ``close``) so the room/presence/parse logic is unit-tested with a fake;
the real SSH-tunnel-backed websocket transport drops in behind the same interface
(the next slice — see docs/design/console-chat-room-source.md §4/§6). Until a
transport is wired, ``available()`` reports ``(False, …)`` so the engine never
starts a half-built source in production.

See docs/design/console-chat-room-source.md.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Protocol

from .. import config
from ..events import EmitFn, Event, register_source

logger = logging.getLogger("runspec.console.triggers.chat")

# Cap a message body so a pathological post can't bloat the event / card, matching
# the Outlook source's _BODY_CAP intent.
_TEXT_CAP = 20_000


# ── transport seam ─────────────────────────────────────────────────────────────


class ChatTransport(Protocol):
    """A bidirectional JSON message channel to the room server.

    The real implementation is a websocket over the console's existing SSH tunnel
    to the room host (next slice); tests inject a fake. ``recv`` blocks until the
    next inbound message and returns ``None`` once the channel closes.
    """

    def send(self, obj: dict[str, Any]) -> None: ...
    def recv(self) -> dict[str, Any] | None: ...
    def close(self) -> None: ...


TransportFactory = Callable[[], ChatTransport]


# ── pure payload extraction / routing (tested with fakes) ──────────────────────


def _str(val: Any) -> str:
    return "" if val is None else str(val)


def _float(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def message_payload(obj: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``message`` payload from a wire dict.

    ``kind``/``data`` carry the helpdesk request shape: a visitor's catalog
    submission arrives as ``kind="request"`` with ``data={"tool", "args"}`` —
    plain chat is ``kind="chat"`` with no data (older servers send neither)."""
    text = _str(obj.get("text"))
    if len(text) > _TEXT_CAP:
        text = text[:_TEXT_CAP] + "…"
    payload = {
        "room": _str(obj.get("room")),
        "sender": _str(obj.get("sender")),
        "text": text,
        "id": _str(obj.get("id")),
        "ts": _float(obj.get("ts")),
        "kind": _str(obj.get("kind")) or "chat",
    }
    if isinstance(obj.get("data"), dict):
        payload["data"] = obj["data"]
    return payload


def presence_payload(obj: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``presence`` payload from a wire dict."""
    return {
        "room": _str(obj.get("room")),
        "user": _str(obj.get("user")),
        "status": _str(obj.get("status")),
    }


def conversation_payload(obj: dict[str, Any]) -> dict[str, Any]:
    """Extract a ``conversation`` lifecycle payload (helpdesk thread state)."""
    assignee = obj.get("assignee")
    return {
        "room": _str(obj.get("room")),
        "state": _str(obj.get("state")),
        "assignee": _str(assignee) if assignee is not None else None,
    }


def parse_inbound(obj: Any, self_user: str) -> Event | None:
    """Convert one inbound wire dict to an :class:`Event`, or ``None`` to drop it.

    Drops anything that isn't a dict, unknown ``type`` values, and — crucially —
    a ``message`` whose ``sender`` is this console (``self_user``): the server
    echoes our own sent messages back, and a console must never react to its own
    acks. A console's own ``presence`` echo is dropped for the same reason.
    ``conversation`` frames (helpdesk thread lifecycle) always pass — state is
    server-authoritative, even when this console's claim caused it.
    """
    if not isinstance(obj, dict):
        return None
    kind = _str(obj.get("type"))
    if kind == "message":
        if self_user and _str(obj.get("sender")) == self_user:
            return None
        return Event.new("chat", "message", message_payload(obj))
    if kind == "presence":
        if self_user and _str(obj.get("user")) == self_user:
            return None
        return Event.new("chat", "presence", presence_payload(obj))
    if kind == "conversation":
        return Event.new("chat", "conversation", conversation_payload(obj))
    return None


def _safe_emit(emit: EmitFn, event: Event) -> None:
    try:
        emit(event)
    except Exception:
        logger.exception("chat: emit failed for %s", event.type)


# ── the source ────────────────────────────────────────────────────────────────


class ChatSource:
    """Chat-room event source. Satisfies the ``EventSource`` protocol.

    Construct with an explicit ``transport`` (tests) or a ``transport_factory``
    (production — the SSH-tunnel websocket); with neither, ``available()`` reports
    that the transport isn't wired and ``start()`` is a no-op. ``room``/``user``
    default to the ``[chat]`` config table.
    """

    id = "chat"

    def __init__(
        self,
        *,
        room: str | None = None,
        user: str | None = None,
        namespace: str | None = None,
        transport: ChatTransport | None = None,
        transport_factory: TransportFactory | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        cfg = config.read_chat()
        self._room = room if room is not None else _str(cfg.get("room"))
        self._user = user if user is not None else _str(cfg.get("user"))
        # Helpdesk agent mode: subscribe to a whole queue namespace
        # (``helpdesk/<queue>``) instead of one room. The hub fans every
        # conversation under it to this connection — the agent's inbox.
        self._namespace = _str(namespace)
        self._transport = transport
        self._transport_factory = transport_factory
        # Fired when the recv loop ends because the channel dropped (not via
        # stop()), so an always-on owner can reconnect. Never called on a clean
        # stop().
        self._on_closed = on_closed
        self._emit: EmitFn | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def is_agent(self) -> bool:
        """True when this source is a helpdesk agent (namespace-subscribed)."""
        return bool(self._namespace)

    def available(self) -> tuple[bool, str]:
        if not self._room and not self._namespace:
            return (False, "no chat room configured — set [chat] room in config")
        if self._transport is None and self._transport_factory is None:
            return (False, "chat transport not yet wired")
        return (True, "")

    def event_types(self) -> list[str]:
        return ["message", "presence", "conversation"]

    def start(self, emit: EmitFn) -> None:
        ok, reason = self.available()
        if not ok:
            logger.info("chat source not started: %s", reason)
            return
        if self._thread is not None:
            return  # already started
        self._emit = emit
        self._stop.clear()
        if self._transport is None and self._transport_factory is not None:
            self._transport = self._transport_factory()
        # Declare identity first — the room server expects ``hello`` as the opening
        # frame and binds the name to the connection (the tunnel hides the OS user,
        # so the console states it). Then announce presence (we're available; the
        # rota may flip us to "away" later via set_presence).
        self._send_hello()
        self._send_presence("available")
        self._thread = threading.Thread(
            target=self._run, name="chat-listener", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._send_presence("away")  # tell the room we're gone before closing
        transport = self._transport
        if transport is not None:
            try:
                transport.close()
            except Exception:
                logger.exception("chat: transport close failed")
        self._thread = None
        self._transport = None
        self._emit = None

    def set_presence(self, available: bool) -> None:
        """Publish a presence change (the bridge calls this when the rota flips)."""
        self._send_presence("available" if available else "away")

    def send_message(self, text: str, *, room: str | None = None) -> None:
        """Send a human-composed message. The agent never calls this directly —
        a drafted reply only pre-fills the composer; the human sends (design §2).

        In room mode the target is this source's room. In agent mode the caller
        must name the conversation (``room=``) — the hub routes it to that one
        visitor (+ the team) and refuses a target outside our namespace."""
        transport = self._transport
        if transport is None:
            logger.info("chat: send_message with no transport — dropped")
            return
        target = (room or "").strip() or self._room
        if not target:
            logger.info("chat: agent send_message without a conversation — dropped")
            return
        try:
            transport.send({"type": "message", "room": target, "text": text})
        except Exception:
            logger.exception("chat: send_message failed")

    def send_catalog(self, tools: list[dict[str, Any]]) -> None:
        """Publish the queue's self-service catalog (agent mode only). The room
        caches it per namespace and serves it to visitors; ``[]`` clears it."""
        transport = self._transport
        if transport is None or not self.is_agent:
            return
        try:
            transport.send({"type": "catalog", "tools": tools})
        except Exception:
            logger.exception("chat: catalog send failed")

    _CONTROL_KINDS = ("claim", "resolve", "reopen", "close")

    def send_control(self, kind: str, room: str) -> None:
        """Send a conversation-lifecycle control frame (agent mode only):
        claim / resolve / reopen / close on one conversation. The hub enforces
        the namespace boundary and broadcasts the state change."""
        transport = self._transport
        if transport is None or not self.is_agent or kind not in self._CONTROL_KINDS:
            return
        try:
            transport.send({"type": kind, "room": room})
        except Exception:
            logger.exception("chat: control %r send failed", kind)

    def _send_hello(self) -> None:
        """Announce identity on connect — the first frame the room server expects.
        It binds ``user`` to this connection and stamps it onto every later
        message, so a per-message sender can't be spoofed. A blank user is left to
        the server to fill (it falls back to ``unknown``). An agent declares its
        queue ``namespace`` instead of a room — the server subscribes it to every
        conversation under that queue."""
        transport = self._transport
        if transport is None:
            return
        frame = (
            {"type": "hello", "namespace": self._namespace, "user": self._user}
            if self.is_agent
            else {"type": "hello", "room": self._room, "user": self._user}
        )
        try:
            transport.send(frame)
        except Exception:
            logger.exception("chat: hello send failed")

    def _send_presence(self, status: str) -> None:
        # Agent presence is server-side (join/leave of the namespace) — the hub
        # ignores agent presence frames, so don't send them.
        transport = self._transport
        if transport is None or self.is_agent:
            return
        try:
            transport.send({"type": "presence", "room": self._room, "status": status})
        except Exception:
            logger.exception("chat: presence send failed")

    def _handle(self, obj: Any) -> None:
        """Route one inbound wire dict (split out so routing is tested without
        threads, mirroring the pure-helper testing of the other sources)."""
        event = parse_inbound(obj, self._user)
        if event is not None and self._emit is not None:
            _safe_emit(self._emit, event)

    def _run(self) -> None:
        """Drain inbound messages from the transport until it closes or stop()."""
        transport = self._transport
        if transport is None:
            return
        logger.info("chat source: listening in room %r", self._room)
        while not self._stop.is_set():
            try:
                obj = transport.recv()
            except Exception:
                logger.exception("chat: recv failed")
                break
            if obj is None:  # channel closed
                break
            self._handle(obj)
        # If the loop ended because the channel dropped (not a clean stop()),
        # notify the owner so it can reconnect.
        if not self._stop.is_set() and self._on_closed is not None:
            try:
                self._on_closed()
            except Exception:
                logger.exception("chat: on_closed callback failed")


register_source("chat", ChatSource)
