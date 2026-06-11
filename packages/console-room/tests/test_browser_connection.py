"""
test_browser_connection.py — the browser participant's logic, with a fake
WebSocket (no Starlette). Mirrors the NDJSON connection tests on the other door:
join → dispatch → leave, message send, presence relay, and the browser-only
backscroll behaviour (wants_backscroll is True).
"""

from __future__ import annotations

import asyncio
from typing import Any

from console_room.hub import Hub
from console_room.models import Message
from console_room.web.connection import BrowserConnection


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


class FakeWS:
    """A scripted WebSocketLike: yields the queued inbound frames, then None
    (close). Captures everything sent back."""

    def __init__(self, inbound: list[dict[str, Any]]) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, obj: dict[str, Any]) -> None:
        self.sent.append(obj)

    async def receive(self) -> dict[str, Any] | None:
        if self._inbound:
            return self._inbound.pop(0)
        return None

    async def close(self) -> None:
        self.closed = True


class FakeP:
    def __init__(self, user: str, room: str) -> None:
        self.user = user
        self.room = room
        self.wants_backscroll = False
        self.frames: list[dict[str, Any]] = []

    async def send(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


def test_browser_gets_backscroll_on_join():
    seed = [
        Message(room="ops", sender="x", text="old1", id="i1", ts=1.0),
        Message(room="ops", sender="y", text="old2", id="i2", ts=2.0),
    ]

    async def scenario():
        hub = Hub(FakeStore(seed))
        ws = FakeWS([])  # no inbound; closes immediately
        await BrowserConnection(ws, user="ana", room="ops").run(hub)
        return ws

    ws = run(scenario())
    texts = [f["text"] for f in ws.sent if f["type"] == "message"]
    assert texts == ["old1", "old2"]  # history replayed, chronological


def test_browser_message_persisted_and_broadcast():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        observer = FakeP("console", "ops")
        await hub.join(observer)
        observer.frames.clear()
        ws = FakeWS([{"type": "message", "room": "ops", "text": "from the browser"}])
        await BrowserConnection(ws, user="ana", room="ops").run(hub)
        return store, observer

    store, observer = run(scenario())
    assert len(store.appended) == 1 and store.appended[0].sender == "ana"
    assert [f["text"] for f in observer.frames if f["type"] == "message"] == [
        "from the browser"
    ]


def test_browser_leave_clears_presence():
    async def scenario():
        hub = Hub(FakeStore())
        observer = FakeP("console", "ops")
        await hub.join(observer)
        observer.frames.clear()
        await BrowserConnection(FakeWS([]), user="ana", room="ops").run(hub)
        return hub, observer

    hub, observer = run(scenario())
    statuses = [
        (f["user"], f["status"]) for f in observer.frames if f["type"] == "presence"
    ]
    assert ("ana", "available") in statuses
    assert statuses[-1] == ("ana", "away")  # left on socket close
    assert hub.room_size("ops") == 1


def test_browser_presence_frame_relayed():
    async def scenario():
        hub = Hub(FakeStore())
        observer = FakeP("console", "ops")
        await hub.join(observer)
        observer.frames.clear()
        ws = FakeWS([{"type": "presence", "room": "ops", "status": "away"}])
        await BrowserConnection(ws, user="ana", room="ops").run(hub)
        return observer

    observer = run(scenario())
    statuses = [
        (f["user"], f["status"]) for f in observer.frames if f["type"] == "presence"
    ]
    assert ("ana", "away") in statuses


def test_browser_ignores_blank_and_unknown_frames():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        ws = FakeWS(
            [
                {},  # skipped (e.g. non-JSON adapted to {})
                {"type": "bogus", "room": "ops"},  # unknown type
                {"type": "message", "room": "ops", "text": ""},  # empty text
                {"type": "message", "room": "ops", "text": "real"},
            ]
        )
        await BrowserConnection(ws, user="ana", room="ops").run(hub)
        return store

    store = run(scenario())
    assert [m.text for m in store.appended] == ["real"]
