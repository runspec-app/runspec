"""
store.py — the durable message transcript (SQLite, stdlib only).

A thin wrapper over ``sqlite3``: append a sent message, and read back the most
recent N for a room (the browser backscroll — consoles never ask, by the
forward-only contract). Kept deliberately tiny and synchronous; the hub calls it
off the event loop via ``asyncio.to_thread`` so SQLite I/O never blocks
broadcasting. See docs/design/console-room-server.md §4.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import Message


class TranscriptStore:
    """SQLite-backed transcript. One connection, guarded by a lock so the
    ``to_thread`` workers the hub dispatches from can share it safely
    (``check_same_thread=False`` + the lock serialises access)."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        path = str(db_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id     TEXT PRIMARY KEY,
                    room   TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    text   TEXT NOT NULL,
                    ts     REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_room_ts ON messages (room, ts)"
            )
            self._conn.commit()

    def append(self, msg: Message) -> None:
        """Persist a sent message. Idempotent on ``id`` (a re-delivered id is
        ignored rather than raising)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO messages (id, room, sender, text, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (msg.id, msg.room, msg.sender, msg.text, msg.ts),
            )
            self._conn.commit()

    def recent(self, room: str, limit: int = 50) -> list[Message]:
        """The most recent ``limit`` messages in ``room``, oldest-first (so a
        browser can render them top-to-bottom as backscroll)."""
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, room, sender, text, ts FROM messages "
                "WHERE room = ? ORDER BY ts DESC, id DESC LIMIT ?",
                (room, limit),
            ).fetchall()
        msgs = [
            Message(id=r[0], room=r[1], sender=r[2], text=r[3], ts=r[4]) for r in rows
        ]
        msgs.reverse()  # newest-N fetched DESC → return chronological
        return msgs

    def close(self) -> None:
        with self._lock:
            self._conn.close()
