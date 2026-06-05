"""
test_adapter_testing.py — the conformance helpers in adapters/testing.py.

These ship as a public helper for plugin authors, so they get their own tests:
they must pass a well-formed adapter/response and fail loudly on the common
mistakes they exist to catch.
"""

from __future__ import annotations

from typing import Any

import pytest

from runspec_console.adapters.base import ChatResponse, ModelAdapter, ToolCall
from runspec_console.adapters.testing import (
    assert_adapter_contract,
    assert_chat_response,
    assert_tool_turn,
)


class _GoodAdapter(ModelAdapter):
    async def chat(self, messages, tools):  # type: ignore[override]
        return ChatResponse(text="hi", tool_calls=[], stop_reason="end_turn")

    async def stream_chat(self, messages, tools):  # type: ignore[override]
        yield "hi"

    def make_tool_turn(self, response, results):  # type: ignore[override]
        return [{"role": "assistant", "content": ""}]


class _NotAsyncChat(_GoodAdapter):
    def chat(self, messages, tools):  # type: ignore[override]  # not async!
        return ChatResponse(text="hi", tool_calls=[], stop_reason="end_turn")


# ── assert_adapter_contract ───────────────────────────────────────────────────


def test_contract_passes_for_good_adapter():
    assert_adapter_contract(_GoodAdapter())


def test_contract_rejects_non_adapter():
    with pytest.raises(AssertionError, match="ModelAdapter"):
        assert_adapter_contract(object())


def test_contract_rejects_non_async_chat():
    with pytest.raises(AssertionError, match="async def"):
        assert_adapter_contract(_NotAsyncChat())


# ── assert_chat_response ──────────────────────────────────────────────────────


def test_chat_response_ok_no_tools():
    assert_chat_response(
        ChatResponse(text="x", tool_calls=[], stop_reason="end_turn"),
        expect_tool_calls=False,
    )


def test_chat_response_ok_with_tools():
    tc = ToolCall(id="1", name="t", input={})
    assert_chat_response(
        ChatResponse(text=None, tool_calls=[tc], stop_reason="tool_use"),
        expect_tool_calls=True,
    )


def test_chat_response_rejects_tool_calls_without_tool_use_stop():
    tc = ToolCall(id="1", name="t", input={})
    with pytest.raises(AssertionError, match="tool_use"):
        assert_chat_response(
            ChatResponse(text=None, tool_calls=[tc], stop_reason="end_turn")
        )


def test_chat_response_rejects_bad_stop_reason():
    with pytest.raises(AssertionError, match="stop_reason"):
        assert_chat_response(ChatResponse(text="x", tool_calls=[], stop_reason="weird"))


def test_chat_response_rejects_wrong_type():
    with pytest.raises(AssertionError, match="ChatResponse"):
        assert_chat_response({"text": "x"})


def test_chat_response_expect_tool_calls_mismatch():
    with pytest.raises(AssertionError, match="expected at least one"):
        assert_chat_response(
            ChatResponse(text="x", tool_calls=[], stop_reason="end_turn"),
            expect_tool_calls=True,
        )


def test_chat_response_rejects_non_toolcall_items():
    bad: Any = ChatResponse(text=None, tool_calls=[{"id": "1"}], stop_reason="tool_use")
    with pytest.raises(AssertionError, match="ToolCall"):
        assert_chat_response(bad)


# ── assert_tool_turn ──────────────────────────────────────────────────────────


def test_tool_turn_ok():
    assert_tool_turn([{"role": "assistant", "content": ""}, {"role": "tool"}])


def test_tool_turn_rejects_empty():
    with pytest.raises(AssertionError, match="non-empty list"):
        assert_tool_turn([])


def test_tool_turn_rejects_missing_role():
    with pytest.raises(AssertionError, match="role"):
        assert_tool_turn([{"content": "x"}])
