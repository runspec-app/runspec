"""
conversation.py — naming + namespace matching for helpdesk conversations.

A helpdesk conversation is a room whose name is derived **server-side** from the
queue and the visitor's identity, so a visitor can never name (and therefore
never read) another visitor's thread:

    helpdesk/<queue>/<visitor>

An agent (console) subscribes to a queue *namespace* — ``helpdesk/<queue>`` — and
the hub fans every conversation under it to that agent (its inbox of threads).
These helpers are pure (no I/O), so the naming / matching rules are unit-tested on
their own. The hub itself stays generic — it only knows prefix matching; the
``helpdesk/`` convention lives here and in the doors. See
docs/design/helpdesk-rooms.md §3.
"""

from __future__ import annotations

#: Top-level prefix marking a room as a helpdesk conversation (vs a group-chat room).
HELPDESK_PREFIX = "helpdesk"

#: The single-queue default. One room = one helpdesk = one queue (its identity is
#: the room's ``--mailbox``); this is the internal queue name visitors enrol into
#: and consoles subscribe to. Must match the console's
#: ``chat_transport.DEFAULT_QUEUE``.
DEFAULT_QUEUE = "helpdesk"

#: The room-name path separator.
SEP = "/"


def valid_token(token: str) -> bool:
    """True if ``token`` is usable as a queue or visitor path segment.

    A token must be non-empty and contain no separator or whitespace — those
    would let a crafted queue / visitor span path segments and escape its
    namespace, which is the whole isolation guarantee. Identity characters
    (``@ . - _ +``) are fine; only structure-breaking characters are rejected.
    """
    if not isinstance(token, str) or not token:
        return False
    return not any(c == SEP or c.isspace() for c in token)


def queue_namespace(queue: str) -> str:
    """The namespace an agent subscribes to for ``queue``: ``helpdesk/<queue>``."""
    return f"{HELPDESK_PREFIX}{SEP}{queue}"


def conversation_room(queue: str, visitor: str) -> str:
    """The room name for ``visitor``'s private conversation in ``queue``."""
    return f"{HELPDESK_PREFIX}{SEP}{queue}{SEP}{visitor}"


def parse_conversation_room(room: str) -> tuple[str, str] | None:
    """Split a conversation room into ``(queue, visitor)``, or ``None`` if ``room``
    is not a ``helpdesk/<queue>/<visitor>`` name."""
    parts = room.split(SEP)
    if len(parts) != 3 or parts[0] != HELPDESK_PREFIX:
        return None
    _, queue, visitor = parts
    if not queue or not visitor:
        return None
    return queue, visitor


def namespace_matches(namespace: str, room: str) -> bool:
    """True if ``room`` falls under ``namespace`` — i.e. an agent subscribed to
    ``namespace`` should receive ``room``'s traffic.

    Prefix match on a **segment boundary**, so ``helpdesk/it`` matches
    ``helpdesk/it/alice`` but not ``helpdesk/it-eu/bob``."""
    return room == namespace or room.startswith(namespace + SEP)
