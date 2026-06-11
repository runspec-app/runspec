"""
test_chat_bridge.py — bridge wiring for the always-on chat-room source.

Covers the seams that wire ``ChatSource`` into the bridge without an LLM, an SSH
tunnel, or a webview:

- ``_on_chat_event`` — fans message + presence to the Rooms UI; routes *messages*
  (not presence) to the live trigger engine via ``engine.feed``.
- ``_RotaPresenceWatcher`` — pushes presence to the room only when the rota's
  on-shift state flips.
- ``send_room_message`` / ``get_room_state`` — the composer's outbound path and
  the Rooms view's connection state.
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

from runspec_console.bridge import Bridge, _RotaPresenceWatcher
from runspec_console.events import Event


# ── _on_chat_event (UI fan-out + engine routing) ───────────────────────────────


class _FakeEngine:
    def __init__(self) -> None:
        self.fed: list[Event] = []

    def feed(self, event: Event) -> None:
        self.fed.append(event)


def _event_stub(engine: Any = None):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._trigger_engine = engine
    s.dispatched: list[tuple[str, dict]] = []
    s._dispatch = lambda ev, detail: s.dispatched.append((ev, detail))
    s._on_chat_event = types.MethodType(Bridge._on_chat_event, s)
    return s


def test_on_chat_event_message_reaches_ui_and_engine():
    engine = _FakeEngine()
    s = _event_stub(engine)
    s._on_chat_event(Event.new("chat", "message", {"room": "ops", "text": "hi"}))
    assert s.dispatched == [("runspec:room_message", {"room": "ops", "text": "hi"})]
    assert len(engine.fed) == 1 and engine.fed[0].type == "message"


def test_on_chat_event_presence_is_ui_only():
    engine = _FakeEngine()
    s = _event_stub(engine)
    s._on_chat_event(Event.new("chat", "presence", {"user": "ana", "status": "away"}))
    assert s.dispatched == [
        ("runspec:room_presence", {"user": "ana", "status": "away"})
    ]
    assert engine.fed == []  # presence never reaches the engine / audit feed


def test_on_chat_event_message_with_no_engine_still_fans_to_ui():
    s = _event_stub(None)  # no trigger engine running
    s._on_chat_event(Event.new("chat", "message", {"room": "ops", "text": "hi"}))
    assert s.dispatched == [("runspec:room_message", {"room": "ops", "text": "hi"})]


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


# ── reconnect supervisor (_chat_supervise / start_chat) ───────────────────────


class _OneShotTransport:
    """A transport that 'drops' immediately (recv → None), forcing a reconnect."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, obj: dict[str, Any]) -> None:
        self.sent.append(obj)

    def recv(self) -> dict[str, Any] | None:
        return None  # channel already closed → ChatSource fires on_closed

    def close(self) -> None:
        pass


def _supervisor_stub():
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_stop = threading.Event()
    s._chat_source = None
    s._trigger_engine = None
    s.dispatched: list[tuple[str, dict]] = []
    s._dispatch = lambda ev, detail: s.dispatched.append((ev, detail))
    s._build_clock_ok = lambda: lambda: True
    s._CHAT_BACKOFF_START = 0.01
    s._CHAT_BACKOFF_MAX = 0.01
    s._on_chat_event = types.MethodType(Bridge._on_chat_event, s)
    s._chat_supervise = types.MethodType(Bridge._chat_supervise, s)
    return s


def test_chat_supervisor_reconnects_until_stopped():
    import time

    s = _supervisor_stub()
    calls: list[int] = []

    def factory():
        calls.append(1)
        return _OneShotTransport()

    s._chat_stop.clear()
    th = threading.Thread(
        target=s._chat_supervise, args=(factory, "me", "ops"), daemon=True
    )
    th.start()
    deadline = time.time() + 3
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.01)
    s._chat_stop.set()
    th.join(timeout=2)

    assert len(calls) >= 3  # it kept reconnecting after each drop
    assert s._chat_source is None  # cleared once stopped
    kinds = [
        d["connected"] for ev, d in s.dispatched if ev == "runspec:room_connection"
    ]
    assert True in kinds and False in kinds  # connect/disconnect were surfaced


def test_start_chat_noop_without_pool():
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_stop = threading.Event()
    s._chat_supervisor = None
    s._ssh_pool = None  # pooling disabled → can't tunnel → no room
    s.start_chat = types.MethodType(Bridge.start_chat, s)
    s.start_chat()
    assert s._chat_supervisor is None  # nothing started


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
