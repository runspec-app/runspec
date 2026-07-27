"""
test_agent_continuation.py — a long agent reply must not be silently truncated.

Every model call has an output-token ceiling (``[llm] max_tokens``). When the
model stops because it hit that ceiling mid-reply, the SDK returns
``stop_reason == "max_tokens"``. The old loop treated any non-``tool_use`` stop
as the end of the turn, so it persisted the partial reply and stopped — no
marker, no continuation. These tests drive the real ``_agentic_chat_turn`` with
a scripted fake adapter and assert it auto-continues, and that once the
continuation budget is spent it ends with a visible cut-off marker.

The bridge methods are bound onto a bare stub (types.SimpleNamespace +
MethodType) to dodge Bridge.__init__ side effects, mirroring test_chat_bridge.py.
"""

from __future__ import annotations

import asyncio
import threading
import types

from runspec_console.adapters.base import ChatResponse, ToolCall
from runspec_console.bridge import Bridge


class _ScriptedAdapter:
    """Yields one text chunk then a done-response per iteration, walking a
    scripted list of ``(text, stop_reason)`` — or ``(text, stop_reason,
    [ToolCall, …])`` — steps (clamped at the last entry, so an 'always
    max_tokens' script just repeats)."""

    model = "scripted"

    def __init__(self, script: list[tuple]) -> None:
        self._script = script
        self.calls = 0

    async def stream_with_tools(self, messages, tools):
        step = self._script[min(self.calls, len(self._script) - 1)]
        text, stop = step[0], step[1]
        tool_calls = list(step[2]) if len(step) > 2 else []
        self.calls += 1
        if text:
            yield ("text", text)
        yield (
            "done",
            ChatResponse(text=None, tool_calls=tool_calls, stop_reason=stop, _raw=None),
        )

    def make_tool_turn(self, response, results):
        return [
            {"role": "assistant", "content": response.text or ""},
            {
                "role": "user",
                "content": [
                    {"tool_use_id": tc.id, "content": out} for tc, out in results
                ],
            },
        ]


def _chat_stub(max_cont: int = 3):
    s = types.SimpleNamespace()
    s._chat_lock = threading.Lock()
    s._chat_history = []
    s._history_turn_ids = []
    s._excluded_turns = set()
    s._chat_cancels = {}
    s._config = {"llm": {"max_continuations": max_cont}}
    s.tokens = []  # every runspec:token string, in order
    s.persisted = {}

    s.get_config = lambda: s._config
    s._runnables_to_tools = lambda *a, **k: []
    s._tool_mode = lambda: "dedup"
    s._chat_index_text = lambda: ""
    s._fold_catalog_index = lambda history, index_text: list(history)
    s._messages_chars = lambda _msgs: 0
    s._trace = lambda *a, **k: None
    s._trim_chat_history = lambda: None
    s._play_sound = lambda *a, **k: None
    s._window_is_foreground = lambda: True
    s._usage_from_response = lambda _r: {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
    }

    def dispatch(event, payload):
        if event == "runspec:token":
            s.tokens.append(payload["token"])

    s._dispatch = dispatch

    def persist(message, assistant, tools, usage, model):
        s.persisted = {"message": message, "assistant": assistant}

    s._persist_conversation_turn = persist

    s.tool_runs = []

    async def gated_run(tool_name, tool_input, chat_id, stage=None, on_decision=None):
        s.tool_runs.append((tool_name, tool_input))
        return "ok: ran"

    s._gated_run_tool_async = gated_run

    s._sync_history_tags = types.MethodType(Bridge._sync_history_tags, s)
    s._visible_history = types.MethodType(Bridge._visible_history, s)
    s._max_continuations = types.MethodType(Bridge._max_continuations, s)
    s._agentic_chat_turn = types.MethodType(Bridge._agentic_chat_turn, s)
    return s


CUT = "_(response was cut off at the output limit)_"
CONTINUE = "Continue exactly where you left off"


