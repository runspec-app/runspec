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


# ── save_jump_hosts preserves unmanaged keys (e.g. room_dest_host) ────────────


def test_save_jump_hosts_preserves_unmanaged_keys(tmp_path, monkeypatch):
    from runspec_console import bridge as bridge_mod

    hosts_file = tmp_path / "runspec_hosts.toml"
    hosts_file.write_text(
        '[[host]]\nname = "web01"\nssh = "ops@web01"\n'
        'room = "ops"\nroom_port = 8765\nroom_dest_host = "localhost"\n'
        'runspec_paths = ["runspec"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge_mod, "hosts_path", lambda: hosts_file)

    s = types.SimpleNamespace()
    s._MANAGED_HOST_KEYS = Bridge._MANAGED_HOST_KEYS
    s._reload_hosts = lambda: None
    s.restart_chat = lambda: None
    s._refresh_cycle = lambda: None
    s.save_jump_hosts = types.MethodType(Bridge.save_jump_hosts, s)

    # The editor sends the host WITHOUT room_dest_host (a field it doesn't manage).
    s.save_jump_hosts(
        [
            {
                "name": "web01",
                "hostname": "web01",
                "user": "ops",
                "room": "ops",
                "room_port": 8765,
                "runspec_paths": ["runspec"],
            }
        ]
    )

    saved = {e["name"]: e for e in bridge_mod.load_hosts(hosts_file)}
    assert saved["web01"].get("room_dest_host") == "localhost"  # preserved, not dropped
    assert saved["web01"]["room"] == "ops" and saved["web01"]["room_port"] == 8765


# ── _on_chat_event (UI fan-out + engine routing) ───────────────────────────────


class _FakeEngine:
    def __init__(self) -> None:
        self.fed: list[Event] = []

    def feed(self, event: Event) -> None:
        self.fed.append(event)


def _event_stub(engine: Any = None, agent_rooms: dict[str, str] | None = None):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._trigger_engine = engine
    s._agent_rooms = dict(agent_rooms or {})
    s._thread_states = {}
    s.dispatched: list[tuple[str, dict]] = []
    s._dispatch = lambda ev, detail: s.dispatched.append((ev, detail))
    s._on_chat_event = types.MethodType(Bridge._on_chat_event, s)
    s._feed_engine_for = types.MethodType(Bridge._feed_engine_for, s)
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


# ── helpdesk: conversation frames + the claim gate ─────────────────────────────

_NS = "helpdesk/it"
_THREAD = "helpdesk/it/alice@corp"


def _conv(state: str, assignee: str | None) -> Event:
    return Event.new(
        "chat", "conversation", {"room": _THREAD, "state": state, "assignee": assignee}
    )


def test_conversation_frame_is_ui_only_and_tracked():
    engine = _FakeEngine()
    s = _event_stub(engine, agent_rooms={_NS: "agent@web01"})
    s._on_chat_event(_conv("open", None))
    assert s.dispatched == [
        (
            "runspec:room_conversation",
            {"room": _THREAD, "state": "open", "assignee": None},
        )
    ]
    assert engine.fed == []  # lifecycle never reaches the engine
    assert s._thread_states[_THREAD] == {"state": "open", "assignee": None}


def test_unclaimed_thread_message_is_ui_only():
    """The claim gate: no agent turn (engine feed) until a human claims."""
    engine = _FakeEngine()
    s = _event_stub(engine, agent_rooms={_NS: "agent@web01"})
    s._on_chat_event(_conv("open", None))
    s._on_chat_event(
        Event.new("chat", "message", {"room": _THREAD, "text": "help", "kind": "chat"})
    )
    assert (
        "runspec:room_message",
        {"room": _THREAD, "text": "help", "kind": "chat"},
    ) in s.dispatched
    assert engine.fed == []  # visible in the inbox, but no model spend


def test_claimed_by_me_thread_message_feeds_engine():
    engine = _FakeEngine()
    s = _event_stub(engine, agent_rooms={_NS: "agent@web01"})
    s._on_chat_event(_conv("assigned", "agent@web01"))  # we claimed it
    s._on_chat_event(Event.new("chat", "message", {"room": _THREAD, "text": "help"}))
    assert len(engine.fed) == 1 and engine.fed[0].payload["room"] == _THREAD


def test_claimed_by_teammate_thread_message_is_ui_only():
    engine = _FakeEngine()
    s = _event_stub(engine, agent_rooms={_NS: "agent@web01"})
    s._on_chat_event(_conv("assigned", "agent2@web02"))  # a teammate owns it
    s._on_chat_event(Event.new("chat", "message", {"room": _THREAD, "text": "help"}))
    assert engine.fed == []  # their thread, their console's turn


def test_group_chat_message_unaffected_by_agent_mode():
    engine = _FakeEngine()
    s = _event_stub(engine, agent_rooms={_NS: "agent@web01"})
    s._on_chat_event(Event.new("chat", "message", {"room": "ops", "text": "deploy"}))
    assert len(engine.fed) == 1  # plain rooms keep the original behaviour


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
    s._agent_rooms = {}
    s._thread_states = {}
    s._published_catalogs = {}
    s._trigger_engine = None
    s.dispatched: list[tuple[str, dict]] = []
    s._dispatch = lambda ev, detail: s.dispatched.append((ev, detail))
    s._build_clock_ok = lambda: lambda: True
    s._CHAT_BACKOFF_START = 0.01
    s._CHAT_BACKOFF_MAX = 0.01
    s._on_chat_event = types.MethodType(Bridge._on_chat_event, s)
    s._feed_engine_for = types.MethodType(Bridge._feed_engine_for, s)
    s._chat_supervise = types.MethodType(Bridge._chat_supervise, s)
    s.published: list[str] = []
    s._publish_catalog = lambda spec, source, *, force=False: s.published.append(
        spec["room"]
    )
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
    assert {"room": "ops", "host": "web01", "connected": True, "queue": ""} in conns
    assert {"room": "ops", "host": "web01", "connected": False, "queue": ""} in conns
    assert s.published == []  # a plain room never publishes a catalog


def test_chat_supervisor_agent_mode_publishes_catalog_and_registers():
    import time

    s = _supervisor_stub()
    spec = {
        "room": "helpdesk/it",
        "host": "web01",
        "user": "agent@web01",
        "queue": "it",
    }
    registered: list[dict[str, str]] = []
    orig_publish = s._publish_catalog

    def publish_and_snapshot(sp, source, *, force=False):
        registered.append(dict(s._agent_rooms))  # registered before publishing
        orig_publish(sp, source, force=force)

    s._publish_catalog = publish_and_snapshot
    s._chat_stop.clear()
    th = threading.Thread(
        target=s._chat_supervise, args=(spec, _OneShotTransport), daemon=True
    )
    th.start()
    deadline = time.time() + 3
    while not s.published and time.time() < deadline:
        time.sleep(0.01)
    s._chat_stop.set()
    th.join(timeout=2)

    assert "helpdesk/it" in s.published  # the catalog went out on subscribe
    assert registered and registered[0].get("helpdesk/it") == "agent@web01"
    assert s._agent_rooms == {}  # cleared once stopped
    conns = [d for ev, d in s.dispatched if ev == "runspec:room_connection"]
    assert {
        "room": "helpdesk/it",
        "host": "web01",
        "connected": True,
        "queue": "it",
    } in conns


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
        self.targets: list[str | None] = []
        self.controls: list[tuple[str, str]] = []

    def send_message(self, text: str, *, room: str | None = None) -> None:
        self.sent.append(text)
        self.targets.append(room)

    def send_control(self, kind: str, room: str) -> None:
        self.controls.append((kind, room))


def _send_stub(sources: dict[str, Any], agent_rooms: dict[str, str] | None = None):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_sources = sources
    s._agent_rooms = dict(agent_rooms or {})
    s.send_room_message = types.MethodType(Bridge.send_room_message, s)
    s._thread_control = types.MethodType(Bridge._thread_control, s)
    s.claim_thread = types.MethodType(Bridge.claim_thread, s)
    s.resolve_thread = types.MethodType(Bridge.resolve_thread, s)
    s.reopen_thread = types.MethodType(Bridge.reopen_thread, s)
    s.close_thread = types.MethodType(Bridge.close_thread, s)
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


def test_send_room_message_routes_conversation_via_agent_source():
    """A helpdesk conversation isn't a source key — it rides the agent source
    staffing its namespace, with the conversation named on the frame."""
    src = _FakeSource()
    stub = _send_stub({"helpdesk/it": src}, agent_rooms={"helpdesk/it": "agent@web01"})
    out = stub.send_room_message("helpdesk/it/alice@corp", "have you rebooted?")
    assert out == {"ok": True}
    assert src.sent == ["have you rebooted?"]
    assert src.targets == ["helpdesk/it/alice@corp"]


def test_send_room_message_unstaffed_conversation_fails():
    out = _send_stub({}, agent_rooms={}).send_room_message(
        "helpdesk/hr/carol@corp", "hi"
    )
    assert out["ok"] is False and "not connected" in out["error"]


# ── catalog publish dedup ──────────────────────────────────────────────────────


class _CatalogSource:
    def __init__(self) -> None:
        self.sends: list[list[dict]] = []

    def send_catalog(self, tools: list[dict]) -> None:
        self.sends.append(tools)


def _catalog_stub(runnables: list[dict[str, Any]]):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._runnables_cache = runnables
    s._catalog_index = {}
    s._published_catalogs = {}
    s._publish_catalog = types.MethodType(Bridge._publish_catalog, s)
    return s


def test_publish_catalog_skips_unchanged_then_sends_on_change():
    rn = {
        "name": "reset-password",
        "host": "web01",
        "description": "",
        "autonomy": "confirm",
        "args": [],
        "requireCommand": False,
        "rawSpec": {"discoverable": ["self-service"]},
    }
    s = _catalog_stub([rn])
    spec = {"room": "helpdesk/it", "queue": "it"}
    src = _CatalogSource()

    s._publish_catalog(spec, src)  # first publish → sends
    s._publish_catalog(spec, src)  # discovery refresh, nothing changed → skipped
    assert len(src.sends) == 1
    assert [t["name"] for t in src.sends[0]] == ["reset-password"]

    # A new self-service runnable appears → the next refresh re-sends.
    s._runnables_cache = [rn, {**rn, "name": "request-vm"}]
    s._publish_catalog(spec, src)
    assert len(src.sends) == 2
    assert [t["name"] for t in src.sends[1]] == ["reset-password", "request-vm"]


def test_publish_catalog_force_always_sends():
    s = _catalog_stub([])
    spec = {"room": "helpdesk/it", "queue": "it"}
    src = _CatalogSource()
    s._publish_catalog(spec, src, force=True)
    s._publish_catalog(spec, src, force=True)  # reconnect → resend even if empty
    assert src.sends == [[], []]


# ── thread controls (claim / resolve / reopen / close) ─────────────────────────


def test_thread_controls_route_via_agent_source():
    src = _FakeSource()
    stub = _send_stub({"helpdesk/it": src}, agent_rooms={"helpdesk/it": "agent@web01"})
    thread = "helpdesk/it/alice@corp"
    assert stub.claim_thread(thread) == {"ok": True}
    assert stub.resolve_thread(thread) == {"ok": True}
    assert stub.reopen_thread(thread) == {"ok": True}
    assert stub.close_thread(thread) == {"ok": True}
    assert src.controls == [
        ("claim", thread),
        ("resolve", thread),
        ("reopen", thread),
        ("close", thread),
    ]


def test_thread_control_refused_outside_staffed_queues():
    src = _FakeSource()
    stub = _send_stub({"helpdesk/it": src}, agent_rooms={"helpdesk/it": "agent@web01"})
    out = stub.claim_thread("helpdesk/hr/carol@corp")
    assert out["ok"] is False and "not staffing" in out["error"]
    assert src.controls == []


def test_thread_control_requires_a_room():
    out = _send_stub({}).claim_thread("")
    assert out["ok"] is False


# ── get_room_state ────────────────────────────────────────────────────────────


def _state_stub(
    specs: list[dict[str, Any]],
    sources: dict[str, Any],
    thread_states: dict[str, dict[str, Any]] | None = None,
):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_sources = sources
    s._thread_states = dict(thread_states or {})
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
            {
                "room": "ops",
                "host": "web01",
                "user": "alice@web01",
                "connected": True,
                "queue": "",
            },
            {
                "room": "dba",
                "host": "db01",
                "user": "alice@db01",
                "connected": False,
                "queue": "",
            },
        ]
    }


