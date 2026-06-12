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
import json
import logging
from typing import Any, Protocol, runtime_checkable

from .conversation import namespace_matches, parse_conversation_room, queue_namespace
from .lifecycle import Conversation, ConversationRegistry
from .models import (
    KIND_REQUEST,
    REQUEST_ARGS_CAP,
    Message,
    conversation_frame,
    presence_frame,
    request_text,
)

logger = logging.getLogger("console_room.hub")


@runtime_checkable
class Recipient(Protocol):
    """The minimum the hub needs to deliver a frame to a connection: a name to
    stamp/log and an async ``send``. Both room members and namespace agents
    satisfy it, so the broadcast path treats them uniformly."""

    user: str

    async def send(self, frame: dict[str, Any]) -> None: ...


@runtime_checkable
class Participant(Recipient, Protocol):
    """A connected member of a single room — the hub's view of any connection.

    ``wants_backscroll`` is the load-bearing asymmetry: browsers want the recent
    transcript on join, consoles must never get it (forward-only)."""

    room: str
    wants_backscroll: bool


@runtime_checkable
class Agent(Recipient, Protocol):
    """A connection subscribed to a whole queue *namespace* rather than one room
    (the helpdesk agent side). The hub fans every room under ``namespace`` to it,
    present and future. Forward-only, like a console — never backscrolled."""

    namespace: str


@runtime_checkable
class Store(Protocol):
    """The slice of the transcript store the hub needs (synchronous; the hub
    calls it via ``asyncio.to_thread``)."""

    def append(self, msg: Message) -> None: ...
    def recent(self, room: str, limit: int) -> list[Message]: ...


async def _safe_send(p: Recipient, frame: dict[str, Any]) -> None:
    """Send to one recipient, swallowing failures so a dead/slow connection
    can't break a broadcast to the rest of the room."""
    try:
        await p.send(frame)
    except Exception:
        logger.warning("room: send to %r failed; dropping from this round", p.user)


