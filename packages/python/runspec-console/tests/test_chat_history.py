"""
test_chat_history.py — the agent chat must remember prior turns (the model sees
earlier user/assistant/tool messages), clear_chat resets it, and the rolling
history is trimmed safely (never orphaning a tool_result).
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from runspec_console.adapters.base import ToolCall
from runspec_console.bridge import Bridge


def _resp(stop_reason, tool_calls, inp=10, out=5):
    return SimpleNamespace(
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        _raw=SimpleNamespace(
            usage=SimpleNamespace(input_tokens=inp, output_tokens=out)
        ),
    )


class _StubBridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._chat_history: list[dict] = []
        self._chat_lock = threading.Lock()
        self._chat_cancels: dict[str, threading.Event] = {}
        self._pending_confirms: dict = {}
        self.dispatched: list = []

    def _dispatch(self, event, detail):
        self.dispatched.append((event, detail))

    def _runnables_to_tools(self):
        return []

    async def _gated_run_tool_async(self, name, tool_input, chat_id):
        return f"ran:{name}"

    _usage_from_response = staticmethod(Bridge._usage_from_response)
    _trim_chat_history = Bridge._trim_chat_history
    clear_chat = Bridge.clear_chat
    cancel_chat = Bridge.cancel_chat
    _agentic_chat_turn = Bridge._agentic_chat_turn


class _TextAdapter:
    """Replies with one text turn; records the history it was handed each call."""

    def __init__(self, reply="ok"):
        self.reply = reply
        self.seen: list[list[dict]] = []

    async def stream_with_tools(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        yield ("text", self.reply)
        yield ("done", _resp("end_turn", []))

    def make_tool_turn(self, response, results):  # pragma: no cover - unused here
        return []


# ── conversational memory ──────────────────────────────────────────────


def test_history_persists_across_turns():
    b, ad = _StubBridge(), _TextAdapter("hi there")
    asyncio.run(b._agentic_chat_turn("c1", "first question", ad))
    asyncio.run(b._agentic_chat_turn("c2", "second question", ad))

    # The second turn must have been handed the first turn's user + assistant.
    second_seen = ad.seen[1]
    contents = [(m["role"], m["content"]) for m in second_seen]
    assert ("user", "first question") in contents
    assert ("assistant", "hi there") in contents
    assert ("user", "second question") in contents
    # Persisted: user1, assistant1, user2, assistant2
    assert [m["role"] for m in b._chat_history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_clear_chat_starts_fresh():
    b, ad = _StubBridge(), _TextAdapter()
    asyncio.run(b._agentic_chat_turn("c1", "remember this", ad))
    assert b._chat_history
    b.clear_chat()
    assert b._chat_history == []

    asyncio.run(b._agentic_chat_turn("c2", "new topic", ad))
    # The post-clear turn only sees its own message, not the old one.
    assert ad.seen[-1] == [{"role": "user", "content": "new topic"}]


# ── tool-calling turns are persisted too ───────────────────────────────


class _ToolThenText:
    def __init__(self):
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def stream_with_tools(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        self.calls += 1
        if self.calls == 1:
            tc = ToolCall(id="t1", name="local__svc", input={"a": 1})
            yield ("done", _resp("tool_use", [tc]))
        else:
            yield ("text", "all done")
            yield ("done", _resp("end_turn", []))

    def make_tool_turn(self, response, results):
        return [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": results[0][1],
                    }
                ],
            },
        ]


def test_tool_turn_round_trip_persisted():
    b, ad = _StubBridge(), _ToolThenText()
    asyncio.run(b._agentic_chat_turn("c1", "do it", ad))
    # user, assistant(tool_use), user(tool_result), assistant(text)
    assert [m["role"] for m in b._chat_history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert b._chat_history[-1] == {"role": "assistant", "content": "all done"}
    # The 2nd model call saw the tool_result from the 1st.
    assert ad.calls == 2
    assert any(
        isinstance(m.get("content"), list)
        and m["content"][0].get("type") == "tool_result"
        for m in ad.seen[1]
    )


# ── stop / cancel ───────────────────────────────────────────────────────


class _CancelAfterText:
    """Streams some text, then (after the bridge has dispatched it) the test has
    flipped the cancel event, so the turn must stop before running any tool."""

    def __init__(self, cancel: threading.Event):
        self.cancel = cancel
        self.ran_tool = False

    async def stream_with_tools(self, messages, tools):
        yield ("text", "thinking…")
        self.cancel.set()  # user hits Stop mid-stream
        yield ("text", " more")
        tc = ToolCall(id="t1", name="local__danger", input={})
        yield ("done", _resp("tool_use", [tc]))

    def make_tool_turn(self, response, results):
        self.ran_tool = True  # must NOT be reached when cancelled
        return []


def test_cancel_stops_before_running_tools():
    b = _StubBridge()
    ev = threading.Event()
    b._chat_cancels["c1"] = ev
    ad = _CancelAfterText(ev)
    asyncio.run(b._agentic_chat_turn("c1", "go", ad))

    # No tool turn was built → no host action taken.
    assert ad.ran_tool is False
    # A "Stopped" notice was surfaced to the UI.
    assert any(
        ev_name == "runspec:token" and "Stopped" in detail.get("token", "")
        for ev_name, detail in b.dispatched
    )
    # History holds the user msg + the partial assistant text (no dangling tool_use).
    assert [m["role"] for m in b._chat_history] == ["user", "assistant"]


def test_cancel_before_turn_runs_nothing():
    b = _StubBridge()
    ev = threading.Event()
    ev.set()  # already cancelled when the turn starts
    b._chat_cancels["c1"] = ev
    ad = _TextAdapter("should not appear")
    asyncio.run(b._agentic_chat_turn("c1", "go", ad))
    assert ad.seen == []  # model never called
    assert [m["role"] for m in b._chat_history] == ["user"]


def test_cancel_chat_sets_the_event():
    b = _StubBridge()
    ev = threading.Event()
    b._chat_cancels["c1"] = ev
    b.cancel_chat("c1")
    assert ev.is_set()
    b.cancel_chat("nonexistent")  # must not raise


# ── trimming ────────────────────────────────────────────────────────────


def test_trim_caps_and_keeps_user_start():
    b = _StubBridge()
    b._chat_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(100)
    ]
    b._trim_chat_history(max_messages=10)
    assert len(b._chat_history) <= 10
    first = b._chat_history[0]
    assert first["role"] == "user" and isinstance(first["content"], str)


def test_trim_drops_orphaned_tool_result_prefix():
    b = _StubBridge()
    b._chat_history = [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
        {"role": "assistant", "content": "mid"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "final"},
    ]
    b._trim_chat_history(max_messages=4)
    # After dropping the oldest 2, the slice would start on an orphaned
    # tool_result — trim must skip forward to the next real user message.
    assert b._chat_history[0] == {"role": "user", "content": "q1"}
    assert [m["role"] for m in b._chat_history] == ["user", "assistant"]


def test_trim_noop_under_cap():
    b = _StubBridge()
    b._chat_history = [{"role": "user", "content": "x"}]
    b._trim_chat_history(max_messages=60)
    assert b._chat_history == [{"role": "user", "content": "x"}]