def test_get_room_state_queue_entry_carries_its_threads():
    specs = [
        {"room": "helpdesk/it", "host": "web01", "user": "agent@web01", "queue": "it"},
    ]
    threads = {
        "helpdesk/it/alice@corp": {"state": "open", "assignee": None},
        "helpdesk/hr/carol@corp": {"state": "open", "assignee": None},  # other queue
    }
    out = _state_stub(specs, {}, thread_states=threads).get_room_state()
    entry = out["rooms"][0]
    assert entry["queue"] == "it"
    # Only this queue's conversations are exposed on the entry.
    assert entry["threads"] == {
        "helpdesk/it/alice@corp": {"state": "open", "assignee": None}
    }


# ── _room_specs (host entries + legacy [chat] fallback) ───────────────────────


def _specs_stub(hosts: list[dict[str, Any]], ssh: dict[str, Any] | None = None):
    s = types.SimpleNamespace()
    s._hosts = hosts
    s._reload_hosts = lambda: None
    s.get_config = lambda: {"ssh": ssh} if ssh else {}
    s._room_specs = types.MethodType(Bridge._room_specs, s)
    return s


def test_room_specs_uses_ssh_default_user_for_bare_host(monkeypatch):
    from runspec_console import config

    monkeypatch.setattr(config, "read_chat", lambda: {})
    s = _specs_stub(
        [{"name": "web01", "ssh": "web01", "room": "ops", "room_port": 8765}],
        ssh={"user": "linuxuser"},
    )
    assert s._room_specs()[0]["user"] == "linuxuser@web01"


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


