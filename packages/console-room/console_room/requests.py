"""
requests.py — the claim-free portal request lifecycle (pure).

A portal request is a visitor asking for one runnable from the live catalogue. It
moves through a small, **claim-free** state machine — assignment is the hub's job,
not a human's:

    submit ─▶ unassigned ──auto-assign──▶ assigned ──operator starts──▶ in_progress
                  ▲                            │                              │
       release   │      (re)auto-assign        │      complete(ok / error)    │
       (timeout) └─────────────────────────────┘                             ▼
                                                                       done / failed

- **unassigned** — accepted but no *on-shift* operator holds it yet; a page targets
  the connected (incl. off-shift) holders, and it auto-assigns the moment an
  eligible operator comes on shift.
- **assigned** — auto-routed to an on-shift operator (round-robin among the
  holders). No claim step.
- **in_progress** — the operator is enacting it through the autonomy gate.
- **done / failed** — terminal; the result is delivered on the request thread.

Assignment is automatic and fair: **plain round-robin** among the on-shift holders
of the runnable (no load tracking in v1; a single holder always gets it). This
module is pure — the hub owns one registry, supplies the candidate list from the
live catalogue, and broadcasts the frames the transitions produce. See
docs/design/runnables-portal.md §3.3 / §3.5 / §6.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

UNASSIGNED = "unassigned"
ASSIGNED = "assigned"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"

STATES = (UNASSIGNED, ASSIGNED, IN_PROGRESS, DONE, FAILED)
#: Terminal states — a request here never transitions again.
TERMINAL = (DONE, FAILED)
#: States from which an operator may complete the request (start is optional).
COMPLETABLE = (ASSIGNED, IN_PROGRESS)


@dataclass
class Request:
    """One portal request — a visitor's ask for a single runnable, plus its live
    lifecycle state and assignee."""

    id: str
    room: str  # the visitor's conversation room (the request thread)
    queue: str
    namespace: str
    visitor: str
    runnable: str
    args: dict[str, Any] = field(default_factory=dict)
    # The visitor's self-asserted full name (email-verify signup). Display-only —
    # the verified ``visitor`` email stays the trust anchor. Empty in dev mode.
    visitor_name: str = ""
    state: str = UNASSIGNED
    assignee: str | None = None
    error: str | None = None

    def assignment_fields(self) -> tuple[str, str, str, dict[str, Any], str, str]:
        """The payload an assigned operator's console needs to enact the request."""
        return (
            self.id,
            self.room,
            self.runnable,
            self.args,
            self.visitor,
            self.visitor_name,
        )

    def status_fields(self) -> tuple[str, str, str, str | None, str | None]:
        """The lifecycle fields broadcast to the request thread (visitor + team)."""
        return self.id, self.room, self.state, self.assignee, self.error


class RequestRegistry:
    """In-memory ``id → Request`` map applying the claim-free transition rules.

    Every mutating method returns the :class:`Request` **iff the state changed**
    (so the hub knows whether to broadcast), or ``None`` for a no-op / refused
    transition. Round-robin state is a per-namespace cursor, advanced only on an
    actual assignment, so the rotation is fair and deterministic and the rules are
    unit-tested directly."""

    def __init__(self) -> None:
        self._requests: dict[str, Request] = {}
        self._cursor: dict[str, int] = {}  # namespace -> round-robin position

    def get(self, request_id: str) -> Request | None:
        return self._requests.get(request_id)

    def create(
        self,
        room: str,
        queue: str,
        namespace: str,
        visitor: str,
        runnable: str,
        args: dict[str, Any],
        visitor_name: str = "",
    ) -> Request:
        """Record a fresh request (``unassigned``) and return it. The id is
        server-assigned so the thread message and the lifecycle record agree."""
        req = Request(
            id=uuid.uuid4().hex,
            room=room,
            queue=queue,
            namespace=namespace,
            visitor=visitor,
            runnable=runnable,
            args=dict(args),
            visitor_name=visitor_name,
        )
        self._requests[req.id] = req
        return req

    def next_assignee(self, namespace: str, candidates: list[str]) -> str | None:
        """Pick the next operator from ``candidates`` (the on-shift holders) by
        plain round-robin, advancing the namespace cursor. ``None`` if there are no
        candidates. A single candidate is always chosen."""
        if not candidates:
            return None
        pos = self._cursor.get(namespace, 0)
        choice = candidates[pos % len(candidates)]
        self._cursor[namespace] = pos + 1
        return choice

    def assign(self, request_id: str, operator: str) -> Request | None:
        """Assign an ``unassigned`` request to ``operator`` → ``assigned``. No-op
        if unknown or not currently unassigned (so a double-assign can't steal an
        in-flight request)."""
        req = self._requests.get(request_id)
        if req is None or req.state != UNASSIGNED:
            return None
        req.state = ASSIGNED
        req.assignee = operator
        return req

    def start(self, request_id: str, operator: str) -> Request | None:
        """The assignee begins enacting → ``in_progress``. Refused unless ``req``
        is assigned to exactly ``operator`` (an operator can't start another's
        request)."""
        req = self._requests.get(request_id)
        if req is None or req.state != ASSIGNED or req.assignee != operator:
            return None
        req.state = IN_PROGRESS
        return req

    def complete(
        self, request_id: str, operator: str, error: str | None = None
    ) -> Request | None:
        """The assignee finishes → ``done`` (or ``failed`` when ``error`` is set).
        Allowed from ``assigned`` (a synchronous run never passing through
        ``in_progress``) or ``in_progress``; refused for a non-assignee or a
        terminal request."""
        req = self._requests.get(request_id)
        if req is None or req.state not in COMPLETABLE or req.assignee != operator:
            return None
        req.state = FAILED if error else DONE
        req.error = error
        return req

    def release(self, request_id: str) -> Request | None:
        """Return an ``assigned`` request to the queue → ``unassigned`` (the AFK
        timeout, or an operator going off shift mid-assignment). Clears the
        assignee so it can route afresh. No-op once it's progressing or terminal —
        only a not-yet-started assignment is reclaimed."""
        req = self._requests.get(request_id)
        if req is None or req.state != ASSIGNED:
            return None
        req.state = UNASSIGNED
        req.assignee = None
        return req

    def unassigned_under(self, namespace: str) -> list[Request]:
        """Every pending (``unassigned``) request in ``namespace`` — the backlog an
        operator coming on shift can pick up. Insertion order (oldest first)."""
        return [
            r
            for r in self._requests.values()
            if r.namespace == namespace and r.state == UNASSIGNED
        ]
