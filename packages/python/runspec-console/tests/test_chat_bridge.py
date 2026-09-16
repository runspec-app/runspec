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
import logging
import threading
import types
from typing import Any

from runspec_console.bridge import Bridge
from runspec_console.bridge_helpers import _RotaPresenceWatcher
from runspec_console.events import Event
from runspec_console.hosts import load_hosts


# ── save_jump_hosts preserves unmanaged keys (e.g. room_dest_host) ────────────


def test_save_jump_hosts_preserves_unmanaged_keys(tmp_path, monkeypatch):

    hosts_file = tmp_path / "runspec_hosts.toml"
    hosts_file.write_text(
        '[[host]]\nname = "web01"\nssh = "ops@web01"\n'
        'room = "ops"\nroom_port = 8765\nroom_dest_host = "localhost"\n'
        'runspec_paths = ["runspec"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("runspec_console.config.hosts_path", lambda: hosts_file)

    s = types.SimpleNamespace()
    s._MANAGED_HOST_KEYS = Bridge._MANAGED_HOST_KEYS
    s._reload_hosts = lambda: None
    s.restart_chat = lambda: None
    s._kick_refresh = lambda **kw: None  # spawned on a daemon thread by save
    s._dispatch = lambda *a, **k: None
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

    saved = {e["name"]: e for e in load_hosts(hosts_file)}
    assert saved["web01"].get("room_dest_host") == "localhost"  # preserved, not dropped
    assert saved["web01"]["room"] == "ops" and saved["web01"]["room_port"] == 8765


def _save_host_stub(bridge_mod, hosts_file, monkeypatch):
    monkeypatch.setattr("runspec_console.config.hosts_path", lambda: hosts_file)
    s = types.SimpleNamespace()
    s._MANAGED_HOST_KEYS = Bridge._MANAGED_HOST_KEYS
    s._reload_hosts = lambda: None
    s.restart_chat = lambda: None
    s._kick_refresh = lambda **kw: None  # spawned on a daemon thread by save
    s._dispatch = lambda *a, **k: None
    s.save_jump_hosts = types.MethodType(Bridge.save_jump_hosts, s)
    s.get_jump_hosts = types.MethodType(Bridge.get_jump_hosts, s)
    return s


def test_save_jump_hosts_writes_room_queue_and_catalog(tmp_path, monkeypatch):
    from runspec_console import bridge as bridge_mod

    hosts_file = tmp_path / "runspec_hosts.toml"
    s = _save_host_stub(bridge_mod, hosts_file, monkeypatch)

    # A queue-only host (no group-chat room) still needs the room port.
    s.save_jump_hosts(
        [
            {
                "name": "web01",
                "hostname": "web01",
                "user": "ops",
                "room_port": 8765,
                "room_queue": "it-support",
                "room_catalog": ["reset-password", "  ", "request-vm"],
                "runspec_paths": ["runspec"],
            }
        ]
    )

    saved = {e["name"]: e for e in load_hosts(hosts_file)}["web01"]
    assert saved["room_queue"] == "it-support"
    assert saved["room_port"] == 8765
    assert "room" not in saved  # queue without a group-chat room
    # Blank/whitespace catalog names are dropped.
    assert saved["room_catalog"] == ["reset-password", "request-vm"]


def test_save_jump_hosts_drops_queue_without_port(tmp_path, monkeypatch):
    from runspec_console import bridge as bridge_mod

    hosts_file = tmp_path / "runspec_hosts.toml"
    s = _save_host_stub(bridge_mod, hosts_file, monkeypatch)

    s.save_jump_hosts(
        [
            {
                "name": "web01",
                "hostname": "web01",
                "user": "ops",
                "room_queue": "it-support",  # no room_port → can't connect → drop
                "runspec_paths": ["runspec"],
            }
        ]
    )

    saved = {e["name"]: e for e in load_hosts(hosts_file)}["web01"]
    assert "room_queue" not in saved and "room_port" not in saved


def test_get_jump_hosts_round_trips_queue(tmp_path, monkeypatch):
    from runspec_console import bridge as bridge_mod

    hosts_file = tmp_path / "runspec_hosts.toml"
    hosts_file.write_text(
        '[[host]]\nname = "web01"\nssh = "ops@web01"\n'
        'room_port = 8765\nroom_queue = "it-support"\n'
        'room_catalog = ["reset-password"]\nrunspec_paths = ["runspec"]\n',
        encoding="utf-8",
    )
    s = _save_host_stub(bridge_mod, hosts_file, monkeypatch)
    s._hosts = load_hosts(hosts_file)
    s._reload_hosts = lambda: None

    entry = {e["name"]: e for e in s.get_jump_hosts()}["web01"]
    assert entry["room_queue"] == "it-support"
    assert entry["room_catalog"] == ["reset-password"]
    assert entry["room_port"] == 8765


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
    s._trigger_cards = {}
    s.dispatched: list[tuple[str, dict]] = []
    s.enacted: list[dict] = []
    s.paged: list[dict] = []
    s._dispatch = lambda ev, detail: s.dispatched.append((ev, detail))
    s._notify_room_message = lambda payload: None  # OS toast tested in test_notify
    s._notify_page = lambda payload: s.paged.append(payload)
    s._enact_assignment = lambda payload: s.enacted.append(payload)
    s._on_chat_event = types.MethodType(Bridge._on_chat_event, s)
    s._chat_event_for_engine = Bridge._chat_event_for_engine  # staticmethod
    s._parse_queue_room = Bridge._parse_queue_room  # staticmethod
    s._touch_queue_card = types.MethodType(Bridge._touch_queue_card, s)
    s._evict_trigger_cards = types.MethodType(Bridge._evict_trigger_cards, s)
    return s


_NS = "helpdesk/it"
_THREAD = "helpdesk/it/alice@corp"


def test_on_chat_event_message_reaches_ui_and_engine():
    engine = _FakeEngine()
    s = _event_stub(engine)
    s._on_chat_event(Event.new("chat", "message", {"room": "ops", "text": "hi"}))
    assert s.dispatched == [("runspec:room_message", {"room": "ops", "text": "hi"})]
    assert len(engine.fed) == 1 and engine.fed[0].type == "message"


def test_on_chat_event_run_message_feeds_a_run_event():
    # A "/run …" message is surfaced as its own chat/run event (logged, future-
    # matchable) — still a normal message to the Rooms UI.
    engine = _FakeEngine()
    s = _event_stub(engine)
    payload = {"room": _THREAD, "text": "/run clean the temp dir", "sender": "alice"}
    s._on_chat_event(Event.new("chat", "message", payload))
    assert ("runspec:room_message", payload) in s.dispatched  # UI sees a message
    assert len(engine.fed) == 1
    assert engine.fed[0].source == "chat" and engine.fed[0].type == "run"
    assert engine.fed[0].payload["text"] == "/run clean the temp dir"


def test_chat_event_for_engine_passes_plain_messages_through():
    msg = Event.new("chat", "message", {"text": "just chatting"})
    assert Bridge._chat_event_for_engine(msg, msg.payload) is msg  # unchanged


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


# ── runnables portal: assignment / request_status / page routing ───────────────


def test_on_chat_event_assignment_dispatches_and_enacts():
    s = _event_stub(_FakeEngine())
    payload = {"id": "r1", "room": _THREAD, "runnable": "reset-password", "args": {}}
    s._on_chat_event(Event.new("chat", "assignment", payload))
    assert ("runspec:room_assignment", payload) in s.dispatched
    assert s.enacted == [payload]  # routed to enactment


def test_on_chat_event_request_status_is_ui_only():
    engine = _FakeEngine()
    s = _event_stub(engine)
    payload = {"id": "r1", "room": _THREAD, "state": "done", "assignee": "agent@web01"}
    s._on_chat_event(Event.new("chat", "request_status", payload))
    assert s.dispatched == [("runspec:room_request_status", payload)]
    assert engine.fed == [] and s.enacted == []


def test_on_chat_event_page_dispatches_and_notifies():
    s = _event_stub(_FakeEngine())
    payload = {"id": "r1", "room": _THREAD, "runnable": "reset-password"}
    s._on_chat_event(Event.new("chat", "page", payload))
    assert ("runspec:room_page", payload) in s.dispatched
    assert s.paged == [payload]


def test_queue_message_feeds_engine_ungated_and_creates_card():
    """A queue message now feeds the engine with no claim gate (assignment is the
    gate), and still surfaces a first-class inbox card, updated in place."""
    engine = _FakeEngine()
    s = _event_stub(engine)
    s._on_chat_event(Event.new("chat", "message", {"room": _THREAD, "text": "help me"}))
    s._on_chat_event(Event.new("chat", "message", {"room": _THREAD, "text": "anyone?"}))
    assert len(engine.fed) == 2  # both fed — no claim gate
    cards = [d for ev, d in s.dispatched if ev == "runspec:trigger_card"]
    assert len({c["card_id"] for c in cards}) == 1  # one card, updated in place
    card = s._trigger_cards[f"queue:{_THREAD}"]
    assert card["summary"] == "anyone?" and card["trigger"] == "queue:it"


def test_group_chat_message_creates_no_queue_card():
    s = _event_stub(_FakeEngine())
    s._on_chat_event(Event.new("chat", "message", {"room": "ops", "text": "deploy?"}))
    assert s._trigger_cards == {}  # plain rooms are not queue conversations


# ── runnables portal: enacting an assigned request ─────────────────────────────


class _PortalSource:
    def __init__(self) -> None:
        self.starts: list[str] = []
        self.completes: list[tuple[str, str | None]] = []
        self.messages: list[tuple[str, str | None]] = []

    def send_request_start(self, request_id: str) -> None:
        self.starts.append(request_id)

    def send_request_complete(
        self, request_id: str, *, error: str | None = None
    ) -> None:
        self.completes.append((request_id, error))

    def send_message(self, text: str, *, room: str | None = None) -> None:
        self.messages.append((text, room))


_DISC = {"host": "web01", "name": "reset-password"}
# The portal index is namespace → name → {host: discovered runnable}.
_INDEX = {_NS: {"reset-password": {"web01": _DISC}}}


def _enact_stub(source, index, *, output="done ✓", raises=None):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_sources = {_NS: source} if source is not None else {}
    s._catalog_index = index
    s.gated_calls: list[tuple[str, dict, str]] = []

    def gated(tool_name, args, chat_id):
        s.gated_calls.append((tool_name, args, chat_id))
        if raises is not None:
            raise raises
        return output

    s._gated_run_tool = gated
    s._namespace_of = Bridge._namespace_of  # staticmethod
    s._portal_card_id = Bridge._portal_card_id  # staticmethod
    s._trigger_cards = {}
    s.cards: list[dict] = []  # every dispatched trigger-card snapshot
    s._dispatch = lambda ev, detail: (
        ev == "runspec:trigger_card" and s.cards.append(detail)
    )
    s._evict_trigger_cards = types.MethodType(Bridge._evict_trigger_cards, s)
    s._portal_card = types.MethodType(Bridge._portal_card, s)
    s._portal_fail = types.MethodType(Bridge._portal_fail, s)
    s._run_assignment = types.MethodType(Bridge._run_assignment, s)
    s._enact_assignment = types.MethodType(Bridge._enact_assignment, s)
    return s


def _card(s):
    """The single portal card's current state (helpers update one card in place)."""
    return s._trigger_cards.get("portal:r1")


def test_run_assignment_happy_path():
    src = _PortalSource()
    s = _enact_stub(src, _INDEX, output="all good")
    s._run_assignment("r1", _THREAD, "reset-password", {"user": "alice"}, _NS)
    # Ran the right tool through the gate (chat_id == the portal card id, so the
    # gate's Approve/Deny attaches to the card), posted the result, marked it done.
    assert s.gated_calls == [("web01__reset-password", {"user": "alice"}, "portal:r1")]
    assert src.starts == ["r1"]
    assert src.messages == [("all good", _THREAD)]
    assert src.completes == [("r1", None)]
    # The Triggers card walked running → done and captured the result for audit.
    card = _card(s)
    assert card["source"] == "portal" and card["column"] == "done"
    assert card["detail"]["state"] == "done" and card["detail"]["result"] == "all good"
    # running came before done.
    assert [c["column"] for c in s.cards] == ["running", "done"]


def test_run_assignment_failure_output_marks_failed():
    src = _PortalSource()
    s = _enact_stub(src, _INDEX, output="[exit 1]\nboom")
    s._run_assignment("r1", _THREAD, "reset-password", {}, _NS)
    assert src.messages == [("[exit 1]\nboom", _THREAD)]
    rid, error = src.completes[0]
    assert rid == "r1" and error == "[exit 1]\nboom"  # error flags the failure
    assert (
        _card(s)["column"] == "failed"
        and _card(s)["detail"]["error"] == "[exit 1]\nboom"
    )


def test_run_assignment_unknown_runnable_completes_with_error():
    src = _PortalSource()
    s = _enact_stub(src, {_NS: {}})  # the runnable isn't in this queue's index
    s._run_assignment("r1", _THREAD, "reset-password", {}, _NS)
    assert s.gated_calls == [] and src.starts == []
    rid, error = src.completes[0]
    assert rid == "r1" and "no longer available" in error
    assert _card(s)["column"] == "failed"


def test_run_assignment_gate_exception_completes_with_error():
    src = _PortalSource()
    s = _enact_stub(src, _INDEX, raises=RuntimeError("ssh down"))
    s._run_assignment("r1", _THREAD, "reset-password", {}, _NS)
    assert src.starts == ["r1"]
    assert src.completes == [("r1", "ssh down")]


def test_run_assignment_picks_requested_host():
    src = _PortalSource()
    index = {
        _NS: {
            "check-disk": {
                "web01": {"host": "web01", "name": "check-disk"},
                "web02": {"host": "web02", "name": "check-disk"},
            }
        }
    }
    s = _enact_stub(src, index, output="ok")
    # Operator picked web02 → the gate runs web02__check-disk, not the first host.
    s._run_assignment("r1", _THREAD, "check-disk", {}, _NS, "web02")
    assert s.gated_calls == [("web02__check-disk", {}, "portal:r1")]


def test_run_assignment_unknown_host_fails():
    src = _PortalSource()
    index = {_NS: {"check-disk": {"web01": {"host": "web01", "name": "check-disk"}}}}
    s = _enact_stub(src, index)
    s._run_assignment("r1", _THREAD, "check-disk", {}, _NS, "web09")
    assert s.gated_calls == [] and src.starts == []
    rid, error = src.completes[0]
    assert rid == "r1" and "not available on web09" in error


def _runbook_enact_stub(source, runbook_index, *, summary="ran ✓"):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_sources = {_NS: source} if source is not None else {}
    s._runbook_index = runbook_index
    s.turns: list[tuple[str, str]] = []

    def trigger_turn(card_id, system, message, *, draft, record=None, trigger=""):
        s.turns.append((card_id, message))
        if record is not None:
            record("working")
        return summary

    s._trigger_run_agent_turn = trigger_turn
    s._base_system = lambda: "SYS"
    s._namespace_of = Bridge._namespace_of
    s._portal_card_id = Bridge._portal_card_id
    s._trigger_cards = {}
    s.cards: list[dict] = []
    s._dispatch = lambda ev, detail: (
        ev == "runspec:trigger_card" and s.cards.append(detail)
    )
    s._evict_trigger_cards = types.MethodType(Bridge._evict_trigger_cards, s)
    s._portal_card = types.MethodType(Bridge._portal_card, s)
    s._portal_fail = types.MethodType(Bridge._portal_fail, s)
    s._run_runbook_assignment = types.MethodType(Bridge._run_runbook_assignment, s)
    return s


def test_run_runbook_assignment_renders_and_runs_agent_turn(monkeypatch):
    from runspec_console import memory, runbookargs

    monkeypatch.setattr(
        memory,
        "load_runbook",
        lambda sid: {"id": "restart", "title": "Restart", "steps": "do {{svc}}"},
    )
    monkeypatch.setattr(runbookargs, "validate_runbook_values", lambda raw, vals: [])
    src = _PortalSource()
    s = _runbook_enact_stub(
        src, {_NS: {"runbook:restart": "restart"}}, summary="done ✓"
    )
    s._run_runbook_assignment("r1", _THREAD, "runbook:restart", {"svc": "nginx"}, _NS)
    # The runbook body was rendered with the visitor's values and run as one agent
    # turn keyed to the portal card; the summary was posted + the request completed.
    assert len(s.turns) == 1
    card_id, message = s.turns[0]
    assert card_id == "portal:r1" and "do nginx" in message
    assert src.starts == ["r1"]
    assert src.messages == [("done ✓", _THREAD)]
    assert src.completes == [("r1", None)]
    assert _card(s)["column"] == "done"


def test_run_runbook_assignment_unknown_runbook_fails(monkeypatch):
    src = _PortalSource()
    s = _runbook_enact_stub(src, {_NS: {}})  # nothing in the runbook index
    s._run_runbook_assignment("r1", _THREAD, "runbook:gone", {}, _NS)
    assert s.turns == [] and src.starts == []
    rid, error = src.completes[0]
    assert rid == "r1" and "no longer available" in error
    assert _card(s)["column"] == "failed"


def test_run_runbook_assignment_invalid_values_fail(monkeypatch):
    from runspec_console import memory, runbookargs

    monkeypatch.setattr(
        memory, "load_runbook", lambda sid: {"id": "restart", "steps": "x", "args": {}}
    )
    monkeypatch.setattr(
        runbookargs, "validate_runbook_values", lambda raw, vals: ["svc is required"]
    )
    src = _PortalSource()
    s = _runbook_enact_stub(src, {_NS: {"runbook:restart": "restart"}})
    s._run_runbook_assignment("r1", _THREAD, "runbook:restart", {}, _NS)
    assert s.turns == []  # never ran the agent turn
    rid, error = src.completes[0]
    assert rid == "r1" and "svc is required" in error


def test_enact_assignment_dispatches_runbook_kind(monkeypatch):
    # An assignment with kind="runbook" routes to the runbook worker, not the runnable
    # one. We stub both workers to record which fired.
    s = _enact_stub(_PortalSource(), {_NS: {}})
    fired: list[str] = []
    s._run_assignment = lambda *a, **k: fired.append("runnable")
    s._run_runbook_assignment = lambda *a, **k: fired.append("runbook")
    s._enact_assignment(
        {
            "id": "r1",
            "room": _THREAD,
            "runnable": "runbook:restart",
            "args": {},
            "kind": "runbook",
        }
    )
    # Give the worker thread a moment to run.
    import time as _t

    for _ in range(50):
        if fired:
            break
        _t.sleep(0.005)
    assert fired == ["runbook"]
    # The card shows the bare runbook id, not the runbook:<id> wire name.
    assert _card(s)["detail"]["runnable"] == "restart"


def test_enact_assignment_ignores_malformed_payload():
    src = _PortalSource()
    s = _enact_stub(src, {_NS: {"reset-password": _DISC}})
    s._enact_assignment({"id": "r1", "room": "not-a-helpdesk-room", "runnable": "x"})
    assert s.gated_calls == [] and src.starts == []
    assert s._trigger_cards == {}  # no card for a malformed assignment


def test_enact_assignment_creates_inbox_card_keyed_to_the_gate():
    src = _PortalSource()
    s = _enact_stub(src, {_NS: {"reset-password": _DISC}})
    s._run_assignment = lambda *a, **k: None  # don't spawn the worker in this test
    s._enact_assignment(
        {
            "id": "r1",
            "room": _THREAD,
            "runnable": "reset-password",
            "args": {"user": "alice"},
        }
    )
    card = _card(s)
    # The card lands in the Triggers inbox immediately, before the (gated) run.
    assert card is not None and card["source"] == "portal"
    # card_id MUST equal the autonomy-gate chat_id so the Approve/Deny prompt
    # (runspec:tool_confirm id=chat_id) renders on this card.
    assert card["card_id"] == Bridge._portal_card_id("r1") == "portal:r1"
    assert card["detail"]["runnable"] == "reset-password"
    assert card["detail"]["args"] == {"user": "alice"}
    assert card["detail"]["visitor"] == "alice@corp"
    assert card["detail"]["state"] == "assigned" and card["column"] == "triggered"
    assert card["summary"] == "reset-password · alice@corp"


def test_enact_assignment_card_shows_verified_visitor_name():
    src = _PortalSource()
    s = _enact_stub(src, {_NS: {"reset-password": _DISC}})
    s._run_assignment = lambda *a, **k: None
    s._enact_assignment(
        {
            "id": "r1",
            "room": _THREAD,
            "runnable": "reset-password",
            "args": {"user": "alice"},
            "visitor": "alice@corp",
            "visitor_name": "Alice Smith",
        }
    )
    card = _card(s)
    # Both the verified email (the anchor) and the display name reach the card…
    assert card["detail"]["visitor"] == "alice@corp"
    assert card["detail"]["visitor_name"] == "Alice Smith"
    # …and the summary line prefers the human name.
    assert card["summary"] == "reset-password · Alice Smith"


def test_namespace_of():
    assert Bridge._namespace_of("helpdesk/it/alice@corp") == "helpdesk/it"
    assert Bridge._namespace_of("ops") == ""


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
    s._chat_connect_log = {}
    s._agent_rooms = {}
    s._published_catalogs = {}
    s._trigger_engine = None
    s.dispatched: list[tuple[str, dict]] = []
    s._dispatch = lambda ev, detail: s.dispatched.append((ev, detail))
    s._build_clock_ok = lambda: lambda: True
    s._CHAT_BACKOFF_START = 0.01
    s._CHAT_BACKOFF_MAX = 0.01
    s._CHAT_MIN_HEALTHY = (
        0.0  # any uptime counts as healthy (keep reconnect tests tight)
    )
    s._on_chat_event = types.MethodType(Bridge._on_chat_event, s)
    s._chat_supervise = types.MethodType(Bridge._chat_supervise, s)
    s.published: list[str] = []
    s._publish_catalog = lambda spec, source, *, force=False: s.published.append(
        spec["room"]
    )
    s.cues: list[tuple[str, str, bool]] = []
    s._connection_cue = lambda kind, name, connected: s.cues.append(
        (kind, name, connected)
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


def test_chat_supervisor_flapping_connection_backs_off_and_logs_once(caplog):
    """A room that opens the channel then drops it immediately (e.g. it rejects the
    console because the queue mailbox can't be watched) is flapping: the supervisor
    must NOT reset the backoff on every open (which would spin a tight reconnect +
    catalog-republish loop), and must log the reason once — no traceback."""
    import logging
    import time

    s = _supervisor_stub()
    s._CHAT_MIN_HEALTHY = 100.0  # nothing stays up this long → every drop is a flap

    def factory():
        return _OneShotTransport()  # opens, then recv → None (immediate drop)

    s._chat_stop.clear()
    with caplog.at_level(logging.WARNING, logger="runspec.console.triggers.chat"):
        th = threading.Thread(
            target=s._chat_supervise, args=(_SPEC, factory), daemon=True
        )
        th.start()
        time.sleep(0.3)  # several flap cycles at the 0.01s backoff
        s._chat_stop.set()
        th.join(timeout=2)

    flaps = [
        r
        for r in caplog.records
        if "closed the connection right after it opened" in r.getMessage()
    ]
    assert len(flaps) == 1  # logged once despite repeated flapping reconnects
    assert all(r.exc_info is None for r in flaps)  # no traceback


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

    def send_message(self, text: str, *, room: str | None = None) -> None:
        self.sent.append(text)
        self.targets.append(room)


def _send_stub(sources: dict[str, Any], agent_rooms: dict[str, str] | None = None):
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_sources = sources
    s._agent_rooms = dict(agent_rooms or {})
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
    s._runbook_index = {}
    s._published_catalogs = {}
    s._publish_catalog = types.MethodType(Bridge._publish_catalog, s)
    s._visible_runnables = types.MethodType(Bridge._visible_runnables, s)
    s._build_room_runbooks = types.MethodType(Bridge._build_room_runbooks, s)
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


def test_forced_resend_of_unchanged_catalog_does_not_log_info(caplog):
    """A flapping room reconnects and force-republishes the same catalog every
    cycle; that must not spam INFO "published N tool(s)" — only a genuine change
    is announced at INFO (the unchanged re-send drops to DEBUG)."""
    s = _catalog_stub([])
    spec = {"room": "helpdesk/it", "queue": "it"}
    src = _CatalogSource()
    with caplog.at_level(logging.INFO, logger="runspec_console.bridge_const"):
        s._publish_catalog(spec, src, force=True)  # first connect → INFO
        s._publish_catalog(spec, src, force=True)  # reconnect, unchanged → no INFO
        s._publish_catalog(spec, src, force=True)  # reconnect, unchanged → no INFO
    published = [r for r in caplog.records if "published" in r.getMessage()]
    assert len(published) == 1  # announced once, not once per reconnect
    assert src.sends == [[], [], []]  # but the catalog was re-sent every time


# ── get_room_state ────────────────────────────────────────────────────────────


def _state_stub(
    specs: list[dict[str, Any]],
    sources: dict[str, Any],
    active: list[dict[str, Any]] | None = None,
):
    # ``active`` = the room specs the console is selected to staff (defaults to all,
    # reproducing the "every configured room is active" case).
    s = types.SimpleNamespace()
    s._lock = threading.Lock()
    s._chat_sources = sources
    s._room_specs = lambda: specs
    s._active_room_specs = lambda: specs if active is None else active
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
                "active": True,
                "connected": True,
                "queue": "",
            },
            {
                "room": "dba",
                "host": "db01",
                "user": "alice@db01",
                "active": True,
                "connected": False,
                "queue": "",
            },
        ]
    }


