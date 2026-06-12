"""
mailbox_claim.py — the email channel's claim + lifecycle, as a validated fold.

A shared mailbox has no hub the way a chat room does, so claim coordination is
done *through the mailbox itself*. Claiming sends a tiny ack email addressed to
the queue address (not the requester) carrying a machine marker; every staffing
console sees that ack land and converges on the same verdict — **first ack
wins**. Lifecycle (resolve / reopen / close) rides the same marker scheme, so the
email thread *is* the event log.

The current state of a conversation is therefore a **validated fold** of its
ordered events:

  - the earliest valid claim-ack → owner (state ``assigned``);
  - a lifecycle marker counts only when it comes from the current owner;
  - a requester reply after resolve/close reopens it.

This is the same lifecycle the chat side gets from its hub, but reconstructed
locally from the thread instead of pushed by a server. Two consoles reading the
same mailbox, sorting by the same server order, reach the same verdict with zero
coordination.

This module is deliberately **pure** (no COM, stdlib only): markers are
formatted/parsed as strings and folds run over plain event dicts, so the whole
keystone is unit-tested without win32. The COM send (the ack email) and the
new_mail ingestion live in the bridge / sources.outlook. See
docs/design/helpdesk-channels.md.
"""

from __future__ import annotations

import email.parser
from dataclasses import dataclass

# The four control kinds a marker can carry. A claim-ack establishes ownership;
# the rest are lifecycle transitions, valid only from the current owner.
KINDS = ("claim", "resolve", "reopen", "close")

# The custom internet header that tags a marker email. Set on send via the
# PS_INTERNET_HEADERS named property; read back from the recipient's transport
# header block. Matched case-insensitively (transport may re-case it).
MARKER_HEADER = "X-Runspec-Claim"

# Lifecycle states, mirroring the chat side (see Bridge._QUEUE_STATE_COLUMN).
OPEN, ASSIGNED, RESOLVED, CLOSED = "open", "assigned", "resolved", "closed"


@dataclass(frozen=True)
class Marker:
    """A parsed claim/lifecycle marker. Self-contained (carries its own queue,
    conversation and agent) so a fold never needs anything but the markers."""

    kind: str  # one of KINDS
    queue: str
    conversation_id: str
    agent: str  # the staffing agent that produced the marker (e.g. "ana@web01")


def format_marker(marker: Marker) -> str:
    """The ``X-Runspec-Claim`` header *value*: ``<queue>/<conversationId>/<agent>/
    <kind>``. queue/conversationId/agent never contain ``/`` (a queue name, an
    Outlook ConversationID hex, an ``user@host`` identity), so ``/`` is an
    unambiguous delimiter."""
    return f"{marker.queue}/{marker.conversation_id}/{marker.agent}/{marker.kind}"


def parse_marker(value: str) -> Marker | None:
    """Parse an ``X-Runspec-Claim`` header value back into a :class:`Marker`, or
    ``None`` if it is malformed / carries an unknown kind (so a stray header can
    never be mistaken for a control event)."""
    parts = str(value or "").strip().split("/")
    if len(parts) != 4:
        return None
    queue, conversation_id, agent, kind = (p.strip() for p in parts)
    if not (queue and conversation_id and agent) or kind not in KINDS:
        return None
    return Marker(kind=kind, queue=queue, conversation_id=conversation_id, agent=agent)


def marker_from_headers(blob: str) -> str | None:
    """The ``X-Runspec-Claim`` header value out of a raw transport-header block,
    or ``None`` when absent. Pure stdlib; the caller pairs it with
    :func:`parse_marker`. Empty for an ordinary (non-marker) email."""
    if not blob.strip():
        return None
    try:
        msg = email.parser.Parser().parsestr(blob, headersonly=True)
    except Exception:
        return None
    value = msg.get(MARKER_HEADER)  # case-insensitive per RFC 5322
    return value.strip() if value else None


def fold_conversation(events: list[dict]) -> dict:
    """Replay a conversation's events in server order → its current ``{state,
    owner}``. The keystone: every console folding the same mailbox in the same
    order lands on the same verdict, so claim/lifecycle need no coordination.

    Each event is a dict with:
      - ``order_key``: a total-order sort key (the server ``receivedDateTime`` +
        ``internetMessageId`` — *server* order, never local clocks, so two
        consoles agree);
      - ``kind``: one of KINDS, or ``"message"`` for a requester email;
      - ``agent``: the acting agent (markers only);
      - ``from_requester``: True for a requester email (drives reopen).

    Rules (validated fold, not last-writer-wins):
      - the **earliest** claim-ack wins ownership; later claims are ignored
        (idempotent — two agents racing Claim converge on the first);
      - ``resolve`` / ``close`` apply only from the current owner;
      - ``reopen`` applies only from the owner, and only from resolved/closed;
      - a requester reply after resolve/close reopens (→ assigned if owned).
    """
    state, owner = OPEN, None
    for event in sorted(events, key=lambda e: e["order_key"]):
        kind = event.get("kind")
        agent = event.get("agent")
        if kind == "claim":
            if owner is None:  # first valid ack wins; subsequent claims are no-ops
                owner, state = agent, ASSIGNED
        elif kind in ("resolve", "close"):
            if owner is not None and agent == owner:
                state = RESOLVED if kind == "resolve" else CLOSED
        elif kind == "reopen":
            if owner is not None and agent == owner and state in (RESOLVED, CLOSED):
                state = ASSIGNED
        elif kind == "message" and event.get("from_requester"):
            if state in (RESOLVED, CLOSED):
                state = ASSIGNED if owner else OPEN
    return {"state": state, "owner": owner}


def owner_of(events: list[dict]) -> str | None:
    """Convenience: the current owner (the read-after-write ownership check —
    'do I see my own ack as the earliest valid claim?')."""
    return fold_conversation(events)["owner"]
