"""
test_ndjson_server.py — the console door's frame handling.

Drives NdjsonConsoleConnection with a fed StreamReader + a fake writer (no real
sockets) against a real Hub + fake store, so the handshake / dispatch / framing
are exercised end-to-end without networking. A second fake participant in the room
receives the broadcasts. Run via asyncio.run (no pytest-asyncio dependency).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from console_room.hub import Hub
from console_room.models import Message
from console_room.ndjson_server import NdjsonConsoleConnection


def run(coro):
    return asyncio.run(coro)


class FakeStore:
    def __init__(self) -> None:
        self.appended: list[Message] = []

    def append(self, msg: Message) -> None:
        self.appended.append(msg)

    def recent(self, room: str, limit: int) -> list[Message]:
        return []


class FakeWriter:
    """Captures written bytes; satisfies the StreamWriter slice the connection
    uses (write / drain / close / wait_closed)."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def frames(self) -> list[dict[str, Any]]:
        text = b"".join(self.chunks).decode("utf-8")
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


class FakeP:
    def __init__(self, user: str, room: str) -> None:
        self.user = user
        self.room = room
        self.wants_backscroll = False
        self.frames: list[dict[str, Any]] = []

    async def send(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


def _reader(*frames: dict[str, Any]) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    for f in frames:
        r.feed_data((json.dumps(f) + "\n").encode("utf-8"))
    r.feed_eof()
    return r


# ── handshake ─────────────────────────────────────────────────────────────────


def test_no_hello_closes_without_joining():
    async def scenario():
        hub = Hub(FakeStore())
        reader = _reader({"type": "message", "room": "ops", "text": "x"})
        writer = FakeWriter()
        await NdjsonConsoleConnection(reader, writer).run(hub)
        return hub, writer

    hub, writer = run(scenario())
    assert hub.room_size("ops") == 0  # never joined
    assert writer.closed and writer.frames() == []


def test_hello_then_message_is_stamped_and_broadcast():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        observer = FakeP("b", "ops")
        await hub.join(observer)
        observer.frames.clear()
        reader = _reader(
            {"type": "hello", "room": "ops", "user": "alice@web01"},
            {"type": "message", "room": "ops", "text": "rolling out"},
        )
        writer = FakeWriter()
        await NdjsonConsoleConnection(reader, writer).run(hub)
        return store, writer, observer

    store, writer, observer = run(scenario())
    # Persisted with the connection-declared sender.
    assert len(store.appended) == 1
    assert store.appended[0].sender == "alice@web01"
    assert store.appended[0].text == "rolling out"
    # Echoed back to the sending connection.
    msg_frames = [f for f in writer.frames() if f["type"] == "message"]
    assert msg_frames and msg_frames[0]["text"] == "rolling out"
    # And delivered to the other room member.
    assert [f["text"] for f in observer.frames if f["type"] == "message"] == [
        "rolling out"
    ]


def test_per_frame_user_cannot_spoof_sender():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        reader = _reader(
            {"type": "hello", "room": "ops", "user": "real"},
            # A later frame tries to claim a different user — must be ignored.
            {"type": "message", "room": "ops", "text": "hi", "user": "imposter"},
        )
        await NdjsonConsoleConnection(reader, FakeWriter()).run(hub)
        return store

    store = run(scenario())
    assert store.appended[0].sender == "real"


def test_presence_frame_relayed_and_leave_on_eof():
    async def scenario():
        hub = Hub(FakeStore())
        observer = FakeP("b", "ops")
        await hub.join(observer)
        observer.frames.clear()
        reader = _reader(
            {"type": "hello", "room": "ops", "user": "a"},
            {"type": "presence", "room": "ops", "status": "away"},
        )
        await NdjsonConsoleConnection(reader, FakeWriter()).run(hub)
        return hub, observer

    hub, observer = run(scenario())
    statuses = [
        (f["user"], f["status"]) for f in observer.frames if f["type"] == "presence"
    ]
    # join → available, the explicit presence frame → away, EOF leave → away.
    assert ("a", "available") in statuses
    assert statuses[-1] == ("a", "away")
    assert hub.room_size("ops") == 1  # the console left on EOF; observer remains


def test_blank_and_malformed_lines_are_skipped():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        reader = asyncio.StreamReader()
        reader.feed_data(b"\n")  # blank
        reader.feed_data(b"not json\n")  # malformed
        reader.feed_data(b"[1,2,3]\n")  # JSON but not an object
        reader.feed_data(
            (json.dumps({"type": "hello", "room": "ops", "user": "a"}) + "\n").encode()
        )
        reader.feed_data(
            (
                json.dumps({"type": "message", "room": "ops", "text": "ok"}) + "\n"
            ).encode()
        )
        reader.feed_eof()
        await NdjsonConsoleConnection(reader, FakeWriter()).run(hub)
        return store

    store = run(scenario())
    assert [m.text for m in store.appended] == ["ok"]  # survived the junk lines


# ── agent mode (helpdesk) ──────────────────────────────────────────────────────


def test_agent_hello_subscribes_to_namespace_and_posts_into_conversation():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        # A visitor waiting in their private conversation under the IT queue.
        visitor = FakeP("alice@corp", "helpdesk/it/alice@corp")
        await hub.join(visitor)
        visitor.frames.clear()
        reader = _reader(
            {"type": "hello", "namespace": "helpdesk/it", "user": "agent@web01"},
            {"type": "message", "room": "helpdesk/it/alice@corp", "text": "on it"},
        )
        writer = FakeWriter()
        await NdjsonConsoleConnection(reader, writer).run(hub)
        return store, writer, visitor

    store, writer, visitor = run(scenario())
    # The agent's reply was persisted to the visitor's conversation and delivered.
    assert store.appended[0].room == "helpdesk/it/alice@corp"
    assert store.appended[0].sender == "agent@web01"
    assert [f["text"] for f in visitor.frames if f["type"] == "message"] == ["on it"]
    # The agent connection got its own echo.
    assert any(f["type"] == "message" and f["text"] == "on it" for f in writer.frames())


def test_agent_cannot_post_outside_its_namespace_over_the_wire():
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        carol = FakeP("carol@corp", "helpdesk/hr/carol@corp")
        await hub.join(carol)
        carol.frames.clear()
        reader = _reader(
            {"type": "hello", "namespace": "helpdesk/it", "user": "agent@web01"},
            # Target is an HR conversation — the agent only staffs IT.
            {
                "type": "message",
                "room": "helpdesk/hr/carol@corp",
                "text": "wrong queue",
            },
        )
        await NdjsonConsoleConnection(reader, FakeWriter()).run(hub)
        return store, carol

    store, carol = run(scenario())
    assert store.appended == []  # refused, not persisted
    assert [f for f in carol.frames if f["type"] == "message"] == []


def test_agent_is_caught_up_on_open_conversations_on_subscribe():
    async def scenario():
        hub = Hub(FakeStore())
        alice = FakeP("alice@corp", "helpdesk/it/alice@corp")
        await hub.join(alice)
        reader = _reader(
            {"type": "hello", "namespace": "helpdesk/it", "user": "agent@web01"}
        )
        writer = FakeWriter()
        await NdjsonConsoleConnection(reader, writer).run(hub)
        return writer

    writer = run(scenario())
    # On subscribe the agent learns alice has an open thread (presence, room-tagged).
    presences = [
        (f["room"], f["user"], f["status"])
        for f in writer.frames()
        if f["type"] == "presence"
    ]
    assert ("helpdesk/it/alice@corp", "alice@corp", "available") in presences


def test_agent_request_start_complete_frames_drive_lifecycle():
    async def scenario():
        hub = Hub(FakeStore())
        conn = NdjsonConsoleConnection(_reader(), FakeWriter())
        conn.user, conn.namespace, conn.is_agent = "agent@web01", "helpdesk/it", True
        await hub.join_agent(conn)
        await conn._dispatch(
            hub, {"type": "catalog", "tools": [{"name": "reset-password"}]}
        )
        alice = FakeP("alice@corp", "helpdesk/it/alice@corp")
        await hub.join(alice)
        req = await hub.submit_request(alice, "reset-password", {})  # → assigns to conn
        await conn._dispatch(hub, {"type": "request_start", "id": req.id})
        started = hub.request_state(req.id)
        await conn._dispatch(hub, {"type": "request_complete", "id": req.id})
        return started, hub.request_state(req.id)

    started, done = run(scenario())
    assert started == "in_progress" and done == "done"


def test_agent_request_complete_with_error_fails():
    async def scenario():
        hub = Hub(FakeStore())
        conn = NdjsonConsoleConnection(_reader(), FakeWriter())
        conn.user, conn.namespace, conn.is_agent = "agent@web01", "helpdesk/it", True
        await hub.join_agent(conn)
        await conn._dispatch(
            hub, {"type": "catalog", "tools": [{"name": "reset-password"}]}
        )
        alice = FakeP("alice@corp", "helpdesk/it/alice@corp")
        await hub.join(alice)
        req = await hub.submit_request(alice, "reset-password", {})
        await conn._dispatch(
            hub, {"type": "request_complete", "id": req.id, "error": "boom"}
        )
        return hub._requests.get(req.id)

    failed = run(scenario())
    assert failed.state == "failed" and failed.error == "boom"


def test_agent_catalog_frame_publishes_to_live_catalogue():
    async def scenario():
        hub = Hub(FakeStore())
        conn = NdjsonConsoleConnection(_reader(), FakeWriter())
        conn.user, conn.namespace, conn.is_agent = "agent@web01", "helpdesk/it", True
        await hub.join_agent(conn)
        await conn._dispatch(
            hub, {"type": "catalog", "tools": [{"name": "reset-password"}]}
        )
        published = [t["name"] for t in hub.live_catalog("helpdesk/it")]
        await hub.leave_agent(conn)  # the console disconnects
        return published, hub.live_catalog("helpdesk/it")

    published, withdrawn = run(scenario())
    assert published == ["reset-password"]
    # A runnable no longer backed by a connected console vanishes from the catalogue.
    assert withdrawn == []


def test_member_catalog_frame_is_ignored():
    # Only agents publish catalogs — a group-chat console's frame is dropped.
    async def scenario():
        hub = Hub(FakeStore())
        reader = _reader(
            {"type": "hello", "room": "ops", "user": "console@web01"},
            {"type": "catalog", "tools": [{"name": "sneaky"}]},
        )
        await NdjsonConsoleConnection(reader, FakeWriter()).run(hub)
        return hub

    hub = run(scenario())
    assert hub.live_catalog("ops") == []  # nothing published from a member frame


def test_member_message_persists_and_unknown_control_ignored():
    # A plain room member's message persists; an unknown control frame is dropped
    # (there is no agent-driven thread lifecycle any more — the request lifecycle
    # is claim-free and travels in request_status).
    async def scenario():
        store = FakeStore()
        hub = Hub(store)
        reader = _reader(
            {"type": "hello", "room": "helpdesk/it/alice@corp", "user": "alice@corp"},
            {"type": "message", "text": "printer down"},
            {"type": "close", "room": "helpdesk/it/alice@corp"},  # unknown → dropped
        )
        await NdjsonConsoleConnection(reader, FakeWriter()).run(hub)
        return store

    store = run(scenario())
    assert [m.text for m in store.appended] == ["printer down"]