def test_get_room_state_marks_queue_entry():
    specs = [
        {"room": "helpdesk/it", "host": "web01", "user": "agent@web01", "queue": "it"},
    ]
    out = _state_stub(specs, {"helpdesk/it": _FakeSource()}).get_room_state()
    entry = out["rooms"][0]
    assert entry["queue"] == "it" and entry["connected"] is True and entry["active"]
    # The claim-based per-thread state is gone — request lifecycle rides events.
    assert "threads" not in entry


def test_get_room_state_standby_not_connected():
    # Two redundant hosts share the namespace; only the active one is connected —
    # a namespace-only check would flip the standby green off the live source.
    specs = [
        {"room": "helpdesk/hd", "host": "web01", "user": "a@web01", "queue": "hd"},
        {"room": "helpdesk/hd", "host": "web02", "user": "a@web02", "queue": "hd"},
    ]
    out = _state_stub(
        specs, {"helpdesk/hd": _FakeSource()}, active=[specs[0]]
    ).get_room_state()
    web01, web02 = out["rooms"]
    assert web01["active"] is True and web01["connected"] is True
    assert web02["active"] is False and web02["connected"] is False  # standby


def test_get_room_state_detached_when_none_active():
    specs = [{"room": "helpdesk/hd", "host": "web01", "user": "a@web01", "queue": "hd"}]
    # Nothing selected (empty active set) → no room is active/connected even though
    # a source lingers.
    out = _state_stub(specs, {"helpdesk/hd": _FakeSource()}, active=[]).get_room_state()
    assert out["rooms"][0]["active"] is False
    assert out["rooms"][0]["connected"] is False


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
    from runspec_console.bridge_helpers import _chat_connect_hint

    exc = Exception("ChannelException(1, 'Administratively prohibited')")
    hint = _chat_connect_hint(exc, via="tunnel")
    assert hint is not None and 'room_via = "exec"' in hint


