"""
Conformance test for the example plugin adapter.

Drives MyCorpAdapter against a fake HTTP client (no live gateway, no network)
and checks it satisfies the runspec-console adapter contract. Copy this pattern
into your real plugin and adjust the canned response to match your gateway.

Run with: pytest
"""

from __future__ import annotations

import asyncio
from typing import Any

from console_adapter_plugin.adapter import MyCorpAdapter

from runspec_console.adapters.testing import (
    assert_adapter_contract,
    assert_chat_response,
    assert_tool_turn,
)

TOOLS = [
    {
        "name": "host__restart",
        "description": "Restart a service",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    }
]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Stand-in for httpx.AsyncClient — returns a canned /invoke body."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]):  # noqa: A002
        return _FakeResponse(self.body)


def _adapter(body: dict[str, Any]) -> MyCorpAdapter:
    return MyCorpAdapter(
        base_url="https://gateway.corp/my-chain",
        api_key="test-token",
        client=_FakeClient(body),
    )


def test_adapter_is_well_formed():
    assert_adapter_contract(_adapter({"output": "hi"}))


def test_chat_returns_a_tool_call():
    body = {
        "output": {
            "content": "restarting",
            "tool_calls": [
                {"name": "host__restart", "args": {"name": "nginx"}, "id": "tc_1"}
            ],
        }
    }
    adapter = _adapter(body)
    resp = asyncio.run(
        adapter.chat([{"role": "user", "content": "restart nginx"}], TOOLS)
    )
    assert_chat_response(resp, expect_tool_calls=True)
    turns = adapter.make_tool_turn(resp, [(resp.tool_calls[0], "ok: restarted")])
    assert_tool_turn(turns)


def test_chat_plain_reply_ends_turn():
    adapter = _adapter({"output": "all done"})
    resp = asyncio.run(adapter.chat([{"role": "user", "content": "hi"}], []))
    assert_chat_response(resp, expect_tool_calls=False)
