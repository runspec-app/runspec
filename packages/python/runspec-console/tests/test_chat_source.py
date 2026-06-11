"""
Tests for the chat-room source (sources/chat.py).

Payload extraction and inbound routing are pure, tested with a fake transport —
no websocket, no SSH, no platform dependency (the source is a network client, so
unlike the Outlook / notification sources it has no OS guard). The threaded recv
loop is exercised through the pure ``_handle`` split so tests stay deterministic.
"""

from __future__ import annotations

from typing import Any

from runspec_console import config
from runspec_console.events import Event, EventSource, available_sources, create_source
from runspec_console.sources.chat import (
    ChatSource,
    message_payload,
    parse_inbound,
    presence_payload,
)


class FakeTransport:
    """Records outbound sends; serves queued inbound dicts to recv()."""

    def __init__(self, inbound: list[dict[str, Any] | None] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self._inbound = list(inbound or [])
        self.closed = False

    def send(self, obj: dict[str, Any]) -> None:
        self.sent.append(obj)

    def recv(self) -> dict[str, Any] | None:
        if self._inbound:
            return self._inbound.pop(0)
        return None  # channel exhausted == closed

    def close(self) -> None:
        self.closed = True


# ── payload extraction ────────────────────────────────────────────────────────


def test_message_payload():
    p = message_payload(
        {"room": "ops", "sender": "alice", "text": "prod is down", "id": "m1", "ts": 5}
    )
    assert p == {
        "room": "ops",
        "sender": "alice",
        "text": "prod is down",
        "id": "m1",
        "ts": 5.0,
    }


def test_message_payload_tolerates_missing_and_bad_ts():
    p = message_payload({"text": "hi", "ts": "not-a-number"})
    assert p == {"room": "", "sender": "", "text": "hi", "id": "", "ts": 0.0}


def test_message_payload_caps_long_text():
    p = message_payload({"text": "x" * 25_000})
    assert len(p["text"]) == 20_001 and p["text"].endswith("…")


def test_presence_payload():
    p = presence_payload({"room": "ops", "user": "bob", "status": "available"})
    assert p == {"room": "ops", "user": "bob", "status": "available"}


# ── inbound routing (parse_inbound) ────────────────────────────────────────────


def test_parse_inbound_message():
    ev = parse_inbound(
        {"type": "message", "sender": "alice", "text": "hi"}, "console-1"
    )
    assert isinstance(ev, Event)
    assert ev.source == "chat" and ev.type == "message"
    assert ev.payload["sender"] == "alice"


def test_parse_inbound_skips_own_message():
    # The server echoes our own sends back; we must not react to our own acks.
    assert (
        parse_inbound(
            {"type": "message", "sender": "console-1", "text": "ack"}, "console-1"
        )
        is None
    )


def test_parse_inbound_presence():
    ev = parse_inbound(
        {"type": "presence", "user": "bob", "status": "away"}, "console-1"
    )
    assert ev is not None and ev.type == "presence"
    assert ev.payload["status"] == "away"


def test_parse_inbound_skips_own_presence():
    assert (
        parse_inbound(
            {"type": "presence", "user": "console-1", "status": "away"}, "console-1"
        )
        is None
    )


def test_parse_inbound_drops_unknown_and_non_dict():
    assert parse_inbound({"type": "typing"}, "me") is None
    assert parse_inbound("not a dict", "me") is None
    assert parse_inbound({"no": "type"}, "me") is None


# ── source behaviour with a fake transport ─────────────────────────────────────


def test_handle_emits_event():
    src = ChatSource(room="ops", user="console-1", transport=FakeTransport())
    emitted: list[Event] = []
    src._emit = emitted.append
    src._handle({"type": "message", "sender": "alice", "text": "down"})
    assert len(emitted) == 1 and emitted[0].payload["text"] == "down"


def test_handle_skips_own_message():
    src = ChatSource(room="ops", user="console-1", transport=FakeTransport())
    emitted: list[Event] = []
    src._emit = emitted.append
    src._handle({"type": "message", "sender": "console-1", "text": "ack"})
    assert emitted == []


def test_start_announces_presence_then_stop_says_away():
    transport = FakeTransport()
    src = ChatSource(room="ops", user="console-1", transport=transport)
    src.start(lambda ev: None)
    src.stop()
    statuses = [m["status"] for m in transport.sent if m["type"] == "presence"]
    assert statuses[0] == "available"
    assert statuses[-1] == "away"
    assert transport.closed is True


def test_run_drains_inbound_and_emits():
    transport = FakeTransport(
        inbound=[
            {"type": "message", "sender": "alice", "text": "one"},
            {"type": "message", "sender": "console-1", "text": "my own echo"},
            {"type": "message", "sender": "bob", "text": "two"},
        ]
    )
    src = ChatSource(room="ops", user="console-1", transport=transport)
    emitted: list[Event] = []
    src.start(emitted.append)
    src._thread.join(timeout=2)  # type: ignore[union-attr]
    texts = [e.payload["text"] for e in emitted if e.type == "message"]
    assert texts == ["one", "two"]  # own echo dropped


def test_send_message_relays_to_transport():
    transport = FakeTransport()
    src = ChatSource(room="ops", user="console-1", transport=transport)
    src.send_message("on it, 5 min")
    msgs = [m for m in transport.sent if m["type"] == "message"]
    assert msgs == [{"type": "message", "room": "ops", "text": "on it, 5 min"}]


def test_set_presence_relays():
    transport = FakeTransport()
    src = ChatSource(room="ops", user="console-1", transport=transport)
    src.set_presence(False)
    src.set_presence(True)
    statuses = [m["status"] for m in transport.sent if m["type"] == "presence"]
    assert statuses == ["away", "available"]


# ── availability guards ────────────────────────────────────────────────────────


def test_unavailable_without_room():
    ok, reason = ChatSource(room="", transport=FakeTransport()).available()
    assert ok is False and "room" in reason


def test_unavailable_without_transport():
    ok, reason = ChatSource(room="ops").available()
    assert ok is False and "transport" in reason


def test_available_with_room_and_transport():
    ok, reason = ChatSource(room="ops", transport=FakeTransport()).available()
    assert ok is True and reason == ""


def test_start_noop_when_unavailable():
    src = ChatSource(room="ops")  # no transport
    emitted: list[Event] = []
    src.start(emitted.append)
    assert emitted == [] and src._thread is None


def test_transport_factory_used_on_start():
    transport = FakeTransport()
    src = ChatSource(room="ops", user="c1", transport_factory=lambda: transport)
    src.start(lambda ev: None)
    assert src._transport is transport  # factory was invoked
    src.stop()


# ── config, protocol, registration ─────────────────────────────────────────────


def test_read_chat_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config.write_config(
        {"chat": {"host": "jump-1", "port": 8765, "room": "ops", "user": "c1"}}
    )
    assert config.read_chat() == {
        "host": "jump-1",
        "port": 8765,
        "room": "ops",
        "user": "c1",
    }


def test_read_chat_empty_when_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config.read_chat() == {}


def test_satisfies_protocol():
    assert isinstance(ChatSource(room="ops", transport=FakeTransport()), EventSource)


def test_registered_on_import():
    assert "chat" in available_sources()
    assert isinstance(create_source("chat"), ChatSource)


def test_event_types():
    assert ChatSource(room="ops", transport=FakeTransport()).event_types() == [
        "message",
        "presence",
    ]