# ── connect-error hint (blocked port-forwarding → exec) ────────────────────────


def test_chat_connect_hint_for_blocked_forwarding():
    from runspec_console.bridge import _chat_connect_hint

    exc = Exception("ChannelException(1, 'Administratively prohibited')")
    hint = _chat_connect_hint(exc, via="tunnel")
    assert hint is not None and 'room_via = "exec"' in hint


def test_chat_connect_hint_none_in_exec_mode():
    from runspec_console.bridge import _chat_connect_hint

    # Already on exec → the prohibited-forwarding hint doesn't apply.
    exc = Exception("ChannelException(1, 'Administratively prohibited')")
    assert _chat_connect_hint(exc, via="exec") is None


def test_chat_connect_hint_none_for_other_errors():
    from runspec_console.bridge import _chat_connect_hint

    assert _chat_connect_hint(TimeoutError("timed out"), via="tunnel") is None


# ── room connection tester (classifier) ────────────────────────────────────────


def test_classify_room_connection_success():
    from runspec_console.bridge import _classify_room_connection

    r = _classify_room_connection(None, via="exec")
    assert r["ok"] and r["stage"] == "ok"


def test_classify_room_connection_proxy_timeout():
    from runspec_console.bridge import _classify_room_connection

    r = _classify_room_connection(TimeoutError("timed out"), via="exec")
    assert not r["ok"] and r["stage"] == "ssh"
    assert "proxy" in r["hint"].lower()


