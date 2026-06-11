"""web — the browser front door (Starlette WebSocket app + static frontend)."""

from __future__ import annotations

from .connection import BrowserConnection, WebSocketLike

__all__ = ["BrowserConnection", "WebSocketLike"]
