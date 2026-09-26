"""Opt-in extras on the built-in Anthropic adapter: 1h prompt cache, refusal
fallbacks and thinking summaries.

They live in adapters/anthropic.py (not the shared base helpers), are off by
default, and must leave the request byte-identical to before when unset.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

from runspec_console.adapters.anthropic import AnthropicAdapter, _echo_content

CLIENT = "runspec_console.adapters.anthropic.anthropic.AsyncAnthropic"

TOOLS = [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}]


def _adapter(**kw: Any) -> AnthropicAdapter:
    with patch(CLIENT):
        return AnthropicAdapter(api_key="k", **kw)


def _shaped(
    adapter: AnthropicAdapter, messages: list[dict[str, Any]]
) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        model=adapter.model,
        max_tokens=100,
        messages=messages,
        system="sys",
        tools=TOOLS,
    )
    adapter._shape_request(kwargs)
    return kwargs


def _markers(kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [*kwargs["tools"], *kwargs["system"]]
    for msg in kwargs["messages"]:
        if isinstance(msg["content"], list):
            blocks.extend(msg["content"])
    return [b["cache_control"] for b in blocks if "cache_control" in b]


def test_defaults_leave_the_request_unchanged():
    kwargs = _shaped(
        _adapter(model="claude-opus-5"), [{"role": "user", "content": "hi"}]
    )
    assert all("ttl" not in m for m in _markers(kwargs))
    assert "betas" not in kwargs and "extra_body" not in kwargs


def test_cache_ttl_1h_on_every_breakpoint_without_mutating_history():
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
    ]
    kwargs = _shaped(_adapter(cache_ttl="1h"), history)
    markers = _markers(kwargs)
    assert len(markers) == 3  # last tool, system, last message
    assert all(m == {"type": "ephemeral", "ttl": "1h"} for m in markers)
    assert history[-1]["content"][0] == {"type": "text", "text": "next"}


def test_unknown_cache_ttl_is_ignored():
    kwargs = _shaped(_adapter(cache_ttl="2h"), [{"role": "user", "content": "hi"}])
    assert all("ttl" not in m for m in _markers(kwargs))


def test_fallbacks_only_on_models_that_support_them():
    for model in ("claude-opus-5", "claude-opus-5-5", "claude-fable-5-1"):
        kwargs = _shaped(
            _adapter(model=model, fallbacks="default"),
            [{"role": "user", "content": "x"}],
        )
        assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
        assert kwargs["extra_body"] == {"fallbacks": "default"}
    kwargs = _shaped(
        _adapter(model="claude-sonnet-4-6", fallbacks="default"),
        [{"role": "user", "content": "x"}],
    )
    assert "betas" not in kwargs and "extra_body" not in kwargs


def test_fallbacks_use_the_beta_namespace():
    adapter = _adapter(model="claude-opus-5", fallbacks="default")
    adapter.client = NS(messages="stable", beta=NS(messages="beta"))
    assert adapter._messages_api({"betas": ["x"]}) == "beta"
    assert adapter._messages_api({}) == "stable"


def test_thinking_display_only_when_thinking_is_adaptive():
    on = _shaped(
        _adapter(model="claude-opus-5", effort="medium", thinking_display="summarized"),
        [{"role": "user", "content": "x"}],
    )
    assert on["thinking"] == {"type": "adaptive", "display": "summarized"}
    # effort off on Opus 5 sends an explicit disable, which must stay untouched.
    off = _shaped(
        _adapter(model="claude-opus-5", thinking_display="summarized"),
        [{"role": "user", "content": "x"}],
    )
    assert off["thinking"] == {"type": "disabled"}


def _event(type_: str, **kw: Any) -> NS:
    return NS(type=type_, **kw)


class _FakeStream:
    def __init__(self, events: list[NS]) -> None:
        self._events = events

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for ev in self._events:
                yield ev

        return gen()

    async def get_final_message(self) -> NS:
        return NS(content=[])


def _stream(adapter: AnthropicAdapter, events: list[NS]) -> list[tuple[str, Any]]:
    seen: dict[str, Any] = {}

    def stream(**kwargs: Any) -> _FakeStream:
        seen.update(kwargs)
        return _FakeStream(events)

    adapter.client = NS(messages=NS(stream=stream), beta=NS(messages=NS(stream=stream)))

    async def run() -> list[tuple[str, Any]]:
        return [
            e
            async for e in adapter.stream_with_tools(
                [{"role": "user", "content": "x"}], TOOLS
            )
        ]

    return asyncio.run(run())


def test_thinking_deltas_become_thinking_events():
    events = [
        _event(
            "content_block_delta",
            index=0,
            delta=NS(type="thinking_delta", thinking="checking the hosts"),
        ),
        _event(
            "content_block_delta", index=1, delta=NS(type="text_delta", text="done")
        ),
    ]
    out = _stream(_adapter(), events)
    assert ("thinking", "checking the hosts") in out
    assert ("text", "done") in out


def test_tool_calls_before_a_fallback_boundary_do_not_run():
    events = [
        _event(
            "content_block_start",
            index=0,
            content_block=NS(type="tool_use", id="t1", name="a"),
        ),
        _event(
            "content_block_delta",
            index=0,
            delta=NS(type="input_json_delta", partial_json="{}"),
        ),
        _event("content_block_start", index=1, content_block=NS(type="fallback")),
        _event(
            "content_block_start",
            index=2,
            content_block=NS(type="tool_use", id="t2", name="b"),
        ),
        _event(
            "content_block_delta",
            index=2,
            delta=NS(type="input_json_delta", partial_json='{"x": 1}'),
        ),
    ]
    done = _stream(_adapter(model="claude-opus-5", fallbacks="default"), events)[-1][1]
    assert [(c.id, c.input) for c in done.tool_calls] == [("t2", {"x": 1})]


def test_echo_content_drops_the_declined_attempts_non_text_blocks():
    blocks = [
        NS(type="thinking"),
        NS(type="text", text="partial"),
        NS(type="tool_use"),
        NS(type="fallback"),
        NS(type="thinking"),
        NS(type="tool_use"),
    ]
    assert [b.type for b in _echo_content(blocks)] == [
        "text",
        "fallback",
        "thinking",
        "tool_use",
    ]
    plain = [NS(type="thinking"), NS(type="tool_use")]
    assert _echo_content(plain) is plain