def test_classify_room_connection_blocked_forwarding():
    from runspec_console.bridge import _classify_room_connection

    exc = Exception("ChannelException(1, 'Administratively prohibited')")
    r = _classify_room_connection(exc, via="tunnel")
    assert not r["ok"] and r["stage"] == "channel"
    assert 'room_via = "exec"' in r["hint"]


def test_classify_room_connection_backoff():
    from runspec_console.bridge import _classify_room_connection

    from runspec_console.ssh_pool import BackoffError

    r = _classify_room_connection(
        BackoffError("host in connect-backoff for 4s"), via="exec"
    )
    assert not r["ok"] and r["stage"] == "ssh" and "backoff" in r["message"].lower()


def test_classify_room_connection_auth():
    from runspec_console.bridge import _classify_room_connection

    r = _classify_room_connection(Exception("Authentication failed."), via="exec")
    assert not r["ok"] and r["stage"] == "ssh" and "identityFile" in r["message"]


def test_test_room_connection_unconfigured_host():
    from runspec_console.bridge import Bridge

    s = types.SimpleNamespace()
    s._room_specs = lambda: []
    s.get_config = lambda: {}
    s.test_room_connection = types.MethodType(Bridge.test_room_connection, s)
    r = s.test_room_connection("nope")
    assert not r["ok"] and r["stage"] == "config"


def test_test_room_connection_fast_path_when_connected():
    from runspec_console.bridge import Bridge

    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._room_specs = lambda: [
        {"host": "web01", "room": "helpdesk/it", "queue": "it", "ssh_target": "a@web01"}
    ]
    s._chat_sources = {"helpdesk/it": object()}  # already connected
    s.test_room_connection = types.MethodType(Bridge.test_room_connection, s)
    r = s.test_room_connection("web01")
    assert r["ok"] and r["stage"] == "ok" and "Connected" in r["message"]
    # No SSH attempt happened — get_config was never needed.
