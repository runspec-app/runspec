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
    s._chat_sources = {}
    s._trigger_engine = None
    s.dispatched: list[tuple[str, dict]] = []
    s._dispatch = lambda ev, detail: s.dispatched.append((ev, detail))
    s._build_clock_ok = lambda: lambda: True
    s._CHAT_BACKOFF_START = 0.01
    s._CHAT_BACKOFF_MAX = 0.01
    s._on_chat_event = types.MethodType(Bridge._on_chat_event, s)
    s._chat_supervise = types.MethodType(Bridge._chat_supervise, s)
    return s


_SPEC = {"room": "ops", "host": "web01", "user": "me"}


def test_chat_supervisor_reconnects_until_stopped():
    import time

    s = _supervisor_stub()
    calls: list[int] = []

    def factory():
        calls.append(1)
        return _OneShotTransport()

    s._chat_stop.clear()
    th = threading.Thread(target=s._chat_supervise, args=(_SPEC, factory), daemon=True)
    th.start()
    deadline = time.time() + 3
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.01)
    s._chat_stop.set()
    th.join(timeout=2)

    assert len(calls) >= 3  # it kept reconnecting after each drop
    assert s._chat_sources == {}  # cleared once stopped
    conns = [d for ev, d in s.dispatched if ev == "runspec:room_connection"]
    assert {"room": "ops", "host": "web01", "connected": True} in conns
    assert {"room": "ops", "host": "web01", "connected": False} in conns


def test_start_chat_noop_without_pool():
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_stop = threading.Event()
    s._chat_supervisors = {}
    s._ssh_pool = None  # pooling disabled → can't tunnel → no rooms
    s.start_chat = types.MethodType(Bridge.start_chat, s)
    s.start_chat()
    assert s._chat_supervisors == {}  # nothing started


# ── send_room_message ─────────────────────────────────────────────────────────


class _FakeSource:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, text: str) -> None:
        self.sent.append(text)


def _send_stub(sources: dict[str, Any]):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_sources = sources
    s.send_room_message = types.MethodType(Bridge.send_room_message, s)
    return s


def test_send_room_message_routes_to_room():
    src = _FakeSource()
    out = _send_stub({"ops": src}).send_room_message("ops", "on it")
    assert out == {"ok": True}
    assert src.sent == ["on it"]


def test_send_room_message_unknown_room():
    out = _send_stub({"ops": _FakeSource()}).send_room_message("other", "hi")
    assert out["ok"] is False and "not connected" in out["error"]


def test_send_room_message_without_connection():
    out = _send_stub({}).send_room_message("ops", "hi")
    assert out["ok"] is False and "not connected" in out["error"]


def test_send_room_message_rejects_empty():
    src = _FakeSource()
    out = _send_stub({"ops": src}).send_room_message("ops", "   ")
    assert out["ok"] is False and "empty" in out["error"]
    assert src.sent == []  # nothing dispatched on the wire


# ── get_room_state ────────────────────────────────────────────────────────────


def _state_stub(specs: list[dict[str, Any]], sources: dict[str, Any]):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_sources = sources
    s._room_specs = lambda: specs
    s.get_room_state = types.MethodType(Bridge.get_room_state, s)
    return s


def test_get_room_state_no_rooms():
    assert _state_stub([], {}).get_room_state() == {"rooms": []}


def test_get_room_state_lists_rooms_with_connection():
    specs = [
        {"room": "ops", "host": "web01", "user": "alice@web01"},
        {"room": "dba", "host": "db01", "user": "alice@db01"},
    ]
    out = _state_stub(specs, {"ops": _FakeSource()}).get_room_state()
    assert out == {
        "rooms": [
            {"room": "ops", "host": "web01", "user": "alice@web01", "connected": True},
            {"room": "dba", "host": "db01", "user": "alice@db01", "connected": False},
        ]
    }


# ── _room_specs (host entries + legacy [chat] fallback) ───────────────────────


def _specs_stub(hosts: list[dict[str, Any]]):
    s = types.SimpleNamespace()
    s._hosts = hosts
    s._reload_hosts = lambda: None
    s._room_specs = types.MethodType(Bridge._room_specs, s)
    return s


def test_room_specs_from_host_entries(monkeypatch):
    from runspec_console import config

    monkeypatch.setattr(config, "read_chat", lambda: {})
    s = _specs_stub(
        [{"name": "web01", "ssh": "ops@web01", "room": "ops", "room_port": 8765}]
    )
    specs = s._room_specs()
    assert len(specs) == 1
    assert specs[0]["room"] == "ops" and specs[0]["ssh_target"] == "ops@web01"


def test_room_specs_legacy_chat_resolves_to_host(monkeypatch):
    # A legacy [chat] table whose `host` names a fleet entry inherits that host's
    # ssh target + key (so old configs keep working AND get the fixed connection).
    from runspec_console import config

    monkeypatch.setattr(
        config, "read_chat", lambda: {"host": "web01", "port": 8765, "room": "ops"}
    )
    s = _specs_stub([{"name": "web01", "ssh": "ops@web01", "identityFile": "~/.ssh/k"}])
    specs = s._room_specs()
    assert len(specs) == 1
    assert specs[0]["ssh_target"] == "ops@web01"
    assert specs[0]["identity_file"] == "~/.ssh/k"  # inherited from the matched host


def test_room_specs_empty_when_nothing_configured(monkeypatch):
    from runspec_console import config

    monkeypatch.setattr(config, "read_chat", lambda: {})
    assert _specs_stub([{"name": "web01", "ssh": "ops@web01"}])._room_specs() == []


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
