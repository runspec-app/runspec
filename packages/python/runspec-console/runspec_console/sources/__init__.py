"""
sources — concrete event sources for triggers.

Importing this package registers the built-in sources (each module calls
``runspec_console.events.register_source`` on import). Source modules keep all
OS-specific imports lazy, so importing them is safe on any platform; a source
that can't run here simply reports ``available() -> (False, reason)``.
"""

from __future__ import annotations

from . import chat  # noqa: F401 — registers the "chat" source
from . import notifications  # noqa: F401 — registers the "notifications" source
from . import outlook  # noqa: F401 — registers the "outlook" source

__all__ = ["chat", "notifications", "outlook"]
