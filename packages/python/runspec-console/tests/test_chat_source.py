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
    assignment_payload,
    message_payload,
    page_payload,
    parse_inbound,
    presence_payload,
    request_status_payload,
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
        "kind": "chat",
    }


def test_message_payload_tolerates_missing_and_bad_ts():
    p = message_payload({"text": "hi", "ts": "not-a-number"})
    assert p == {
        "room": "",
        "sender": "",
        "text": "hi",
        "id": "",
        "ts": 0.0,
        "kind": "chat",
    }


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


def test_start_sends_hello_first_with_identity():
    transport = FakeTransport()
    src = ChatSource(room="ops", user="console-1", transport=transport)
    src.start(lambda ev: None)
    src.stop()
    # hello is the opening frame, carrying the declared room + user.
    assert transport.sent[0] == {"type": "hello", "room": "ops", "user": "console-1"}
    # …and it precedes the first presence announcement.
    first_presence = next(
        i for i, m in enumerate(transport.sent) if m["type"] == "presence"
    )
    assert first_presence > 0


class _BlockingTransport:
    """recv() blocks until close() — to exercise a clean stop() (no on_closed)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._ev = __import__("threading").Event()
        self.closed = False

    def send(self, obj: dict[str, Any]) -> None:
        self.sent.append(obj)

    def recv(self) -> dict[str, Any] | None:
        self._ev.wait()
        return None

    def close(self) -> None:
        self.closed = True
        self._ev.set()


def test_on_closed_fires_when_channel_drops():
    # The fake transport's recv returns None once drained → an unexpected drop.
    transport = FakeTransport(inbound=[{"type": "message", "sender": "a", "text": "x"}])
    closed: list[bool] = []
    src = ChatSource(
        room="ops",
        user="me",
        transport=transport,
        on_closed=lambda: closed.append(True),
    )
    src.start(lambda ev: None)
    src._thread.join(timeout=2)  # type: ignore[union-attr]
    assert closed == [True]


def test_on_closed_not_fired_on_clean_stop():
    transport = _BlockingTransport()
    closed: list[bool] = []
    src = ChatSource(
        room="ops",
        user="me",
        transport=transport,
        on_closed=lambda: closed.append(True),
    )
    src.start(lambda ev: None)
    thread = src._thread
    src.stop()  # sets _stop, closes transport → recv unblocks to None, loop sees stop
    if thread is not None:
        thread.join(timeout=2)
    assert closed == []  # a deliberate stop must not look like a drop
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
        "assignment",
        "request_status",
        "page",
    ]


# ── helpdesk agent mode ────────────────────────────────────────────────────────


def _agent(transport: FakeTransport) -> ChatSource:
    return ChatSource(
        room="", namespace="helpdesk/it", user="agent@web01", transport=transport
    )


def test_agent_hello_declares_namespace():
    transport = FakeTransport()
    src = _agent(transport)
    src.start(lambda ev: None)
    src.stop()
    assert transport.sent[0] == {
        "type": "hello",
        "namespace": "helpdesk/it",
        "user": "agent@web01",
    }
    # An agent's presence is its rota (available on start, away on stop) — sent
    # without a room, since it's namespace-wide. The hub moves the console's
    # runnables in/out of the live catalogue on it.
    presence = [m for m in transport.sent if m["type"] == "presence"]
    assert presence[0] == {"type": "presence", "status": "available"}
    assert presence[-1] == {"type": "presence", "status": "away"}


def test_agent_send_message_names_the_conversation():
    transport = FakeTransport()
    src = _agent(transport)
    src.start(lambda ev: None)
    src.send_message("on it", room="helpdesk/it/alice@corp")
    src.stop()
    sent = [m for m in transport.sent if m["type"] == "message"]
    assert sent == [
        {"type": "message", "room": "helpdesk/it/alice@corp", "text": "on it"}
    ]


def test_agent_send_message_without_conversation_is_dropped():
    transport = FakeTransport()
    src = _agent(transport)
    src.start(lambda ev: None)
    src.send_message("lost")  # no room named, no member room to fall back to
    src.stop()
    assert [m for m in transport.sent if m["type"] == "message"] == []


def test_send_catalog_is_agent_only():
    member_t = FakeTransport()
    member = ChatSource(room="ops", user="c", transport=member_t)
    member.start(lambda ev: None)
    member.send_catalog([{"name": "x"}])
    member.stop()
    assert [m for m in member_t.sent if m["type"] == "catalog"] == []

    agent_t = FakeTransport()
    agent = _agent(agent_t)
    agent.start(lambda ev: None)
    agent.send_catalog([{"name": "reset-password"}])
    agent.stop()
    cat = next(m for m in agent_t.sent if m["type"] == "catalog")
    assert cat["tools"] == [{"name": "reset-password"}]


# ── portal frames: assignment / request_status / page ──────────────────────────


def test_parse_inbound_assignment_frame():
    ev = parse_inbound(
        {
            "type": "assignment",
            "id": "req1",
            "room": "helpdesk/it/alice@corp",
            "runnable": "reset-password",
            "args": {"user": "alice"},
            "visitor": "alice@corp",
            "visitor_name": "Alice Smith",
        },
        "agent@web01",
    )
    assert ev is not None and ev.type == "assignment"
    assert ev.payload == {
        "id": "req1",
        "room": "helpdesk/it/alice@corp",
        "runnable": "reset-password",
        "args": {"user": "alice"},
        "visitor": "alice@corp",
        "visitor_name": "Alice Smith",
    }


def test_assignment_payload_tolerates_bad_args():
    assert assignment_payload({"id": "r", "args": "not-a-dict"})["args"] == {}


def test_assignment_and_page_visitor_name_defaults_blank():
    # Absent in OIDC / dev mode → blank (the card falls back to the email).
    assert assignment_payload({"id": "r", "visitor": "a@corp"})["visitor_name"] == ""
    assert page_payload({"id": "r", "visitor": "a@corp"})["visitor_name"] == ""


def test_parse_inbound_request_status_frame():
    ev = parse_inbound(
        {
            "type": "request_status",
            "id": "req1",
            "room": "helpdesk/it/alice@corp",
            "state": "failed",
            "assignee": "agent@web01",
            "error": "boom",
        },
        "agent@web01",
    )
    assert ev is not None and ev.type == "request_status"
    assert ev.payload == {
        "id": "req1",
        "room": "helpdesk/it/alice@corp",
        "state": "failed",
        "assignee": "agent@web01",
        "error": "boom",
    }
    # No error key → error is None (a clean done).
    assert request_status_payload({"id": "r", "state": "done"})["error"] is None


def test_parse_inbound_page_frame():
    ev = parse_inbound(
        {
            "type": "page",
            "id": "req1",
            "room": "helpdesk/it/alice@corp",
            "runnable": "reset-password",
            "queue": "it",
            "visitor": "alice@corp",
        },
        "agent@web01",
    )
    assert ev is not None and ev.type == "page"
    assert ev.payload == page_payload(
        {
            "id": "req1",
            "room": "helpdesk/it/alice@corp",
            "runnable": "reset-password",
            "queue": "it",
            "visitor": "alice@corp",
        }
    )


def test_send_request_controls_are_agent_only():
    member_t = FakeTransport()
    member = ChatSource(room="ops", user="c", transport=member_t)
    member.start(lambda ev: None)
    member.send_request_start("req1")
    member.send_request_complete("req1")
    member.stop()
    assert [m for m in member_t.sent if m["type"].startswith("request_")] == []

    agent_t = FakeTransport()
    agent = _agent(agent_t)
    agent.start(lambda ev: None)
    agent.send_request_start("req1")
    agent.send_request_complete("req1")
    agent.send_request_complete("req2", error="boom")
    agent.stop()
    controls = [m for m in agent_t.sent if m["type"].startswith("request_")]
    assert controls == [
        {"type": "request_start", "id": "req1"},
        {"type": "request_complete", "id": "req1"},
        {"type": "request_complete", "id": "req2", "error": "boom"},
    ]


def test_parse_inbound_drops_conversation_frame():
    # The claim-based conversation lifecycle is gone — its frame is now unknown.
    assert parse_inbound({"type": "conversation", "state": "assigned"}, "me") is None


def test_message_payload_carries_request_kind_and_data():
    payload = message_payload(
        {
            "type": "message",
            "room": "helpdesk/it/alice@corp",
            "sender": "alice@corp",
            "text": "Requested: reset-password",
            "kind": "request",
            "data": {"tool": "reset-password", "args": {"user": "alice"}},
        }
    )
    assert payload["kind"] == "request"
    assert payload["data"] == {"tool": "reset-password", "args": {"user": "alice"}}
    # Plain chat (and older servers sending no kind) default to "chat", no data.
    plain = message_payload({"type": "message", "room": "ops", "text": "hi"})
    assert plain["kind"] == "chat" and "data" not in plain


# ── email-verify: advertise on connect + send_verify handling ─────────────────


def _ev_agent(transport, **kw):
    return ChatSource(
        namespace="helpdesk/it-support",
        user="agent@web01",
        queue="it-support",
        transport=transport,
        **kw,
    )


def test_agent_advertises_queue_on_start_when_mailbox_set():
    transport = FakeTransport()
    src = _ev_agent(
        transport, queue_email="it-support@corp.com", queue_label="IT Support"
    )
    src.start(lambda ev: None)
    src.stop()
    ads = [m for m in transport.sent if m["type"] == "advertise"]
    assert ads == [
        {
            "type": "advertise",
            "queue": "it-support",
            "queue_email": "it-support@corp.com",
            "display_name": "IT Support",
        }
    ]


def test_advertise_display_name_defaults_to_queue():
    transport = FakeTransport()
    src = _ev_agent(transport, queue_email="it-support@corp.com")  # no label
    src.start(lambda ev: None)
    src.stop()
    ad = next(m for m in transport.sent if m["type"] == "advertise")
    assert ad["display_name"] == "it-support"


def test_no_advertise_without_mailbox():
    transport = FakeTransport()
    src = _ev_agent(transport)  # no queue_email
    src.start(lambda ev: None)
    src.stop()
    assert [m for m in transport.sent if m["type"] == "advertise"] == []


def test_no_advertise_in_room_mode():
    transport = FakeTransport()
    src = ChatSource(room="ops", user="c", transport=transport)
    src.start(lambda ev: None)
    src.stop()
    assert [m for m in transport.sent if m["type"] == "advertise"] == []


def test_send_verify_mails_and_acks_ok():
    transport = FakeTransport()
    calls: list[dict[str, Any]] = []

    def fake_sender(*, to, subject, body, from_mailbox):
        calls.append({"to": to, "subject": subject, "body": body, "from": from_mailbox})

    src = _ev_agent(
        transport, queue_email="it-support@corp.com", verify_sender=fake_sender
    )
    src._handle(
        {
            "type": "send_verify",
            "nonce": "n1",
            "to": "jane@corp.com",
            "name": "Jane Doe",
            "queue": "it-support",
            "queue_email": "it-support@corp.com",
            "verify_url": "https://room.test/verify?token=t",
        }
    )
    # The mail was composed + sent as the queue mailbox…
    assert len(calls) == 1
    assert calls[0]["to"] == "jane@corp.com"
    assert calls[0]["from"] == "it-support@corp.com"
    assert "https://room.test/verify?token=t" in calls[0]["body"]
    # …and acked ok.
    assert {"type": "verify_sent", "nonce": "n1", "ok": True} in transport.sent
    # send_verify is a protocol action, not a trigger event.
    assert src.event_types() and "send_verify" not in src.event_types()


def test_send_verify_acks_failure_when_sender_raises():
    transport = FakeTransport()

    def boom(*, to, subject, body, from_mailbox):
        raise RuntimeError("no Outlook here")

    src = _ev_agent(transport, queue_email="it-support@corp.com", verify_sender=boom)
    src._handle(
        {
            "type": "send_verify",
            "nonce": "n2",
            "to": "jane@corp.com",
            "name": "Jane",
            "queue": "it-support",
            "queue_email": "it-support@corp.com",
            "verify_url": "https://room.test/verify?token=t",
        }
    )
    assert {"type": "verify_sent", "nonce": "n2", "ok": False} in transport.sent


def test_send_verify_without_nonce_is_ignored():
    transport = FakeTransport()
    calls: list[Any] = []
    src = _ev_agent(
        transport,
        queue_email="it-support@corp.com",
        verify_sender=lambda **kw: calls.append(kw),
    )
    src._handle({"type": "send_verify", "to": "jane@corp.com"})  # no nonce
    assert calls == [] and transport.sent == []


def test_emitted_events_unaffected_by_send_verify_branch():
    # A normal message still routes to an emitted event.
    transport = FakeTransport()
    emitted: list[Event] = []
    src = _ev_agent(transport, queue_email="it-support@corp.com")
    src._emit = emitted.append
    src._handle(
        {
            "type": "message",
            "room": "helpdesk/it-support/jane",
            "sender": "jane",
            "text": "hi",
        }
    )
    assert [e.type for e in emitted] == ["message"]
