"""
test_hub.py — the broker: join/leave presence, message stamping + echo, presence
relay, and the browser-only backscroll asymmetry. Driven with fake participants
and a fake store (no sockets, no SQLite); run via asyncio.run so no pytest-asyncio
dependency is needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from console_room.hub import Hub
from console_room.models import Message


def run(coro):
    return asyncio.run(coro)


class FakeStore:
    def __init__(self, seed: list[Message] | None = None) -> None:
        self.appended: list[Message] = []
        self._seed = list(seed or [])

    def append(self, msg: Message) -> None:
        self.appended.append(msg)

    def recent(self, room: str, limit: int) -> list[Message]:
        msgs = [m for m in self._seed if m.room == room]
        return msgs[-limit:] if limit > 0 else []


class FakeP:
    def __init__(self, user: str, room: str, wants_backscroll: bool = False) -> None:
        self.user = user
        self.room = room
        self.wants_backscroll = wants_backscroll
        self.frames: list[dict[str, Any]] = []

    async def send(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


def _presences(p: FakeP) -> list[tuple[str, str]]:
    return [(f["user"], f["status"]) for f in p.frames if f["type"] == "presence"]


def _messages(p: FakeP) -> list[str]:
    return [f["text"] for f in p.frames if f["type"] == "message"]


# ── presence on join / leave ──────────────────────────────────────────────────


def test_join_announces_and_catches_up():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeP("a", "ops")
        b = FakeP("b", "ops")
        await hub.join(a)  # alone — no one to hear or learn from
        await hub.join(b)  # arrives second
        return a, b

    a, b = run(scenario())
    # a was told b became available; b learned a was already present.
    assert _presences(a) == [("b", "available")]
    assert _presences(b) == [("a", "available")]


def test_leave_broadcasts_away():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeP("a", "ops")
        b = FakeP("b", "ops")
        await hub.join(a)
        await hub.join(b)
        a.frames.clear()
        b.frames.clear()
        await hub.leave(b)
        return hub, a

    hub, a = run(scenario())
    assert _presences(a) == [("b", "away")]
    assert hub.room_size("ops") == 1


def test_leave_when_not_joined_is_silent():
    async def scenario():
        hub = Hub(FakeStore())
        ghost = FakeP("ghost", "ops")
        await hub.leave(ghost)  # never joined → no broadcast, no crash
        return ghost

    ghost = run(scenario())
    assert ghost.frames == []


# ── messages ──────────────────────────────────────────────────────────────────


def test_message_persisted_and_echoed_to_all():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        a = FakeP("a", "ops")
        b = FakeP("b", "ops")
        await hub.join(a)
        await hub.join(b)
        a.frames.clear()
        b.frames.clear()
        await hub.on_message(a, "deploying now")
        return store, a, b

    store, a, b = run(scenario())
    assert len(store.appended) == 1
    saved = store.appended[0]
    assert saved.sender == "a" and saved.text == "deploying now" and saved.id
    # Echoed to the sender too (so all views converge on the server id/ts).
    assert _messages(a) == ["deploying now"]
    assert _messages(b) == ["deploying now"]
    assert a.frames[0]["sender"] == "a" and a.frames[0]["id"] == saved.id


def test_message_only_reaches_its_room():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeP("a", "ops")
        other = FakeP("c", "infra")
        await hub.join(a)
        await hub.join(other)
        a.frames.clear()
        other.frames.clear()
        await hub.on_message(a, "ops only")
        return a, other

    a, other = run(scenario())
    assert _messages(a) == ["ops only"]
    assert other.frames == []  # different room hears nothing


# ── presence relay ────────────────────────────────────────────────────────────


def test_on_presence_relays_to_others_not_self():
    async def scenario():
        hub = Hub(FakeStore())
        a = FakeP("a", "ops")
        b = FakeP("b", "ops")
        await hub.join(a)
        await hub.join(b)
        a.frames.clear()
        b.frames.clear()
        await hub.on_presence(a, "away")  # a's rota flipped it off-shift
        return a, b

    a, b = run(scenario())
    assert _presences(a) == []  # never echoed to self
    assert _presences(b) == [("a", "away")]


# ── backscroll asymmetry ──────────────────────────────────────────────────────


def test_browser_gets_backscroll_console_does_not():
    seed = [
        Message(room="ops", sender="x", text="old1", id="i1", ts=1.0),
        Message(room="ops", sender="y", text="old2", id="i2", ts=2.0),
    ]

    async def scenario():
        hub = Hub(FakeStore(seed))
        console = FakeP("c", "ops", wants_backscroll=False)
        browser = FakeP("p", "ops", wants_backscroll=True)
        await hub.join(console)
        await hub.join(browser)
        return console, browser

    console, browser = run(scenario())
    assert _messages(console) == []  # forward-only: no history
    assert _messages(browser) == ["old1", "old2"]  # chronological backscroll
