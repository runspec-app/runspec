"""
testing.py — conformance helpers for custom LLM provider adapters.

Drop these into a plugin's own test suite to mechanically verify it satisfies
the runspec-console adapter contract *before* wiring it into the console — they
catch the mistakes that are easy to make (wrong return type, a missing
``stop_reason="tool_use"`` when tool calls are present, a malformed
``make_tool_turn``). Plain asserts, no pytest dependency, usable from any runner:

    import asyncio
    from runspec_console.adapters.testing import (
        assert_adapter_contract, assert_chat_response, assert_tool_turn,
    )

    adapter = MyCorpAdapter(base_url="...", client=FakeClient(canned_response))
    assert_adapter_contract(adapter)                       # structural

    resp = asyncio.run(adapter.chat([{"role": "user", "content": "hi"}], TOOLS))
    assert_chat_response(resp, expect_tool_calls=True)     # a real response
    assert_tool_turn(adapter.make_tool_turn(resp, [(resp.tool_calls[0], "ok")]))

``assert_adapter_contract`` is network-free (it only inspects signatures). The
other two validate values your adapter produced, so call them after driving the
adapter against a fake transport.
"""

from __future__ import annotations

import inspect
from typing import Any

from .base import ChatResponse, ModelAdapter, ToolCall

# Stop reasons the agent loop understands. Only "tool_use" triggers tool
# execution; anything else ends the turn.
VALID_STOP_REASONS = {"tool_use", "end_turn", "stop"}


def assert_adapter_contract(adapter: Any) -> None:
    """Structural checks — the adapter is wired correctly (no network needed)."""
    assert isinstance(adapter, ModelAdapter), (
        f"adapter must subclass runspec_console.ModelAdapter, got {type(adapter)!r}"
    )
    assert inspect.iscoroutinefunction(adapter.chat), "chat() must be `async def`"
    assert inspect.isasyncgenfunction(adapter.stream_chat), (
        "stream_chat() must be an async generator (`async def` with `yield`)"
    )
    assert inspect.isasyncgenfunction(adapter.stream_with_tools), (
        "stream_with_tools() must be an async generator"
    )
    assert callable(adapter.make_tool_turn), "make_tool_turn() must be callable"


def assert_chat_response(
    response: Any, *, expect_tool_calls: bool | None = None
) -> None:
    """Validate a ChatResponse your adapter returned from ``chat()``."""
    assert isinstance(response, ChatResponse), (
        f"chat() must return a ChatResponse, got {type(response)!r}"
    )
    assert response.text is None or isinstance(response.text, str), (
        "ChatResponse.text must be a str or None"
    )
    assert isinstance(response.tool_calls, list), (
        "ChatResponse.tool_calls must be a list"
    )
    for tc in response.tool_calls:
        assert isinstance(tc, ToolCall), (
            f"each tool call must be a ToolCall, got {type(tc)!r}"
        )
        assert isinstance(tc.id, str) and tc.id, "ToolCall.id must be a non-empty str"
        assert isinstance(tc.name, str) and tc.name, (
            "ToolCall.name must be a non-empty str"
        )
        assert isinstance(tc.input, dict), "ToolCall.input must be a dict"
    assert isinstance(response.stop_reason, str), (
        "ChatResponse.stop_reason must be a str"
    )
    assert response.stop_reason in VALID_STOP_REASONS, (
        f"stop_reason {response.stop_reason!r} not in {sorted(VALID_STOP_REASONS)}"
    )
    # The critical agent-loop invariant: tools run iff stop_reason == "tool_use".
    # Return tool calls without it and the console ignores them; set it without
    # tool calls and the loop spins.
    if response.tool_calls:
        assert response.stop_reason == "tool_use", (
            "stop_reason must be 'tool_use' when tool_calls are present"
        )
    if expect_tool_calls is True:
        assert response.tool_calls, "expected at least one tool call, got none"
    if expect_tool_calls is False:
        assert not response.tool_calls, (
            "expected no tool calls, but the adapter returned some"
        )


def assert_tool_turn(turns: Any) -> None:
    """Validate the turns ``make_tool_turn()`` appends back to the conversation."""
    assert isinstance(turns, list) and turns, (
        "make_tool_turn() must return a non-empty list of message turns"
    )
    for turn in turns:
        assert isinstance(turn, dict), "each tool turn must be a dict"
        assert turn.get("role"), "each tool turn must have a non-empty 'role'"
