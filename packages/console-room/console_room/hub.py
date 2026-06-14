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

from .catalog import LiveCatalog
from .conversation import namespace_matches, parse_conversation_room, queue_namespace
from .models import (
    KIND_REQUEST,
    REQUEST_ARGS_CAP,
    Message,
    assignment_frame,
    page_frame,
    presence_frame,
    request_status_frame,
    request_text,
)
from .requests import UNASSIGNED, Request, RequestRegistry

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
        self._live = LiveCatalog()  # presence-aware per-agent catalogue + affinity
        self._requests = RequestRegistry()  # claim-free portal request lifecycle
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
        # Announce the newcomer to everyone already in the room (incl. agents).
        await self._broadcast(
            p.room, presence_frame(p.room, p.user, "available"), exclude=p
        )

    async def join_agent(self, agent: Agent) -> None:
        """Subscribe ``agent`` to its namespace: catch it up on every active
        conversation under that queue (the visitors present in each), then announce
        the agent as available there. No backscroll — an agent is forward-only,
        like a console."""
        ns = agent.namespace
        async with self._lock:
            # Snapshot the active rooms under this namespace before joining.
            active = {
                room: list(members)
                for room, members in self._rooms.items()
                if namespace_matches(ns, room)
            }
            self._agents.setdefault(ns, set()).add(agent)
            # A freshly connected console is on shift by default until its rota
            # says otherwise (set_agent_shift). It contributes no runnables until
            # it publishes a catalogue.
            self._live.set_presence(ns, agent.user, True)
        # Catch the agent up: who is in each active conversation (its inbox).
        for room, members in active.items():
            for other in members:
                await _safe_send(agent, presence_frame(room, other.user, "available"))
        # Tell each active conversation a team agent is now available.
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
            # The console is gone: drop its runnables from the live catalogue so a
            # single-holder runnable vanishes the moment its only console leaves.
            self._live.withdraw(ns, agent.user)
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
        server-assigned id/ts. In a helpdesk conversation room this is the small
        clarification thread that hangs off a request; the request's own lifecycle
        travels in ``request_status`` frames, not here."""
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

    # ── presence-aware live catalogue + affinity (the portal spine) ───────────
    # The catalogue comes FROM the agent consoles (the side that discovers
    # runnables over SSH+MCP and sees H0's x-discoverable audiences) — the room
    # just caches what *each* connected console publishes and whether it is on
    # shift, so the live catalogue a visitor browses is the union of the on-shift
    # consoles' runnables and every request can be routed to a console that
    # actually holds it. The room never learns what runspec is and never executes
    # anything. See docs/design/runnables-portal.md §3.2.

    def publish_catalog(self, agent: Agent, tools: list[dict[str, Any]]) -> None:
        """Record the runnables ``agent`` (one console) holds for its queue. ``[]``
        means it holds nothing right now — it stays connected but drops out of the
        live catalogue. Presence is preserved across re-publishes."""
        self._live.publish(agent.namespace, agent.user, tools)
        logger.info(
            "room: %r published %d runnable(s) to %r",
            agent.user,
            len(tools),
            agent.namespace,
        )

    async def set_agent_shift(self, agent: Agent, on_shift: bool) -> None:
        """Flip ``agent``'s rota presence. Off shift, it stays connected (and
        pageable) but its runnables leave the live catalogue; back on shift they
        reappear. Coming *on* shift drains the backlog: any ``unassigned`` request
        in the queue this operator can now serve auto-assigns to an on-shift
        holder (so a request that was stranded gets picked up without a re-submit)."""
        self._live.set_presence(agent.namespace, agent.user, on_shift)
        if on_shift:
            for req in self._requests.unassigned_under(agent.namespace):
                await self._try_assign(req)

    def live_catalog(self, namespace: str) -> list[dict[str, Any]]:
        """The runnables a visitor of ``namespace`` can request right now — the
        deduplicated union of the on-shift consoles' published catalogues."""
        return self._live.live(namespace)

    def runnable_holders(self, namespace: str, tool: str) -> list[str]:
        """The on-shift consoles that hold ``tool`` — the routing candidates for a
        request. Empty when no on-shift console can serve it."""
        return self._live.holders(namespace, tool)

    def connected_holders(self, namespace: str, tool: str) -> list[str]:
        """Every connected console that holds ``tool``, on shift or not — the
        audience a page targets when no on-shift holder is available."""
        return self._live.connected_holders(namespace, tool)

    # ── portal requests: auto-assignment + claim-free lifecycle ───────────────
    # A visitor submits a catalog request; the hub routes it to an on-shift holder
    # (round-robin) and tracks its lifecycle without any claim step. The assigned
    # operator's console gets an ``assignment`` frame; the request thread (visitor
    # + team) gets ``request_status`` frames as the state moves. See §3.3 / §3.5.

    def _find_agent(self, namespace: str, user: str) -> Agent | None:
        """The connected agent ``user`` under ``namespace``, or ``None``. Used to
        deliver an assignment to exactly the operator it routed to."""
        for agent in self._agents.get(namespace, ()):
            if agent.user == user:
                return agent
        return None

    async def _try_assign(self, req: Request) -> bool:
        """Route an ``unassigned`` request to an on-shift holder by round-robin and
        announce it; ``False`` (left unassigned) when no on-shift holder exists."""
        candidates = self._live.holders(req.namespace, req.runnable)
        operator = self._requests.next_assignee(req.namespace, candidates)
        if operator is None:
            return False
        assigned = self._requests.assign(req.id, operator)
        if assigned is None:  # raced into a non-unassigned state — leave it
            return False
        agent = self._find_agent(assigned.namespace, operator)
        if agent is not None:
            await _safe_send(agent, assignment_frame(*assigned.assignment_fields()))
        await self._broadcast(
            assigned.room, request_status_frame(*assigned.status_fields())
        )
        return True

    async def _hold_unassigned(self, req: Request) -> None:
        """Announce an ``unassigned`` request to its thread (the visitor sees "no
        operator available") and **page** every connected console of the queue so a
        holder can come on shift to pick it up."""
        await self._broadcast(req.room, request_status_frame(*req.status_fields()))
        await self._page(req)

    async def _page(self, req: Request) -> int:
        """Chime every *connected* console of ``req``'s queue — on shift and off —
        naming the runnable. Returns how many consoles were paged (for logging /
        tests). The frame is informational; coming on shift is what actually picks
        the request up."""
        async with self._lock:
            consoles = list(self._agents.get(req.namespace, ()))
        frame = page_frame(req.id, req.room, req.runnable, req.queue, req.visitor)
        for console in consoles:
            await _safe_send(console, frame)
        logger.info(
            "room: paged %d console(s) for %r in %r",
            len(consoles),
            req.runnable,
            req.namespace,
        )
        return len(consoles)

    async def page_request(self, p: Participant, request_id: str) -> bool:
        """The visitor's "Page the team" action: re-chime the queue for a still
        ``unassigned`` request of their own thread. Refused (``False``) if the
        request is unknown, not in the visitor's room, or already assigned/done —
        you can only page something that is actually waiting."""
        req = self._requests.get(request_id)
        if req is None or req.room != p.room or req.state != UNASSIGNED:
            return False
        await self._page(req)
        return True

    async def submit_request(
        self, p: Participant, tool: str, args: dict[str, Any]
    ) -> Request | None:
        """A visitor submits a portal request from their conversation. Validated
        against the **live catalogue** (some connected console must hold ``tool`` —
        on shift or, tolerating the off-shift race, at least pageable) and the args
        budget. Persists the request as a ``request``-kind thread message, then
        **auto-assigns** to an on-shift holder (round-robin) or holds it
        ``unassigned`` for a page. Returns the :class:`Request` (with its resolved
        state), or ``None`` when refused."""
        parsed = parse_conversation_room(p.room)
        if parsed is None:
            logger.warning("room: portal request from non-conversation room %r", p.room)
            return None
        queue, visitor = parsed
        ns = queue_namespace(queue)
        if not self._live.connected_holders(ns, tool):
            logger.warning(
                "room: %r requested %r — no connected console holds it in %r",
                p.user,
                tool,
                ns,
            )
            return None
        if len(json.dumps(args)) > REQUEST_ARGS_CAP:
            logger.warning(
                "room: %r request for %r refused — args too large", p.user, tool
            )
            return None
        req = self._requests.create(p.room, queue, ns, visitor, tool, args)
        # Record the ask on the thread (links to the lifecycle record via id).
        msg = Message.new(
            p.room,
            p.user,
            request_text(tool, args),
            kind=KIND_REQUEST,
            data={"tool": tool, "args": args, "request_id": req.id},
        )
        await asyncio.to_thread(self._store.append, msg)
        await self._broadcast(p.room, msg.to_frame())
        # Auto-assign, or hold unassigned and page the team so a holder can come on.
        if not await self._try_assign(req):
            await self._hold_unassigned(req)
        return req

    async def start_request(self, agent: Agent, request_id: str) -> None:
        """The assignee marks a request ``in_progress`` (it began enacting). Scoped
        to the agent's namespace and to the actual assignee; broadcasts the new
        state to the thread."""
        req = self._requests.get(request_id)
        if req is None or not namespace_matches(agent.namespace, req.room):
            return
        updated = self._requests.start(request_id, agent.user)
        if updated is not None:
            await self._broadcast(
                updated.room, request_status_frame(*updated.status_fields())
            )

    async def complete_request(
        self, agent: Agent, request_id: str, error: str | None = None
    ) -> None:
        """The assignee marks a request ``done`` (or ``failed`` with ``error``).
        Scoped to the namespace and the assignee; broadcasts the terminal state to
        the thread."""
        req = self._requests.get(request_id)
        if req is None or not namespace_matches(agent.namespace, req.room):
            return
        updated = self._requests.complete(request_id, agent.user, error)
        if updated is not None:
            await self._broadcast(
                updated.room, request_status_frame(*updated.status_fields())
            )

    async def release_request(self, request_id: str) -> None:
        """Return an assigned-but-untouched request to the queue (the AFK timeout,
        or an operator dropping off mid-assignment). It re-assigns to another
        on-shift holder if one exists, else holds ``unassigned`` for a page."""
        req = self._requests.get(request_id)
        if req is None:
            return
        released = self._requests.release(request_id)
        if released is None:
            return
        if not await self._try_assign(released):
            await self._hold_unassigned(released)

    def request_state(self, request_id: str) -> str | None:
        """The current lifecycle state of a request (for tests), or ``None`` if
        unknown."""
        req = self._requests.get(request_id)
        return req.state if req is not None else None

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
