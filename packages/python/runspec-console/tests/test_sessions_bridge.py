"""
test_sessions_bridge.py — sessions on the Bridge: the registry + its public
methods, two interactive turns in flight at once, the one engine reporting the
same way for a headless (trigger) turn as for a chat turn, cards opening /
finishing their sessions, per-session workspaces, and stop_triggers covering
schedule cards.
"""

from __future__ import annotations

import asyncio
import threading
import types
from types import SimpleNamespace

from runspec_console.adapters.base import ToolCall
from runspec_console.bridge import Bridge
from runspec_console.bridge_turn import TurnSpec
from runspec_console.session import new_automation
from tests.session_stub import agentic_shim, current as _S, install_sessions


def _resp(stop_reason="end_turn", tool_calls=None, inp=10, out=5):
    return SimpleNamespace(
        stop_reason=stop_reason,
        tool_calls=tool_calls or [],
        _raw=SimpleNamespace(
            usage=SimpleNamespace(input_tokens=inp, output_tokens=out)
        ),
    )


class _Stub:
    """Bare bridge with the real engine + registry bound (see session_stub)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._chat_cancels: dict[str, threading.Event] = {}
        self._pending_confirms: dict = {}
        self._in_flight: dict = {}
        self._run_origin: dict = {}
        self.dispatched: list = []
        self.audits: list = []
        install_sessions(self)
        self._agentic_chat_turn = agentic_shim(self)

    def _dispatch(self, event, detail):
        self.dispatched.append((event, detail))

    def _persist_conversation_turn(self, *a, **k):
        pass

    def _play_sound(self, *a, **k):
        pass

    def _audit_chat_turn(self, **kw):
        self.audits.append(kw)

    def _window_is_foreground(self):
        return True

    def _runnables_to_tools(self, *a, **k):
        return []

    def get_config(self):
        return {}

    async def _gated_run_tool_async(
        self, name, tool_input, chat_id, stage=None, on_decision=None, headless=False
    ):
        return f"ran:{name}"

    _tool_mode = Bridge._tool_mode
    _chat_index_text = staticmethod(lambda: "")
    _debug = False
    _usage_from_response = staticmethod(Bridge._usage_from_response)
    _messages_chars = staticmethod(Bridge._messages_chars)
    _context_window = staticmethod(Bridge._context_window)
    _trace = Bridge._trace
    cancel_chat = Bridge.cancel_chat
    _max_tool_rounds = Bridge._max_tool_rounds
    _max_continuations = Bridge._max_continuations
    _max_network_retries = Bridge._max_network_retries
    _max_self_recoveries = Bridge._max_self_recoveries
    _append_recovery_prompt = Bridge._append_recovery_prompt

    def events(self, name):
        return [d for e, d in self.dispatched if e == name]


class _Text:
    def __init__(self, reply="ok"):
        self.reply = reply

    async def stream_with_tools(self, messages, tools):
        yield ("text", self.reply)
        yield ("done", _resp())

    def make_tool_turn(self, response, results):
        return []


class _Blocking:
    """Streams only once ``release`` is set — holds a turn in flight."""

    def __init__(self, release: threading.Event, started: threading.Event):
        self.release, self.started = release, started

    async def stream_with_tools(self, messages, tools):
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        yield ("text", "done")
        yield ("done", _resp())

    def make_tool_turn(self, response, results):
        return []


# ── registry + public methods ─────────────────────────────────────────────────


def test_new_select_list_close():
    b = _Stub()
    first = b.new_chat_session()
    second = b.new_chat_session()
    assert b._chat_session_id == second["id"]
    assert [s["id"] for s in b.list_chat_sessions()] == [first["id"], second["id"]]
    assert b.select_chat_session(first["id"])["ok"] is True
    assert b._chat_session_id == first["id"]
    assert b.select_chat_session("nope")["ok"] is False
    assert b.close_chat_session(second["id"]) == {"ok": True}
    assert [s["id"] for s in b.list_chat_sessions()] == [first["id"]]
    closed = [d for d in b.events("runspec:session") if d["id"] == second["id"]]
    assert closed[-1]["state"] == "closed"


def test_select_refuses_automation_sessions():
    b = _Stub()
    b._open_automation_session("trigger", "card-1", "nightly")
    res = b.select_chat_session("card-1")
    assert res["ok"] is False and "read-only" in res["error"]


def test_chat_session_is_created_lazily_and_announced():
    b = _Stub()
    s = _S(b)
    assert b._chat_session_id == s.id and s.interactive
    assert b.events("runspec:session")[0]["id"] == s.id


# ── concurrent interactive sessions ───────────────────────────────────────────


def test_two_sessions_can_each_have_a_turn_in_flight():
    b = _Stub()
    a = b.new_chat_session()["id"]
    c = b.new_chat_session()["id"]
    rel, st_a, st_c = threading.Event(), threading.Event(), threading.Event()

    def run(session_id, chat_id, adapter):
        spec = TurnSpec(
            chat_id=chat_id,
            session=b._sessions[session_id],
            message=f"hi from {chat_id}",
            adapter=adapter,
            tools=[],
        )
        asyncio.run(b._run_agent_turn(spec))

    ta = threading.Thread(target=run, args=(a, "ta", _Blocking(rel, st_a)))
    tc = threading.Thread(target=run, args=(c, "tc", _Blocking(rel, st_c)))
    ta.start()
    tc.start()
    assert st_a.wait(2) and st_c.wait(2)
    # Both running at once — neither session's lock blocks the other.
    assert b._sessions[a].state == "running" and b._sessions[c].state == "running"
    assert b._turn_sessions == {"ta": a, "tc": c}
    rel.set()
    ta.join(2)
    tc.join(2)
    assert b._sessions[a].state == "idle" and b._sessions[c].state == "idle"
    assert [m["content"] for m in b._sessions[a].history] == ["hi from ta", "done"]
    assert [m["content"] for m in b._sessions[c].history] == ["hi from tc", "done"]
    ends = {d["id"]: d for d in b.events("runspec:run_end")}
    assert ends["ta"]["session"] == a and ends["tc"]["session"] == c
    assert b._turn_sessions == {}


def test_new_chat_leaves_the_running_session_alone():
    b = _Stub()
    rel, started = threading.Event(), threading.Event()
    first = _S(b)
    t = threading.Thread(
        target=lambda: asyncio.run(
            b._agentic_chat_turn("c1", "long task", _Blocking(rel, started))
        )
    )
    t.start()
    assert started.wait(2)
    fresh = b.new_chat_session()  # returns immediately — no wait on the lock
    assert fresh["id"] != first.id and b._chat_session_id == fresh["id"]
    assert first.state == "running"
    rel.set()
    t.join(2)
    assert first.state == "idle" and first.history[-1]["content"] == "done"


def test_close_running_session_stops_its_turn():
    b = _Stub()
    rel, started = threading.Event(), threading.Event()
    s = _S(b)
    b._chat_cancels["c1"] = threading.Event()
    t = threading.Thread(
        target=lambda: asyncio.run(
            b._agentic_chat_turn("c1", "go", _Blocking(rel, started))
        )
    )
    t.start()
    assert started.wait(2)
    assert b.close_chat_session(s.id)["ok"] is True
    assert b._chat_cancels["c1"].is_set()  # Stop was pressed for it
    rel.set()
    t.join(2)
    assert s.id not in b._sessions
    # The turn's own end does not resurrect the closed session's event.
    assert [d for d in b.events("runspec:session") if d["id"] == s.id][-1][
        "state"
    ] == "closed"


# ── one engine: a headless turn reports like a chat turn ──────────────────────


def test_headless_turn_emits_usage_audit_and_summary():
    b = _Stub()
    session = b._open_automation_session("trigger", "card-7", "nightly · new_mail")
    record: list[str] = []
    stages: list[str] = []

    class _ToolThenText:
        calls = 0

        async def stream_with_tools(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield (
                    "done",
                    _resp("tool_use", [ToolCall(id="t1", name="h__svc", input={})]),
                )
            else:
                yield ("text", "all good")
                yield ("done", _resp())

        def make_tool_turn(self, response, results):
            return [
                {"role": "assistant", "content": "tool"},
                {"role": "user", "content": "result"},
            ]

    spec = TurnSpec(
        chat_id="card-7",
        session=session,
        message="event arrived",
        adapter=_ToolThenText(),
        tools=[],
        kind="trigger",
        interactive=False,
        headless_confirm=True,
        record=record.append,
        stage=stages.append,
        pause_hint="re-run to continue",
    )
    result = asyncio.run(b._run_agent_turn(spec))
    assert result.summary == "• svc → ran:h__svc\n\nall good"
    assert record == ["svc → ran:h__svc"]
    # The same reporting a chat turn gets: usage (with the session), run_end
    # carrying `stopped`, an audit line, and the session lifecycle.
    usage = b.events("runspec:chat_usage")[0]
    assert usage["id"] == "card-7" and usage["session"] == "card-7"
    assert usage["input_tokens"] == 20 and usage["output_tokens"] == 10
    end = b.events("runspec:run_end")[0]
    assert end == {
        "id": "card-7",
        "session": "card-7",
        "exit_code": 0,
        "duration_ms": end["duration_ms"],
        "stopped": False,
    }
    assert b.audits and b.audits[0]["chat_id"] == "card-7"
    assert b.audits[0]["error"] is False
    states = [d["state"] for d in b.events("runspec:session") if d["id"] == "card-7"]
    assert states == ["idle", "running", "done"]
    tool_end = b.events("runspec:tool_end")[0]
    assert tool_end["output_chars"] == len("ran:h__svc")  # the richer payload


def test_engine_error_is_reported_and_reraised_only_for_headless():
    class _Boom:
        async def stream_with_tools(self, messages, tools):
            yield (
                "done",
                _resp("tool_use", [ToolCall(id="t1", name="h__svc", input={})]),
            )

        def make_tool_turn(self, response, results):
            raise RuntimeError("adapter broke")

    b = _Stub()
    # Interactive: surfaced in the transcript, run_end exit 1, no raise.
    asyncio.run(b._agentic_chat_turn("c1", "go", _Boom()))
    tokens = "".join(d["token"] for d in b.events("runspec:token"))
    assert "⚠ Error: RuntimeError: adapter broke" in tokens
    assert b.events("runspec:run_end")[-1]["exit_code"] == 1
    assert b.audits[-1]["error"] is True
    assert _S(b).state == "idle"
    # Headless: the same report, then re-raised so the card-failure path fires.
    session = b._open_automation_session("schedule", "card-9", "job")
    spec = TurnSpec(
        chat_id="card-9",
        session=session,
        message="go",
        adapter=_Boom(),
        tools=[],
        kind="schedule",
        interactive=False,
    )
    try:
        asyncio.run(b._run_agent_turn(spec))
    except RuntimeError as exc:
        assert "adapter broke" in str(exc)
    else:
        raise AssertionError("headless turn must re-raise")
    assert b.events("runspec:run_end")[-1] == {
        "id": "card-9",
        "session": "card-9",
        "exit_code": 1,
        "duration_ms": b.events("runspec:run_end")[-1]["duration_ms"],
        "stopped": False,
    }


def test_send_chat_to_automation_session_is_refused_visibly():
    b = _Stub()
    b._open_automation_session("trigger", "card-1", "nightly")
    b._spawn_chat_turn = types.MethodType(Bridge._spawn_chat_turn, b)
    chat_id = b._spawn_chat_turn("hello", session_id="card-1")
    for _ in range(200):
        if b.events("runspec:run_end"):
            break
        threading.Event().wait(0.01)
    assert b.events("runspec:run_end") == [
        {"id": chat_id, "exit_code": 1, "duration_ms": 0}
    ]
    assert "read-only" in b.events("runspec:token")[0]["token"]
    assert chat_id not in b._chat_cancels  # cleaned up


# ── cards open + finish their sessions; workspaces follow ─────────────────────


def _card_stub():
    ns = types.SimpleNamespace()
    ns._lock = threading.Lock()
    ns.dispatched = []
    ns._dispatch = lambda ev, payload: ns.dispatched.append((ev, payload))
    install_sessions(ns)
    ns._sync_card_session = types.MethodType(Bridge._sync_card_session, ns)
    ns._current_workspace_root = types.MethodType(Bridge._current_workspace_root, ns)
    return ns


def test_card_sighting_opens_session_and_terminal_column_finishes_it(monkeypatch):
    ns = _card_stub()
    monkeypatch.setattr(
        "runspec_console.config.workspace_dir", lambda sid: f"/ws/{sid}"
    )
    card = {
        "card_id": "card-1",
        "trigger": "nightly",
        "event_type": "new_mail",
        "column": "triggered",
    }
    ns._sync_card_session("trigger", card)
    s = ns._sessions["card-1"]
    assert s.kind == "trigger" and s.label == "nightly · new_mail" and s.state == "idle"
    # A rule-mode run under this card spills into the card's own workspace —
    # never the operator's chat workspace.
    assert str(ns._current_workspace_root("card-1")) == "/ws/trigger-card-1"
    ns._sync_card_session("trigger", {**card, "column": "running"})
    assert ns._sessions["card-1"] is s  # re-sighting keeps the entry
    ns._sync_card_session("trigger", {**card, "column": "done"})
    assert s.state == "done"
    sched = {
        "card_id": "inv-1",
        "trigger": "job",
        "event_type": "runnable",
        "column": "running",
    }
    ns._sync_card_session("schedule", sched)
    assert ns._sessions["inv-1"].workspace_id == "schedule-inv-1"


def test_unknown_turn_falls_back_to_the_chat_workspace(monkeypatch):
    ns = _card_stub()
    monkeypatch.setattr(
        "runspec_console.config.workspace_dir", lambda sid: f"/ws/{sid}"
    )
    root = str(ns._current_workspace_root("no-such-turn"))
    assert root == f"/ws/{ns._chat_session_id}"


def test_finished_automation_sessions_are_capped():
    ns = _card_stub()
    for i in range(15):
        ns._sync_card_session(
            "trigger",
            {"card_id": f"c{i}", "trigger": "t", "event_type": "e", "column": "done"},
        )
        ns._sessions[f"c{i}"].created_at = float(i)
    finished = [s for s in ns._sessions.values() if s.state == "done"]
    assert len(finished) == 12
    closed = [
        d["id"]
        for e, d in ns.dispatched
        if e == "runspec:session" and d.get("state") == "closed"
    ]
    assert closed == ["c0", "c1", "c2"]


def test_stop_triggers_aborts_schedule_cards_too():
    ns = types.SimpleNamespace()
    ns._lock = threading.Lock()
    ns._trigger_engine = None
    ns._schedule_source = None
    ns._trigger_cards = {"t1": {}}
    ns._schedule_cards = {"s1": {}}
    ns._chat_cancels = {
        "t1": threading.Event(),
        "s1": threading.Event(),
        "chat": threading.Event(),
    }
    ns._pending_confirms = {}
    ns._abort_turns = types.MethodType(Bridge._abort_turns, ns)
    ns.stop_triggers = types.MethodType(Bridge.stop_triggers, ns)
    ns.stop_triggers()
    assert ns._chat_cancels["t1"].is_set() and ns._chat_cancels["s1"].is_set()
    assert not ns._chat_cancels["chat"].is_set()  # a chat turn is untouched


def test_set_turn_included_resolves_the_owning_session():
    b = _Stub()
    b.set_turn_included = types.MethodType(Bridge.set_turn_included, b)
    asyncio.run(b._agentic_chat_turn("c1", "first", _Text("a")))
    other = b.new_chat_session()["id"]
    asyncio.run(b._agentic_chat_turn("c2", "second", _Text("b")))
    first = next(s for s in b._sessions.values() if s.id != other)
    assert b.set_turn_included("c1", False) == {"ok": True, "included": False}
    assert first.excluded == {"c1"} and b._sessions[other].excluded == set()
    assert b.set_turn_included("nope", False) == {"ok": False, "included": True}


def test_automation_session_registry_helper_shapes():
    s = new_automation("trigger", "x", "label")
    assert s.to_dict()["interactive"] is False
