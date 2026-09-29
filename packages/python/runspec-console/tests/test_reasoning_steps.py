"""
test_reasoning_steps.py — reasoning summaries are attached to the step they led
to (option C): a model call that ends in tool calls hands its thought to the
first tool call (live on runspec:tool_start, saved on the tools[] entry); one
that ends in the reply keeps it as the reply's reasoning (live on
runspec:reply_thinking, saved as the turn's ``reply_thinking``). Never replayed
to the model.
"""

from __future__ import annotations

import asyncio

from runspec_console.adapters.base import ToolCall
from tests.test_chat_history import _resp, _StubBridge
from tests.session_stub import current as _S


class _ThinkToolThenThinkReply:
    """Call 1: thinks, then two tool calls (a parallel batch). Call 2: thinks,
    then replies."""

    def __init__(self, *, second_thought: bool = True):
        self.calls = 0
        self.second_thought = second_thought

    async def stream_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            yield ("thinking", "Check which directory ")
            yield ("thinking", "is biggest.")
            yield (
                "done",
                _resp(
                    "tool_use",
                    [
                        ToolCall(id="t1", name="local__du", input={"path": "/"}),
                        ToolCall(id="t2", name="local__df", input={}),
                    ],
                ),
            )
        else:
            if self.second_thought:
                yield ("thinking", "Logs dominate; recommend cleanup.")
            yield ("text", "It's the logs.")
            yield ("done", _resp("end_turn", []))

    def make_tool_turn(self, response, results):
        return [
            {"role": "assistant", "content": "tools"},
            {"role": "user", "content": "results"},
        ]


def _run(adapter):
    b = _StubBridge()
    saved: dict = {}

    def persist(session, user, assistant, tools, usage, model, **extra):
        saved.update(tools=tools, **extra)

    b._persist_conversation_turn = persist
    asyncio.run(b._agentic_chat_turn("c1", "why is disk full?", adapter))
    return b, saved


def test_thought_rides_the_first_tool_call_live_and_saved():
    b, saved = _run(_ThinkToolThenThinkReply())
    starts = [d for ev, d in b.dispatched if ev == "runspec:tool_start"]
    assert starts[0]["thinking"] == "Check which directory is biggest."
    assert isinstance(starts[0]["thinking_ms"], int)
    # A parallel batch shares one thought — shown once, on the first row.
    assert "thinking" not in starts[1]
    assert saved["tools"][0]["thinking"] == "Check which directory is biggest."
    assert "thinking" not in saved["tools"][1]


def test_thought_before_the_reply_is_the_reply_reasoning():
    b, saved = _run(_ThinkToolThenThinkReply())
    replies = [d for ev, d in b.dispatched if ev == "runspec:reply_thinking"]
    assert replies == [
        {
            "id": "c1",
            "text": "Logs dominate; recommend cleanup.",
            "ms": replies[0]["ms"],
        }
    ]
    assert saved["reply_thinking"]["text"] == "Logs dominate; recommend cleanup."


def test_no_reply_thought_means_no_reply_reasoning():
    b, saved = _run(_ThinkToolThenThinkReply(second_thought=False))
    assert not [d for ev, d in b.dispatched if ev == "runspec:reply_thinking"]
    assert "reply_thinking" not in saved


def test_thoughts_never_reach_the_model_history():
    b, _ = _run(_ThinkToolThenThinkReply())
    for msg in _S(b).history:
        assert "Check which directory" not in str(msg.get("content"))
        assert "Logs dominate" not in str(msg.get("content"))
