"""
test_chat_bridge.py — slice-3 bridge wiring for the chat-room source.

Covers the seams added to wire ``ChatSource`` into the bridge without an LLM, an
SSH tunnel, or a webview:

- ``_ChatUIFanout`` — fans message + presence to the Rooms UI; only messages
  reach the trigger engine; tracks the live inner source via on_start/on_stop.
- ``_RotaPresenceWatcher`` — pushes presence to the room only when the rota's
  on-shift state flips.
- ``send_room_message`` — the composer's one outbound path (and its guards).
- ``_gated_propose_reply`` early-return guards (the confirmed path is exercised
  by the autonomy-gate tests).

The bridge methods are bound onto a bare stub to dodge Bridge.__init__ side
effects, mirroring test_draft_send.py.
"""

from __future__ import annotations

import asyncio
import threading
import types
from typing import Any

from runspec_console.bridge import Bridge, _ChatUIFanout, _RotaPresenceWatcher
from runspec_console.events import Event


# ── _ChatUIFanout ─────────────────────────────────────────────────────────────


class _FakeInner:
    """Stand-in ChatSource: records lifecycle and captures the wrapped emit."""

    def __init__(self) -> None:
        self.emit: Any = None
        self.started = False
        self.stopped = False

    def available(self) -> tuple[bool, str]:
        return (True, "")

    def event_types(self) -> list[str]:
        return ["message", "presence"]

    def start(self, emit: Any) -> None:
        self.emit = emit
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _fanout():
    inner = _FakeInner()
    dispatched: list[tuple[str, dict]] = []
    starts: list[Any] = []
    stops: list[bool] = []
    wrapper = _ChatUIFanout(
        inner,
        lambda ev, detail: dispatched.append((ev, detail)),
        on_start=lambda src: starts.append(src),
        on_stop=lambda: stops.append(True),
    )
    return inner, wrapper, dispatched, starts, stops


def test_fanout_message_reaches_ui_and_engine():
    inner, wrapper, dispatched, starts, _ = _fanout()
    engine_seen: list[Event] = []
    wrapper.start(engine_seen.append)

    assert inner.started and starts == [inner]  # on_start tracked the inner source
    inner.emit(Event.new("chat", "message", {"room": "ops", "text": "hi"}))

    assert dispatched == [("runspec:room_message", {"room": "ops", "text": "hi"})]
    assert len(engine_seen) == 1 and engine_seen[0].type == "message"


def test_fanout_presence_is_ui_only():
    inner, wrapper, dispatched, _, _ = _fanout()
    engine_seen: list[Event] = []
    wrapper.start(engine_seen.append)

    inner.emit(Event.new("chat", "presence", {"user": "ana", "status": "away"}))

    assert dispatched == [("runspec:room_presence", {"user": "ana", "status": "away"})]
    assert engine_seen == []  # presence never reaches the engine / audit feed


def test_fanout_stop_runs_inner_and_on_stop():
    inner, wrapper, _, _, stops = _fanout()
    wrapper.start(lambda _ev: None)
    wrapper.stop()
    assert inner.stopped and stops == [True]


def test_fanout_delegates_available_and_event_types():
    inner, wrapper, *_ = _fanout()
    assert wrapper.available() == (True, "")
    assert wrapper.event_types() == ["message", "presence"]
    assert wrapper.id == "chat"


# ── _RotaPresenceWatcher ──────────────────────────────────────────────────────


def test_presence_watcher_pushes_only_on_flip():
    clock = {"on": True}
    pushed: list[bool] = []
    w = _RotaPresenceWatcher(lambda: clock["on"], pushed.append)

    w._tick()  # first read → available
    w._tick()  # unchanged → no push
    clock["on"] = False
    w._tick()  # flip → away
    w._tick()  # unchanged → no push
    clock["on"] = True
    w._tick()  # flip → available

    assert pushed == [True, False, True]


def test_presence_watcher_swallows_clock_errors():
    pushed: list[bool] = []

    def boom() -> bool:
        raise RuntimeError("calendar down")

    w = _RotaPresenceWatcher(boom, pushed.append)
    w._tick()  # must not raise
    assert pushed == []


# ── send_room_message ─────────────────────────────────────────────────────────


class _FakeSource:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, text: str) -> None:
        self.sent.append(text)


def _send_stub(chat_source: Any):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_source = chat_source
    s.send_room_message = types.MethodType(Bridge.send_room_message, s)
    return s


def test_send_room_message_routes_to_source():
    src = _FakeSource()
    out = _send_stub(src).send_room_message("ops", "on it")
    assert out == {"ok": True}
    assert src.sent == ["on it"]


def test_send_room_message_without_connection():
    out = _send_stub(None).send_room_message("ops", "hi")
    assert out["ok"] is False and "not connected" in out["error"]


def test_send_room_message_rejects_empty():
    src = _FakeSource()
    out = _send_stub(src).send_room_message("ops", "   ")
    assert out["ok"] is False and "empty" in out["error"]
    assert src.sent == []  # nothing dispatched on the wire


# ── get_room_state ────────────────────────────────────────────────────────────


def _state_stub(chat_source: Any):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_source = chat_source
    s.get_room_state = types.MethodType(Bridge.get_room_state, s)
    return s


def test_get_room_state_unconfigured(monkeypatch):
    from runspec_console import config

    monkeypatch.setattr(config, "read_chat", lambda: {})
    out = _state_stub(None).get_room_state()
    assert out == {"connected": False, "room": "", "user": ""}


def test_get_room_state_configured_and_connected(monkeypatch):
    from runspec_console import config

    monkeypatch.setattr(
        config, "read_chat", lambda: {"room": "ops", "user": "alice@web01"}
    )
    out = _state_stub(_FakeSource()).get_room_state()
    assert out == {"connected": True, "room": "ops", "user": "alice@web01"}


def test_get_room_state_defaults_user_when_unset(monkeypatch):
    from runspec_console import config
    from runspec_console.sources import chat_transport

    monkeypatch.setattr(config, "read_chat", lambda: {"room": "ops", "host": "web01"})
    monkeypatch.setattr(chat_transport.getpass, "getuser", lambda: "bob", raising=False)
    out = _state_stub(None).get_room_state()
    assert out == {"connected": False, "room": "ops", "user": "bob@web01"}


# ── _gated_propose_reply guards (pre-confirm early returns) ────────────────────


def _propose_stub():
    s = types.SimpleNamespace()
    s._gated_propose_reply = types.MethodType(Bridge._gated_propose_reply, s)
    return s


def test_propose_reply_rejects_missing_room():
    out = asyncio.run(
        _propose_stub()._gated_propose_reply({"text": "hi"}, "card1", None)
    )
    assert out.startswith("✗") and "no room" in out


def test_propose_reply_rejects_empty_text():
    out = asyncio.run(
        _propose_stub()._gated_propose_reply({"text": "  "}, "card1", "ops")
    )
    assert out.startswith("✗") and "empty" in out