class Hub:
    """Owns the live rooms and routes messages/presence between participants.

    Two kinds of recipient share the hub: **members** (one exact room each —
    group-chat consoles and helpdesk visitors) and **agents** (subscribed to a
    queue *namespace*, receiving every room under it). Agents are kept in a
    separate registry and merged into a room's recipients by prefix match, so the
    group-chat path is unchanged — a room with no agents behaves exactly as before.
    """

    def __init__(self, store: Store, *, backscroll_limit: int = 50) -> None:
        self._store = store
        self._backscroll = backscroll_limit
        self._rooms: dict[str, set[Participant]] = {}
        self._agents: dict[str, set[Agent]] = {}  # namespace -> subscribed agents
        self._convs = ConversationRegistry()  # helpdesk thread lifecycle (live)
        self._catalogs: dict[str, list[dict[str, Any]]] = {}  # namespace -> tools
        self._lock = asyncio.Lock()  # guards _rooms / _agents membership

    def _members_locked(self, room: str) -> set[Recipient]:
        """Everyone who should receive ``room``'s traffic: its exact members plus
        any namespace agent whose queue prefixes the room. Call under ``_lock``."""
        members: set[Recipient] = set(self._rooms.get(room, ()))
        for ns, agents in self._agents.items():
            if namespace_matches(ns, room):
                members |= agents
        return members

    async def join(self, p: Participant) -> None:
        """Add ``p`` to its room: tell it who's already here (members *and* any
        subscribed agents), replay backscroll if it wants it (browsers only), then
        announce it to the others."""
        async with self._lock:
            existing = self._members_locked(p.room)  # p not added yet
            self._rooms.setdefault(p.room, set()).add(p)
        # Catch the newcomer up on who is already present.
        for other in existing:
            await _safe_send(p, presence_frame(p.room, other.user, "available"))
        # Backscroll: browsers see recent history; consoles never do.
        if p.wants_backscroll:
            for msg in await self._recent(p.room):
                await _safe_send(p, msg.to_frame())
        # Catch the newcomer up on the conversation's lifecycle state, if any —
        # so a visitor reloading sees "assigned to … / resolved", not a blank slate.
        conv = self._convs.get(p.room)
        if conv is not None:
            await _safe_send(p, conversation_frame(*conv.frame_fields()))
        # Announce the newcomer to everyone already in the room (incl. agents).
        await self._broadcast(
            p.room, presence_frame(p.room, p.user, "available"), exclude=p
        )

    async def join_agent(self, agent: Agent) -> None:
        """Subscribe ``agent`` to its namespace: catch it up on every conversation
        already open under that queue (the visitors present in each), then announce
        the agent as available in those conversations. No backscroll — an agent is
        forward-only, like a console."""
        ns = agent.namespace
        async with self._lock:
            # Snapshot the open conversations under this namespace before joining.
            active = {
                room: list(members)
                for room, members in self._rooms.items()
                if namespace_matches(ns, room)
            }
            self._agents.setdefault(ns, set()).add(agent)
        # Catch the agent up: who is in each open conversation (its inbox).
        for room, members in active.items():
            for other in members:
                await _safe_send(agent, presence_frame(room, other.user, "available"))
        # …and each thread's lifecycle state (open / assigned-to-whom / resolved),
        # so a console can render its inbox without replaying history. Covers every
        # known thread under the queue, including dormant ones with no one present.
        for conv in self._convs.active_under(ns):
            await _safe_send(agent, conversation_frame(*conv.frame_fields()))
        # Tell each open conversation a team agent is now available.
        for room in active:
            await self._broadcast(
                room, presence_frame(room, agent.user, "available"), exclude=agent
            )

    async def leave_agent(self, agent: Agent) -> None:
        """Unsubscribe ``agent`` and mark it away in every conversation it covered
        (best-effort; idempotent)."""
        ns = agent.namespace
        async with self._lock:
            agents = self._agents.get(ns)
            present = bool(agents and agent in agents)
            if agents and agent in agents:
                agents.remove(agent)
                if not agents:
                    self._agents.pop(ns, None)
            active = [room for room in self._rooms if namespace_matches(ns, room)]
        if present:
            for room in active:
                await self._broadcast(
                    room, presence_frame(room, agent.user, "away"), exclude=agent
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
        server-assigned id/ts.

        In a helpdesk conversation room a visitor message also drives the
        lifecycle: it opens the thread the first time and reopens it if it had been
        resolved/closed — the conversation state goes out *before* the message so a
        reopened thread is live again as the message lands."""
        if parse_conversation_room(p.room) is not None:
            conv = self._convs.open_or_reopen(p.room)
            if conv is not None:
                await self._broadcast(p.room, conversation_frame(*conv.frame_fields()))
        msg = Message.new(p.room, p.user, text)
        await asyncio.to_thread(self._store.append, msg)
        await self._broadcast(p.room, msg.to_frame())

    async def agent_message(self, agent: Agent, room: str, text: str) -> None:
        """An agent posts into a specific conversation ``room`` under its
        namespace. Validated server-side: a target outside the agent's namespace
        is refused, so an agent can't reach a queue it doesn't staff. Otherwise
        identical to ``on_message`` — stamp, persist, broadcast (incl. echo)."""
        if not namespace_matches(agent.namespace, room):
            logger.warning(
                "room: agent %r refused post to %r outside namespace %r",
                agent.user,
                room,
                agent.namespace,
            )
            return
        msg = Message.new(room, agent.user, text)
        await asyncio.to_thread(self._store.append, msg)
        await self._broadcast(room, msg.to_frame())

    # ── conversation lifecycle (agent-driven control) ─────────────────────────
    # claim / resolve / reopen / close move a thread through its states. Each is
    # refused outside the agent's namespace (the queue boundary) and broadcasts the
    # new state to the thread (visitor + team) only when something actually changed.

    async def claim(self, agent: Agent, room: str) -> None:
        """The agent takes ownership of ``room`` → assigned. This is the gate the
        console uses to release the (deferred) agent turn — spend tracks the claim."""
        await self._transition(agent, room, self._convs.claim, room, agent.user)

    async def resolve(self, agent: Agent, room: str) -> None:
        """Mark ``room`` resolved (a visitor reply will reopen it)."""
        await self._transition(agent, room, self._convs.resolve, room)

    async def reopen(self, agent: Agent, room: str) -> None:
        """Reopen a resolved/closed ``room`` → open, back to the queue."""
        await self._transition(agent, room, self._convs.reopen, room)

    async def close(self, agent: Agent, room: str) -> None:
        """Close ``room`` → terminal."""
        await self._transition(agent, room, self._convs.close, room)

    async def _transition(self, agent: Agent, room: str, op: Any, *args: Any) -> None:
        if not namespace_matches(agent.namespace, room):
            logger.warning(
                "room: agent %r refused control of %r outside namespace %r",
                agent.user,
                room,
                agent.namespace,
            )
            return
        conv: Conversation | None = op(*args)
        if conv is not None:
            await self._broadcast(room, conversation_frame(*conv.frame_fields()))

    def conversation_state(self, room: str) -> str | None:
        """The current lifecycle state of ``room`` (for tests), or ``None`` if the
        hub has no record of it yet."""
        conv = self._convs.get(room)
        return conv.state if conv is not None else None

    # ── self-service catalog ──────────────────────────────────────────────────
    # The catalog comes FROM the agent consoles (the side that discovers runnables
    # over SSH+MCP and sees H0's x-discoverable audiences) — the room just caches
    # the published tool schemas per queue and serves them to visitors. It never
    # learns what runspec is and never executes anything: a visitor submission is
    # only ever a structured message an agent console enacts through its own
    # speak/autonomy gates. See docs/design/helpdesk-rooms.md §5.

    def set_catalog(self, namespace: str, tools: list[dict[str, Any]]) -> None:
        """Publish (or clear, with ``[]``) the self-service catalog for a queue
        namespace. Last writer wins — with multiple consoles on one queue the
        most recent publish is the truth, which tracks 'what the on-shift fleet
        can run right now'."""
        if tools:
            self._catalogs[namespace] = tools
        else:
            self._catalogs.pop(namespace, None)
        logger.info("room: catalog for %r now has %d tool(s)", namespace, len(tools))

    def catalog_for(self, namespace: str) -> list[dict[str, Any]]:
        """The published self-service tools for a queue namespace (may be [])."""
        return list(self._catalogs.get(namespace, ()))

    async def visitor_request(
        self, p: Participant, tool: str, args: dict[str, Any]
    ) -> bool:
        """A visitor submits a catalog request from their conversation: validate it
        against the queue's published catalog, then persist + broadcast it as a
        ``request``-kind message (text = human summary, data = machine payload).
        Drives the lifecycle like any visitor message (open/reopen). Returns
        ``False`` when refused — unknown tool, oversized args, or not a
        conversation room — so the door can tell the client."""
        parsed = parse_conversation_room(p.room)
        if parsed is None:
            logger.warning("room: request from non-conversation room %r", p.room)
            return False
        queue, _visitor = parsed
        ns = queue_namespace(queue)
        if not any(t.get("name") == tool for t in self._catalogs.get(ns, ())):
            logger.warning(
                "room: %r requested %r — not in the %r catalog", p.user, tool, ns
            )
            return False
        if len(json.dumps(args)) > REQUEST_ARGS_CAP:
            logger.warning(
                "room: %r request for %r refused — args too large", p.user, tool
            )
            return False
        conv = self._convs.open_or_reopen(p.room)
        if conv is not None:
            await self._broadcast(p.room, conversation_frame(*conv.frame_fields()))
        msg = Message.new(
            p.room,
            p.user,
            request_text(tool, args),
            kind=KIND_REQUEST,
            data={"tool": tool, "args": args},
        )
        await asyncio.to_thread(self._store.append, msg)
        await self._broadcast(p.room, msg.to_frame())
        return True

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
        exclude: Recipient | None = None,
    ) -> None:
        async with self._lock:
            members = self._members_locked(room)
        for m in members:
            if m is exclude:
                continue
            await _safe_send(m, frame)

    def room_size(self, room: str) -> int:
        """Number of exact members currently in ``room`` (for tests / a health
        endpoint). Excludes namespace agents. Read-only snapshot."""
        return len(self._rooms.get(room, ()))

    def agent_count(self, namespace: str) -> int:
        """Number of agents currently subscribed to ``namespace`` (for tests)."""
        return len(self._agents.get(namespace, ()))