def test_chat_connect_hint_none_in_exec_mode():
    from runspec_console.bridge_helpers import _chat_connect_hint

    # Already on exec → the prohibited-forwarding hint doesn't apply.
    exc = Exception("ChannelException(1, 'Administratively prohibited')")
    assert _chat_connect_hint(exc, via="exec") is None


def test_chat_connect_hint_none_for_other_errors():
    from runspec_console.bridge_helpers import _chat_connect_hint

    assert _chat_connect_hint(TimeoutError("timed out"), via="tunnel") is None


def test_chat_connect_summary_room_server_down_is_quiet():
    from runspec_console.bridge_helpers import _chat_connect_summary

    # paramiko's ChannelException(2, 'Connect failed') when the room port isn't
    # listening (the room server is stopped) → concise, no traceback.
    exc = Exception("ChannelException(2, 'Connect failed')")
    message, want_traceback = _chat_connect_summary(exc, via="tunnel")
    assert want_traceback is False
    assert "room server isn't reachable" in message


def test_chat_connect_summary_blocked_forwarding_reuses_hint():
    from runspec_console.bridge_helpers import _chat_connect_summary

    exc = Exception("ChannelException(1, 'Administratively prohibited')")
    message, want_traceback = _chat_connect_summary(exc, via="tunnel")
    assert want_traceback is False
    assert 'room_via = "exec"' in message


