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

from .catalog import LiveCatalog, visible_to
from .conversation import (
    DEFAULT_QUEUE,
    namespace_matches,
    parse_conversation_room,
    queue_namespace,
)
from .enrollment_store import EnrollmentStore
from .queues import QueueDescriptor, QueueDirectory
from .models import (
    KIND_REQUEST,
    REQUEST_ARGS_CAP,
    Message,
    assignment_frame,
    now_ts,
    page_frame,
    presence_frame,
    request_status_frame,
    request_text,
    send_verify_frame,
)
from .requests import ASSIGNED, UNASSIGNED, Request, RequestRegistry
from .signup import (
    EmailVerifyConfig,
    Member,
    PendingSignup,
    Team,
    make_nonce,
    make_team_slug,
    read_verify_token,
    sign_verify_token,
)

logger = logging.getLogger("console_room.hub")

#: How often the email-verify outbox is swept for expired pending signups
#: (seconds). A row lingers at most one interval past the token TTL before it's
#: dropped — exact cadence doesn't matter (an expired link just makes the user
#: re-sign), so a periodic cleanup is enough.
SWEEP_INTERVAL = 300.0


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

    def __init__(
        self,
        store: Store,
        *,
        backscroll_limit: int = 50,
        assignment_timeout: float = 300.0,
        enrollment: EnrollmentStore | None = None,
        email_verify: EmailVerifyConfig | None = None,
        mailbox: str = "",
    ) -> None:
        self._store = store
        self._backscroll = backscroll_limit
        # Email-verify queue signup (optional): the durable members/outbox store and
        # the signing config. Both present → the hub dispatches verify mail to a
        # console of the queue and verifies the clicked link. Absent → no-op.
        self._enrollment = enrollment
        self._email_verify = email_verify
        # The helpdesk identity this room serves (a ``[mailbox]`` address). When
        # set, the console NDJSON door only accepts consoles configured for the
        # same mailbox — one email = one room. Lowercased for case-insensitive
        # matching; empty = no tie (dev / open).
        self.mailbox = mailbox.strip().lower()
        # An assigned request whose console never picks it up (no request_start)
        # within this many seconds is auto-released back to the queue + re-paged.
        # 0/negative disables the timer.
        self._assignment_timeout = assignment_timeout
        self._afk_timers: dict[str, asyncio.Task[None]] = {}
        self._rooms: dict[str, set[Participant]] = {}
        self._agents: dict[str, set[Agent]] = {}  # namespace -> subscribed agents
        self._live = LiveCatalog()  # presence-aware per-agent catalogue + affinity
        self._queues = QueueDirectory()  # advertised queues → email-verify sign-up
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
            # …and drop the queues it advertised from the sign-up directory.
            self._queues.withdraw(agent.user)
            active = [room for room in self._rooms if namespace_matches(ns, room)]
        # Return any verify sends this console claimed-but-never-acked to the outbox,
        # then re-flush those queues so a remaining console picks them up.
        if self._enrollment is not None:
            for queue in self._enrollment.release_console_claims(agent.user):
                await self._flush_queue(queue)
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

    def live_catalog(
        self, namespace: str, *, operator: bool = False
    ) -> list[dict[str, Any]]:
        """The entries a session of ``namespace`` can request right now — the
        deduplicated union of the on-shift consoles' published catalogues, filtered
        to what the session may see: a visitor (``operator=False``) gets only the
        self-service subset; an operator gets the full catalogue plus runbooks."""
        return self._live.live(namespace, operator=operator)

    def runnable_holders(self, namespace: str, tool: str) -> list[str]:
        """The on-shift consoles that hold ``tool`` — the routing candidates for a
        request. Empty when no on-shift console can serve it."""
        return self._live.holders(namespace, tool)

    def connected_holders(self, namespace: str, tool: str) -> list[str]:
        """Every connected console that holds ``tool``, on shift or not — the
        audience a page targets when no on-shift holder is available."""
        return self._live.connected_holders(namespace, tool)

    # ── advertised queues (email-verify sign-up directory) ────────────────────
    # A console advertises, per queue it staffs, the mailbox + display name that
    # populate the email-verify sign-up dropdown. The room aggregates the live
    # advertisements; the wire frame that drives this lands in slice 3, and the
    # withdraw is tied to the console's disconnect. See email-queue-signup.md §4.

    def advertise_queue(
        self, console: str, queue: str, queue_email: str, display_name: str = ""
    ) -> None:
        """Record that ``console`` staffs ``queue`` (mailbox + label) — a row in the
        sign-up dropdown for as long as the console is connected."""
        self._queues.advertise(
            console, QueueDescriptor(queue, queue_email, display_name)
        )

    def withdraw_queues(self, console: str) -> None:
        """Drop every queue ``console`` advertised (its console disconnected)."""
        self._queues.withdraw(console)

    def advertised_queues(self) -> list[QueueDescriptor]:
        """The live sign-up list — the advertised queues with a usable mailbox
        domain, deduplicated by queue name."""
        return self._queues.queues()

    def queue_descriptor(self, queue: str) -> QueueDescriptor | None:
        """The advertised descriptor for ``queue`` (mailbox + domain), or ``None``
        when no connected console staffs it."""
        return self._queues.get(queue)

    # ── email-verify signup: outbox dispatch + verify ─────────────────────────
    # The web sign-up door writes a pending row then asks the hub to dispatch the
    # verify mail; the hub claims the queue's pending sends for a connected console
    # and pushes a send_verify frame (the console sends it as the queue mailbox).
    # The console acks with verify_sent → the row flips to sent (or back to pending
    # to retry). A console that advertises a queue (re)connecting flushes its
    # backlog. See docs/design/email-queue-signup.md §3.3 / §7.

    @property
    def email_verify_enabled(self) -> bool:
        """Whether the hub can dispatch + verify (store and config both present)."""
        return self._enrollment is not None and self._email_verify is not None

    def list_teams(self) -> list[Team]:
        """The customer teams a visitor may pick at sign-up (Default-seeded). Empty
        when email-verify isn't configured (no enrollment store)."""
        return self._enrollment.list_teams() if self._enrollment is not None else []

    def sync_teams(self, teams: list[Team]) -> None:
        """Reconcile the team list to ``teams`` authoritatively (the config-file boot
        path). No-op when email-verify isn't configured or ``teams`` is empty."""
        if self._enrollment is not None and teams:
            self._enrollment.sync_teams(teams)

    def find_member(self, email: str) -> Member | None:
        """The verified member for ``email`` in this room's single queue, or ``None``.
        Drives the "welcome back" login shortcut — a known email re-logs in with one
        click, reusing its stored name + team. ``None`` when email-verify isn't
        configured."""
        if self._enrollment is None:
            return None
        return self._enrollment.get_member(email, DEFAULT_QUEUE)

    def add_team(self, name: str) -> list[Team]:
        """Create (or rename, if the slug collides) a team from a display ``name``;
        the slug is derived. A name with no usable slug is ignored. Returns the full
        list. No-op (``[]``) when email-verify isn't configured."""
        if self._enrollment is None:
            return []
        slug = make_team_slug(name)
        if slug:
            self._enrollment.add_team(Team(slug=slug, name=name.strip()))
        return self._enrollment.list_teams()

    def remove_team(self, slug: str) -> list[Team]:
        """Remove a team by slug (the protected ``Default`` refuses) and return the
        full list. No-op (``[]``) when email-verify isn't configured."""
        if self._enrollment is None:
            return []
        self._enrollment.remove_team(slug)
        return self._enrollment.list_teams()

    async def on_advertise(
        self, agent: Agent, queue: str, queue_email: str, display_name: str
    ) -> None:
        """Record a queue ``agent`` staffs and flush any pending verify mail for it
        to this console. Refuses an advertisement whose queue doesn't match the
        agent's namespace — a console can only advertise the queue it subscribes to."""
        if queue_namespace(queue) != agent.namespace:
            logger.warning(
                "room: agent %r advertised %r outside its namespace %r — ignored",
                agent.user,
                queue,
                agent.namespace,
            )
            return
        self.advertise_queue(agent.user, queue, queue_email, display_name)
        await self._flush_queue(queue)

    async def submit_signup(self, email: str, name: str, team: str = "") -> None:
        """Record a pending signup for the room's single queue (``DEFAULT_QUEUE``)
        and dispatch its verify mail (now if a console staffs it, else left in the
        outbox for the next console). ``team`` is the visitor's chosen department,
        carried through to the member. No-op when email-verify isn't configured."""
        if self._enrollment is None:
            return
        self._enrollment.add_pending(
            PendingSignup(
                nonce=make_nonce(),
                email=email,
                name=name,
                queue=DEFAULT_QUEUE,
                created_at=now_ts(),
                team=team,
            )
        )
        await self._flush_queue(DEFAULT_QUEUE)

    def verify_token(self, token: str) -> Member | None:
        """Validate a clicked verify link and enrol the member: check the signed,
        in-TTL token, **burn** its single-use nonce, and persist the member. Returns
        the :class:`Member` on success, or ``None`` for a bad / expired / already-used
        link. No-op (``None``) when email-verify isn't configured."""
        if self._enrollment is None or self._email_verify is None:
            return None
        payload = read_verify_token(
            self._email_verify.session_secret, token, self._email_verify.token_ttl
        )
        nonce = payload.get("nonce", "") if isinstance(payload, dict) else ""
        pending = self._enrollment.consume_pending(nonce) if nonce else None
        if pending is None:
            return None
        member = Member(
            email=pending.email,
            queue=pending.queue,
            name=pending.name,
            verified_at=now_ts(),
            team=pending.team,
        )
        self._enrollment.add_member(member)
        logger.info("room: %r verified into %r", member.email, member.queue)
        return member

    async def sweep_expired_signups(self) -> int:
        """Drop pending signups whose verify link has lapsed (older than the token
        TTL) — they're un-verifiable, so the user re-signs. ``sent`` rows are kept as
        the audit trail. Returns how many were removed; ``0`` when email-verify isn't
        configured. Runs the SQLite delete off the event loop."""
        if self._enrollment is None or self._email_verify is None:
            return 0
        return await asyncio.to_thread(
            self._enrollment.sweep_expired, float(self._email_verify.token_ttl)
        )

    async def run_signup_sweeper(self, interval: float = SWEEP_INTERVAL) -> None:
        """Periodically :meth:`sweep_expired_signups` until cancelled — the app
        lifespan owns this task. No-op (returns immediately) when email-verify isn't
        configured or ``interval`` is non-positive."""
        if not self.email_verify_enabled or interval <= 0:
            return
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                removed = await self.sweep_expired_signups()
                if removed:
                    logger.info("room: swept %d expired pending signup(s)", removed)
            except Exception:
                logger.exception("room: pending-signup sweep failed")

    async def on_verify_sent(self, nonce: str, ok: bool) -> None:
        """Apply a console's ``verify_sent`` ack: mark the outbox row ``sent`` on
        success, or return it to ``pending`` to retry elsewhere on failure."""
        if self._enrollment is None:
            return
        if ok:
            self._enrollment.mark_sent(nonce)
        else:
            self._enrollment.release_claim(nonce)

    async def _flush_queue(self, queue: str) -> None:
        """Claim every pending verify send for ``queue`` for one connected console of
        it and push a ``send_verify`` frame for each. No console / no advertised
        mailbox → the rows stay pending for a later flush."""
        if self._enrollment is None or self._email_verify is None:
            return
        descriptor = self._queues.get(queue)
        if descriptor is None:  # nobody advertises this queue right now
            return
        async with self._lock:
            agents = list(self._agents.get(queue_namespace(queue), ()))
        if not agents:
            return
        agent = agents[0]  # any connected console of the queue can send the mail
        claimed = self._enrollment.claim_pending_sends(queue, agent.user)
        for row in claimed:
            token = sign_verify_token(
                self._email_verify.session_secret,
                {
                    "nonce": row.nonce,
                    "email": row.email,
                    "name": row.name,
                    "queue": row.queue,
                },
            )
            verify_url = (
                self._email_verify.public_url.rstrip("/") + f"/verify?token={token}"
            )
            await _safe_send(
                agent,
                send_verify_frame(
                    row.nonce,
                    row.email,
                    row.name,
                    queue,
                    descriptor.queue_email,
                    verify_url,
                ),
            )

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

    async def _try_assign(
        self, req: Request, *, exclude: set[str] | None = None
    ) -> bool:
        """Route an ``unassigned`` request to an on-shift holder by round-robin and
        announce it; ``False`` (left unassigned) when no on-shift holder exists.
        ``exclude`` drops operators from the candidate pool — used on an AFK release
        so a stranded request isn't handed straight back to the console that
        ignored it."""
        candidates = self._live.holders(req.namespace, req.runnable)
        if exclude:
            candidates = [c for c in candidates if c not in exclude]
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
        self._schedule_afk(assigned.id)  # auto-release if the console never picks it up
        return True

    # ── AFK release timer ─────────────────────────────────────────────────────
    # An assigned request whose console never sends request_start is a stranded
    # visitor. After ``assignment_timeout`` we release it back to the queue (which
    # re-pages / re-assigns). The timer is cancelled the moment the request moves
    # off ``assigned`` (start / complete / a manual release). See §3.5 / §6.2.

    def _schedule_afk(self, request_id: str) -> None:
        if self._assignment_timeout <= 0:
            return
        self._cancel_afk(request_id)
        self._afk_timers[request_id] = asyncio.create_task(
            self._afk_countdown(request_id)
        )

    def _cancel_afk(self, request_id: str) -> None:
        task = self._afk_timers.pop(request_id, None)
        if task is not None:
            task.cancel()

    async def _afk_countdown(self, request_id: str) -> None:
        try:
            await asyncio.sleep(self._assignment_timeout)
        except asyncio.CancelledError:
            return
        # We're firing — drop our own handle first so release_request's cancel is a
        # no-op (we must not cancel the running task).
        self._afk_timers.pop(request_id, None)
        req = self._requests.get(request_id)
        if req is not None and req.state == ASSIGNED:
            logger.info("room: request %s AFK — releasing to the queue", request_id)
            await self.release_request(request_id)

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
        frame = page_frame(
            req.id,
            req.room,
            req.runnable,
            req.queue,
            req.visitor,
            req.visitor_name,
            req.team,
        )
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
        self,
        p: Participant,
        tool: str,
        args: dict[str, Any],
        kind: str = "runnable",
        host: str = "",
    ) -> Request | None:
        """A session submits a portal request from their conversation. Validated
        against the **live catalogue** (some connected console must hold ``tool`` —
        on shift or, tolerating the off-shift race, at least pageable) and the args
        budget. **Authorization is visibility**: an operator-audience entry is only
        requestable by an operator session (``p.operator``), so a visitor can never
        submit what they can't see. ``kind`` distinguishes a runnable request from a
        ``skill`` (runbook) one; ``host`` is the operator's chosen target host for a
        fleet runnable (opaque here — the enacting console resolves it). Both ride
        onto the request for the console to dispatch. Persists the request as a
        ``request``-kind thread message, then **auto-assigns** to an on-shift holder
        (round-robin) or holds it ``unassigned`` for a page. Returns the
        :class:`Request` (with its resolved state), or ``None`` when refused."""
        parsed = parse_conversation_room(p.room)
        if parsed is None:
            logger.warning("room: portal request from non-conversation room %r", p.room)
            return None
        queue, visitor = parsed
        ns = queue_namespace(queue)
        entry = self._live.entry_for(ns, tool)
        if entry is None:
            logger.warning(
                "room: %r requested %r — no connected console holds it in %r",
                p.user,
                tool,
                ns,
            )
            return None
        if not visible_to(entry, operator=bool(getattr(p, "operator", False))):
            logger.warning(
                "room: %r requested operator-only %r without operator access in %r",
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
        # The visitor's display name + team (email-verify signup) ride onto the
        # operator card; a connection without them (dev / console) just shows the id.
        visitor_name = str(getattr(p, "display_name", "") or "")
        team = str(getattr(p, "team", "") or "")
        req = self._requests.create(
            p.room,
            queue,
            ns,
            visitor,
            tool,
            args,
            visitor_name=visitor_name,
            team=team,
            kind=kind,
            host=host,
        )
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
            self._cancel_afk(request_id)  # the console picked it up — not AFK
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
            self._cancel_afk(request_id)  # terminal — no AFK release
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
        prev = req.assignee  # don't immediately re-hand it to the AFK console
        self._cancel_afk(request_id)  # no-op when the AFK timer itself called us
        released = self._requests.release(request_id)
        if released is None:
            return
        exclude = {prev} if prev else None
        if not await self._try_assign(released, exclude=exclude):
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
