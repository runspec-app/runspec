"""
lifecycle.py — the conversation state machine for helpdesk threads (pure).

A helpdesk conversation moves through four states::

        visitor message                 agent claim
    · ─────────────────▶ open ───────────────────────▶ assigned
                          ▲  ▲                              │
            visitor reply │  │ visitor reply (reopen)       │ agent resolve
            (reopen)      │  │                              ▼
                       closed ◀───── agent close ───── resolved
                          ▲                                 │
                          └───────── agent close ───────────┘

- **open** — a live thread, unassigned. Broadcast to the queue's agents as a
  notice; the agent's LLM turn is deliberately *deferred* until a claim, so spend
  tracks a human claim (the "claim-gated turn").
- **assigned** — an agent claimed it; that agent owns it, the team sees it's taken.
- **resolved** — an agent marked it done. A visitor reply reopens it (→ open), so
  they are never stuck.
- **closed** — terminal. A visitor reply still reopens it; otherwise the transcript
  is retained and a fresh thread is what a brand-new message would start.

This module is pure (no I/O, no sockets): the ``ConversationRegistry`` is just an
in-memory map of room → :class:`Conversation` with the transition rules, so the
state machine is unit-tested on its own. The hub owns one registry and broadcasts
the ``conversation`` frames the transitions produce. See
docs/design/helpdesk-rooms.md §4.
"""

from __future__ import annotations

from dataclasses import dataclass

from .conversation import namespace_matches

OPEN = "open"
ASSIGNED = "assigned"
RESOLVED = "resolved"
CLOSED = "closed"

STATES = (OPEN, ASSIGNED, RESOLVED, CLOSED)
#: States in which a fresh visitor message is just a message (not a reopen).
LIVE = (OPEN, ASSIGNED)
#: States a visitor message reopens.
DORMANT = (RESOLVED, CLOSED)


@dataclass
class Conversation:
    """The lifecycle record for one conversation room (live, in-memory)."""

    room: str
    state: str = OPEN
    assignee: str | None = None

    def frame_fields(self) -> tuple[str, str, str | None]:
        return self.room, self.state, self.assignee


class ConversationRegistry:
    """In-memory room → :class:`Conversation` map applying the transition rules.

    Every mutating method returns the :class:`Conversation` **iff the state
    changed** (so the hub knows whether to broadcast), or ``None`` for a no-op /
    refused transition. Methods are deterministic and side-effect-free beyond the
    map itself, so they're tested directly."""

    def __init__(self) -> None:
        self._convs: dict[str, Conversation] = {}

    def get(self, room: str) -> Conversation | None:
        return self._convs.get(room)

    def open_or_reopen(self, room: str) -> Conversation | None:
        """Visitor activity in ``room``: create it (open) if new, or reopen it if
        it was resolved/closed. No change if it's already live."""
        conv = self._convs.get(room)
        if conv is None:
            conv = Conversation(room=room, state=OPEN)
            self._convs[room] = conv
            return conv
        if conv.state in DORMANT:
            conv.state = OPEN
            conv.assignee = None
            return conv
        return None  # already live

    def claim(self, room: str, agent: str) -> Conversation | None:
        """An agent claims ``room`` → assigned to that agent. Refused once closed
        (a visitor must reopen first); idempotent if the same agent re-claims."""
        conv = self._convs.setdefault(room, Conversation(room=room))
        if conv.state == CLOSED:
            return None
        if conv.state == ASSIGNED and conv.assignee == agent:
            return None
        conv.state = ASSIGNED
        conv.assignee = agent
        return conv

    def resolve(self, room: str) -> Conversation | None:
        """An agent marks ``room`` resolved. Keeps the assignee (who resolved it).
        No-op if unknown or already resolved/closed."""
        conv = self._convs.get(room)
        if conv is None or conv.state in DORMANT:
            return None
        conv.state = RESOLVED
        return conv

    def reopen(self, room: str) -> Conversation | None:
        """Reopen a resolved/closed ``room`` → open, unassigned (back to the
        queue). No-op if unknown or already live."""
        conv = self._convs.get(room)
        if conv is None or conv.state not in DORMANT:
            return None
        conv.state = OPEN
        conv.assignee = None
        return conv

    def close(self, room: str) -> Conversation | None:
        """An agent closes ``room`` → terminal. No-op if unknown or already
        closed."""
        conv = self._convs.get(room)
        if conv is None or conv.state == CLOSED:
            return None
        conv.state = CLOSED
        return conv

    def active_under(self, namespace: str) -> list[Conversation]:
        """Every known conversation whose room falls under ``namespace`` — an
        agent's inbox of threads (any state, so a console can show resolved ones
        too). Order is insertion order."""
        return [c for c in self._convs.values() if namespace_matches(namespace, c.room)]