def test_chat_connect_summary_unknown_keeps_traceback():
    from runspec_console.bridge_helpers import _chat_connect_summary

    message, want_traceback = _chat_connect_summary(
        Exception("totally unexpected boom"), via="tunnel"
    )
    assert want_traceback is True
    assert "boom" in message


def test_chat_supervisor_logs_room_down_once_no_traceback(caplog):
    """A stopped room server (connect raises Connect failed) logs a single concise
    WARNING across many backoff retries — not a traceback every cycle."""
    import logging
    import time

    s = _supervisor_stub()

    def factory():
        raise Exception("ChannelException(2, 'Connect failed')")

    s._chat_stop.clear()
    with caplog.at_level(logging.WARNING, logger="runspec.console.triggers.chat"):
        th = threading.Thread(
            target=s._chat_supervise, args=(_SPEC, factory), daemon=True
        )
        th.start()
        # Let it churn through several backoff cycles (start/max are 0.01s).
        time.sleep(0.3)
        s._chat_stop.set()
        th.join(timeout=2)

    room_down = [
        r for r in caplog.records if "room server isn't reachable" in r.getMessage()
    ]
    assert len(room_down) == 1  # logged once, despite many reconnect attempts
    assert all(r.exc_info is None for r in room_down)  # no traceback spilled


