"""
console_room — the runspec chat-room server.

A small "dumb message pipe with presence" the consoles connect to: it holds, per
room, the connected participants, their live presence, and a durable transcript,
and broadcasts every message / presence change. All intelligence stays in the
consoles. See docs/design/console-room-server.md.

S1 (this release) serves the console NDJSON-localhost door; the browser door and
OIDC/TLS land in later slices.
"""

from __future__ import annotations

from .hub import Hub, Participant
from .models import Message, presence_frame
from .store import TranscriptStore

__version__ = "0.3.0"

__all__ = [
    "Hub",
    "Participant",
    "Message",
    "TranscriptStore",
    "presence_frame",
    "__version__",
]