def test_max_tokens_reply_auto_continues():
    """A reply cut at the ceiling is picked up and streamed to completion — the
    two halves reach the same bubble and there is no cut-off marker."""
    s = _chat_stub(max_cont=3)
    adapter = _ScriptedAdapter([("part one", "max_tokens"), ("part two", "end_turn")])

    asyncio.run(s._agentic_chat_turn("chat1", "write a long thing", adapter))

    # The model was re-prompted once to continue…
    continue_turns = [
        m
        for m in s._chat_history
        if m["role"] == "user" and CONTINUE in str(m["content"])
    ]
    assert len(continue_turns) == 1
    # …both halves streamed into the same chat bubble, in order, no marker.
    assert "".join(s.tokens) == "part onepart two"
    assert not any(CUT in t for t in s.tokens)
    # The saved transcript keeps both halves.
    assert "part one" in s.persisted["assistant"]
    assert "part two" in s.persisted["assistant"]


def test_continuation_budget_exhausted_shows_marker():
    """When the model keeps hitting the ceiling, the turn continues up to the
    budget and then ends with a visible cut-off marker rather than silently."""
    s = _chat_stub(max_cont=2)
    adapter = _ScriptedAdapter([("chunk", "max_tokens")])  # always cut off

    asyncio.run(s._agentic_chat_turn("chat1", "write forever", adapter))

    # Exactly max_continuations re-prompts, then it gives up.
    continue_turns = [
        m
        for m in s._chat_history
        if m["role"] == "user" and CONTINUE in str(m["content"])
    ]
    assert len(continue_turns) == 2
    # The last thing the user sees is the visible cut-off marker.
    assert any(CUT in t for t in s.tokens)
    # It stopped — didn't spin to the hard iteration cap (2 continues + 1 final).
    assert adapter.calls == 3


def test_natural_end_does_not_continue_or_mark():
    """A reply that ends naturally is untouched — no continue prompt, no marker."""
    s = _chat_stub()
    adapter = _ScriptedAdapter([("all done", "end_turn")])

    asyncio.run(s._agentic_chat_turn("chat1", "hi", adapter))

    assert adapter.calls == 1
    assert "".join(s.tokens) == "all done"
    assert not any(CONTINUE in str(m["content"]) for m in s._chat_history)
    assert not any(CUT in t for t in s.tokens)


def test_custom_adapter_length_spelling_auto_continues():
    """A custom adapter that reports an OpenAI-style ``length`` cutoff (not the
    literal ``max_tokens``) still triggers auto-continuation — the loop keys off
    is_output_truncation, not one hardcoded string."""
    s = _chat_stub(max_cont=3)
    adapter = _ScriptedAdapter([("part one", "length"), ("part two", "stop")])

    asyncio.run(s._agentic_chat_turn("chat1", "long", adapter))

    assert any(
        m["role"] == "user" and CONTINUE in str(m["content"]) for m in s._chat_history
    )
    assert "".join(s.tokens) == "part onepart two"
    assert not any(CUT in t for t in s.tokens)


def test_tool_calls_run_regardless_of_stop_reason_spelling():
    """A custom adapter that returns tool calls with a non-'tool_use' stop reason
    (e.g. the OpenAI 'tool_calls' spelling) still has its tools executed — the
    loop keys off the presence of tool_calls, not one literal stop string."""
    s = _chat_stub()
    tc = ToolCall(id="1", name="host__restart", input={"name": "nginx"})
    adapter = _ScriptedAdapter(
        [("calling a tool", "tool_calls", [tc]), ("done", "end_turn")]
    )

    asyncio.run(s._agentic_chat_turn("chat1", "restart nginx", adapter))

    # The tool ran even though stop_reason was "tool_calls", not "tool_use".
    assert s.tool_runs == [("host__restart", {"name": "nginx"})]
    assert adapter.calls == 2  # tool turn, then the final reply