# ── room connection tester (classifier) ────────────────────────────────────────


def test_classify_room_connection_success():
    from runspec_console.bridge_helpers import _classify_room_connection

    r = _classify_room_connection(None, via="exec")
    assert r["ok"] and r["stage"] == "ok"


def test_classify_room_connection_proxy_timeout():
    from runspec_console.bridge_helpers import _classify_room_connection

    r = _classify_room_connection(TimeoutError("timed out"), via="exec")
    assert not r["ok"] and r["stage"] == "ssh"
    assert "proxy" in r["hint"].lower()


def test_classify_room_connection_blocked_forwarding():
    from runspec_console.bridge_helpers import _classify_room_connection

    exc = Exception("ChannelException(1, 'Administratively prohibited')")
    r = _classify_room_connection(exc, via="tunnel")
    assert not r["ok"] and r["stage"] == "channel"
    assert 'room_via = "exec"' in r["hint"]


def test_classify_room_connection_backoff():
    from runspec_console.bridge_helpers import _classify_room_connection

    from runspec_console.ssh_pool import BackoffError

    r = _classify_room_connection(
        BackoffError("host in connect-backoff for 4s"), via="exec"
    )
    assert not r["ok"] and r["stage"] == "ssh" and "backoff" in r["message"].lower()


def test_classify_room_connection_auth():
    from runspec_console.bridge_helpers import _classify_room_connection

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
