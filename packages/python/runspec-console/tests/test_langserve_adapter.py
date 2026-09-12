"""
test_langserve_adapter.py — LangServe adapter mapping + request/response flow.

The pure helpers (build_payload / parse_output / _to_openai_tool) are exercised
without httpx; the adapter itself is driven through a fake async client so no
network is touched.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from runspec_console.adapters.langserve import (
    LangServeAdapter,
    _to_openai_tool,
    build_payload,
    cacheable_prefix,
    extract_stop_reason,
    normalize_stop_reason,
    parse_output,
    stream_delta_text,
    stream_final_output,
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


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_tool_conversion_to_openai_function():
    fn = _to_openai_tool(TOOLS[0])
    assert fn["type"] == "function"
    assert fn["function"]["name"] == "host__restart"
    assert fn["function"]["description"] == "Restart a service"
    assert fn["function"]["parameters"] == TOOLS[0]["input_schema"]


def test_build_payload_default_puts_messages_and_tools_in_input():
    messages = [{"role": "user", "content": "hi"}]
    payload = build_payload(messages, TOOLS, "be helpful")
    inp = payload["input"]
    # system prepended as a system-role message
    assert inp["messages"][0] == {"role": "system", "content": "be helpful"}
    assert inp["messages"][1] == {"role": "user", "content": "hi"}
    assert inp["tools"][0]["function"]["name"] == "host__restart"
    assert payload["config"] == {}


def test_build_payload_blank_system_is_omitted():
    payload = build_payload([{"role": "user", "content": "hi"}], [], "   ")
    assert payload["input"]["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in payload["input"]


def test_build_payload_tools_in_config():
    payload = build_payload(
        [{"role": "user", "content": "hi"}],
        TOOLS,
        "",
        tools_in_config=True,
        tools_key="tools",
    )
    assert "tools" not in payload["input"]
    assert payload["config"]["configurable"]["tools"][0]["function"]["name"] == (
        "host__restart"
    )


def test_build_payload_custom_keys():
    payload = build_payload(
        [{"role": "user", "content": "hi"}],
        TOOLS,
        "",
        messages_key="chat_history",
        tools_key="functions",
    )
    assert "chat_history" in payload["input"]
    assert "functions" in payload["input"]


def test_build_payload_max_tokens_default_kwargs_slot():
    payload = build_payload(
        [{"role": "user", "content": "hi"}], [], "", max_tokens=8192
    )
    assert payload["kwargs"]["max_tokens"] == 8192
    assert "max_tokens" not in payload["input"]


def test_build_payload_max_tokens_off_by_default():
    payload = build_payload([{"role": "user", "content": "hi"}], [], "")
    assert payload["kwargs"] == {}  # nothing sent unless max_tokens > 0


def test_build_payload_max_tokens_in_config_and_custom_key():
    payload = build_payload(
        [{"role": "user", "content": "hi"}],
        [],
        "",
        max_tokens=4096,
        max_tokens_in="config",
        max_tokens_key="maxTokens",
    )
    assert payload["config"]["configurable"]["maxTokens"] == 4096


def test_build_payload_max_tokens_config_merges_with_tools():
    payload = build_payload(
        [{"role": "user", "content": "hi"}],
        TOOLS,
        "",
        tools_in_config=True,
        max_tokens=1000,
        max_tokens_in="config",
    )
    conf = payload["config"]["configurable"]
    assert conf["tools"][0]["function"]["name"] == "host__restart"
    assert conf["max_tokens"] == 1000  # both live under configurable, no clobber


def test_build_payload_effort_off_by_default():
    payload = build_payload([{"role": "user", "content": "hi"}], [], "")
    assert payload["kwargs"] == {}  # nothing sent unless effort is set


def test_build_payload_effort_default_openai_field_and_slot():
    payload = build_payload([{"role": "user", "content": "hi"}], [], "", effort="high")
    # OpenAI-style field name in the kwargs bucket by default (pass-through string).
    assert payload["kwargs"]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in payload["input"]


def test_build_payload_effort_custom_key_and_slot():
    payload = build_payload(
        [{"role": "user", "content": "hi"}],
        [],
        "",
        effort="max",
        effort_key="thinking_level",
        effort_in="config",
    )
    assert payload["config"]["configurable"]["thinking_level"] == "max"


def test_build_payload_effort_and_max_tokens_coexist():
    payload = build_payload(
        [{"role": "user", "content": "hi"}],
        [],
        "",
        max_tokens=2048,
        effort="medium",
    )
    assert payload["kwargs"]["max_tokens"] == 2048
    assert payload["kwargs"]["reasoning_effort"] == "medium"


def test_adapter_normalises_off_effort_to_nothing():
    from runspec_console.adapters.langserve import LangServeAdapter

    for off in ("", "off", "OFF", "none"):
        a = LangServeAdapter(
            base_url="https://gw.corp/chain", effort=off, client=object()
        )
        assert a.effort == ""
        assert a._build_payload([{"role": "user", "content": "hi"}], [])["kwargs"] == {}


def test_adapter_threads_effort_into_payload():
    from runspec_console.adapters.langserve import LangServeAdapter

    a = LangServeAdapter(
        base_url="https://gw.corp/chain", effort="High", client=object()
    )
    assert a.effort == "high"  # normalised
    payload = a._build_payload([{"role": "user", "content": "hi"}], [])
    assert payload["kwargs"]["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("max_tokens", "max_tokens"),
        ("length", "max_tokens"),  # OpenAI-shaped truncation
        ("MAX_TOKENS", "max_tokens"),
        ("tool_use", "tool_use"),
        ("tool_calls", "tool_use"),
        ("end_turn", "end_turn"),
        ("stop", "end_turn"),
        (None, "end_turn"),
        ("something_odd", "end_turn"),
    ],
)
def test_normalize_stop_reason(raw, expected):
    assert normalize_stop_reason(raw) == expected


def test_extract_stop_reason_from_metadata_spellings():
    # Anthropic
    assert (
        extract_stop_reason(
            {"content": "x", "response_metadata": {"stop_reason": "max_tokens"}}
        )
        == "max_tokens"
    )
    # Bedrock Converse
    assert (
        extract_stop_reason(
            {"content": "x", "response_metadata": {"stopReason": "max_tokens"}}
        )
        == "max_tokens"
    )
    # OpenAI-shaped
    assert (
        extract_stop_reason(
            {"content": "x", "response_metadata": {"finish_reason": "length"}}
        )
        == "length"
    )
    # Nothing reported
    assert extract_stop_reason({"content": "x"}) is None


def test_parse_output_plain_string():
    text, calls, usage = parse_output("just text")
    assert text == "just text"
    assert calls == []
    assert usage == {}


def test_parse_output_aimessage_dict_with_tool_calls():
    output = {
        "content": "restarting it",
        "type": "ai",
        "tool_calls": [
            {"name": "host__restart", "args": {"name": "nginx"}, "id": "tc_1"}
        ],
        "usage_metadata": {"input_tokens": 12, "output_tokens": 7},
    }
    text, calls, usage = parse_output(output)
    assert text == "restarting it"
    assert len(calls) == 1
    assert calls[0].id == "tc_1"
    assert calls[0].name == "host__restart"
    assert calls[0].input == {"name": "nginx"}
    assert usage == {"input_tokens": 12, "output_tokens": 7}


def test_parse_output_constructor_envelope():
    """LangServe's dumpd form nests the real fields under kwargs."""
    output = {
        "lc": 1,
        "type": "constructor",
        "id": ["langchain_core", "messages", "ai", "AIMessage"],
        "kwargs": {
            "content": "done",
            "tool_calls": [{"name": "t", "args": {}, "id": "x"}],
        },
    }
    text, calls, usage = parse_output(output)
    assert text == "done"
    assert calls[0].id == "x"


def test_parse_output_block_list_content():
    output = {
        "content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
            {"type": "tool_use", "name": "t"},
        ],
        "tool_calls": [],
    }
    text, _calls, _usage = parse_output(output)
    assert text == "hello world"


def test_parse_output_tool_call_without_id_gets_synthetic_id():
    output = {"content": "", "tool_calls": [{"name": "t", "args": {}}]}
    _text, calls, _usage = parse_output(output)
    assert calls[0].id == "call_0"


def test_parse_output_cache_read_tokens():
    output = {
        "content": "x",
        "usage_metadata": {
            "input_tokens": 100,
            "output_tokens": 5,
            "input_token_details": {"cache_read": 80},
        },
    }
    _text, _calls, usage = parse_output(output)
    assert usage["cache_read_input_tokens"] == 80


def test_parse_output_cache_creation_tokens():
    output = {
        "content": "x",
        "usage_metadata": {
            "input_tokens": 100,
            "output_tokens": 5,
            "input_token_details": {"cache_read": 10, "cache_creation": 90},
        },
    }
    _text, _calls, usage = parse_output(output)
    assert usage["cache_read_input_tokens"] == 10
    assert usage["cache_creation_input_tokens"] == 90


# ── cache markers (opt-in) ─────────────────────────────────────────────────────


def test_cache_markers_off_by_default_no_markers():
    payload = build_payload([{"role": "user", "content": "hi"}], TOOLS, "sys")
    # system stays a plain string; tools carry no cache_control
    assert payload["input"]["messages"][0] == {"role": "system", "content": "sys"}
    assert "cache_control" not in payload["input"]["tools"][0]


def test_cache_markers_mark_system_last_tool_and_last_message():
    tools = [
        {"name": "b_tool", "input_schema": {"type": "object"}},
        {"name": "a_tool", "input_schema": {"type": "object"}},
    ]
    payload = build_payload(
        [{"role": "user", "content": "please help"}],
        tools,
        "be helpful",
        cache_markers=True,
    )
    msgs = payload["input"]["messages"]
    # system became a content-block message with the marker
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # tools are sorted by name and only the last carries the marker
    out_tools = payload["input"]["tools"]
    names = [t["function"]["name"] for t in out_tools]
    assert names == ["a_tool", "b_tool"]
    assert "cache_control" not in out_tools[0]
    assert out_tools[-1]["cache_control"] == {"type": "ephemeral"}
    # last message's last block carries the per-turn marker
    assert msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_cache_markers_byte_identical_prefix_across_turns():
    """Two turns with the same tools+system but different conversations must
    produce a byte-identical cacheable prefix (system + tools) — the property
    prompt caching relies on."""
    tools = [
        {"name": "z_tool", "input_schema": {"type": "object"}},
        {"name": "a_tool", "input_schema": {"type": "object"}},
    ]
    p1 = build_payload(
        [{"role": "user", "content": "first short one"}],
        tools,
        "the system prompt",
        cache_markers=True,
    )
    p2 = build_payload(
        [
            {"role": "user", "content": "first short one"},
            {"role": "assistant", "content": "ok"},
            {
                "role": "user",
                "content": "a completely different, much longer follow-up",
            },
        ],
        list(reversed(tools)),  # discovery reordered them
        "the system prompt",
        cache_markers=True,
    )
    prefix1 = cacheable_prefix(p1)
    prefix2 = cacheable_prefix(p2)
    assert prefix1 == prefix2
    # the prefix ends at the last marker (the last tool's cache_control)
    assert '"cache_control"' in prefix1


def test_cache_markers_do_not_mutate_caller_tools():
    tools = [{"name": "t", "input_schema": {"type": "object"}}]
    build_payload([{"role": "user", "content": "x"}], tools, "s", cache_markers=True)
    assert "cache_control" not in tools[0]


# ── stream-event interpretation helpers (pure) ─────────────────────────────────


def test_stream_delta_text_from_chat_model_stream():
    ev = {"event": "on_chat_model_stream", "data": {"chunk": {"content": "tok"}}}
    assert stream_delta_text(ev) == "tok"


def test_stream_delta_text_ignores_other_events():
    assert stream_delta_text({"event": "on_chain_start", "data": {}}) == ""
    assert stream_delta_text({"event": "on_chat_model_end", "data": {}}) == ""


def test_stream_delta_text_constructor_envelope():
    ev = {
        "event": "on_chat_model_stream",
        "data": {
            "chunk": {
                "type": "constructor",
                "kwargs": {"content": "hi"},
            }
        },
    }
    assert stream_delta_text(ev) == "hi"


def test_stream_final_output_from_end_event():
    out = {"content": "done", "tool_calls": []}
    ev = {"event": "on_chat_model_end", "data": {"output": out}}
    assert stream_final_output(ev) == out
    assert stream_final_output({"event": "on_chat_model_stream", "data": {}}) is None


# ── adapter via a fake client ─────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Records the last request and returns a canned /invoke body."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]):  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self.body)


def _adapter(body: dict[str, Any], **kw: Any) -> tuple[LangServeAdapter, _FakeClient]:
    client = _FakeClient(body)
    adapter = LangServeAdapter(
        base_url="https://gw.corp/chain",
        api_key="static-token",
        client=client,
        **kw,
    )
    return adapter, client


def test_endpoint_appends_invoke():
    adapter, _ = _adapter({"output": "hi"})
    assert adapter.endpoint == "https://gw.corp/chain/invoke"


def test_endpoint_not_double_appended():
    adapter, _ = _adapter({"output": "hi"})
    assert adapter._endpoint("https://gw.corp/chain/invoke/") == (
        "https://gw.corp/chain/invoke"
    )


def test_missing_base_url_raises():
    with pytest.raises(ValueError, match="base_url"):
        LangServeAdapter(client=_FakeClient({}))


def test_chat_sends_bearer_and_parses_response():
    body = {
        "output": {
            "content": "all set",
            "tool_calls": [{"name": "host__restart", "args": {"name": "x"}, "id": "1"}],
        }
    }
    adapter, client = _adapter(body)
    resp = asyncio.run(adapter.chat([{"role": "user", "content": "restart x"}], TOOLS))
    # request shape
    sent = client.calls[0]
    assert sent["url"] == "https://gw.corp/chain/invoke"
    assert sent["headers"]["Authorization"] == "Bearer static-token"
    assert sent["json"]["input"]["tools"][0]["function"]["name"] == "host__restart"
    # response shape
    assert resp.text == "all set"
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].input == {"name": "x"}


def test_chat_end_turn_when_no_tool_calls():
    adapter, _ = _adapter({"output": "just chatting"})
    resp = asyncio.run(adapter.chat([{"role": "user", "content": "hi"}], []))
    assert resp.stop_reason == "end_turn"
    assert resp.tool_calls == []


def test_chat_forwards_max_tokens_to_gateway():
    adapter, client = _adapter({"output": "hi"}, max_tokens=8192)
    asyncio.run(adapter.chat([{"role": "user", "content": "hi"}], []))
    assert client.calls[0]["json"]["kwargs"]["max_tokens"] == 8192


def test_chat_surfaces_max_tokens_stop_reason():
    # A gateway that truncated the reply at the output ceiling reports it in the
    # message metadata; the adapter surfaces "max_tokens" so the agentic loop
    # auto-continues instead of ending silently.
    body = {
        "output": {
            "content": "a very long half-written answer",
            "response_metadata": {"stopReason": "max_tokens"},
        }
    }
    adapter, _ = _adapter(body)
    resp = asyncio.run(adapter.chat([{"role": "user", "content": "write a lot"}], []))
    assert resp.stop_reason == "max_tokens"
    assert resp.tool_calls == []


def test_chat_tool_calls_still_win_over_stop_metadata():
    body = {
        "output": {
            "content": "",
            "tool_calls": [{"name": "host__restart", "args": {}, "id": "1"}],
            "response_metadata": {"stopReason": "max_tokens"},
        }
    }
    adapter, _ = _adapter(body)
    resp = asyncio.run(adapter.chat([{"role": "user", "content": "go"}], TOOLS))
    assert resp.stop_reason == "tool_use"  # runs the tool next, doesn't continue-loop


def test_custom_auth_scheme_and_header():
    adapter, client = _adapter({"output": "x"}, auth_header="X-Api-Key", auth_scheme="")
    asyncio.run(adapter.chat([{"role": "user", "content": "hi"}], []))
    headers = client.calls[0]["headers"]
    assert headers["X-Api-Key"] == "static-token"
    assert "Authorization" not in headers


def test_make_tool_turn_round_trips_openai_shape():
    adapter, _ = _adapter({"output": "x"})
    from runspec_console.adapters.langserve import ChatResponse, ToolCall

    tc = ToolCall(id="abc", name="host__restart", input={"name": "nginx"})
    resp = ChatResponse(text="restarting", tool_calls=[tc], stop_reason="tool_use")
    turns = adapter.make_tool_turn(resp, [(tc, "ok: restarted")])
    assert turns[0]["role"] == "assistant"
    assert turns[0]["tool_calls"][0]["id"] == "abc"
    # arguments are JSON (re-parseable by LangChain), not a Python repr
    assert json.loads(turns[0]["tool_calls"][0]["function"]["arguments"]) == {
        "name": "nginx"
    }
    assert turns[1] == {
        "role": "tool",
        "tool_call_id": "abc",
        "content": "ok: restarted",
    }


def test_rotating_token_via_command(monkeypatch):
    import subprocess

    calls = {"n": 0}

    def fake_run(*_a: Any, **_k: Any):
        calls["n"] += 1
        return subprocess.CompletedProcess(
            args="", returncode=0, stdout="rot-token\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = _FakeClient({"output": "x"})
    adapter = LangServeAdapter(
        base_url="https://gw.corp/chain",
        api_key_command="vend-token",
        api_key_ttl_ms=0,
        client=client,
    )
    asyncio.run(adapter.chat([{"role": "user", "content": "hi"}], []))
    assert client.calls[0]["headers"]["Authorization"] == "Bearer rot-token"
    assert calls["n"] == 1


# ── streaming over /stream_events ───────────────────────────────────────────────


def _sse(events: list[str]) -> list[str]:
    """Turn a list of astream_events JSON strings into SSE lines (event: data /
    data: <json> / blank), ending with an ``event: end`` marker."""
    lines: list[str] = []
    for ev in events:
        lines.append("event: data")
        lines.append(f"data: {ev}")
        lines.append("")
    lines.append("event: end")
    lines.append("")
    return lines


class _FakeStream:
    def __init__(self, lines: list[str], status: int = 200) -> None:
        self._lines = lines
        self.status_code = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        pass

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamClient:
    """Serves /stream_events SSE lines (and records the request), plus a canned
    /invoke body for the fallback path."""

    def __init__(self, lines: list[str], status: int = 200, invoke_body=None) -> None:
        self._lines = lines
        self._status = status
        self._invoke_body = invoke_body or {"output": "fallback reply"}
        self.stream_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    def stream(
        self, method: str, url: str, json: dict[str, Any], headers: dict[str, str]
    ):  # noqa: A002
        self.stream_calls.append(
            {"method": method, "url": url, "json": json, "headers": headers}
        )
        return _FakeStream(self._lines, status=self._status)

    async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]):  # noqa: A002
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._invoke_body)


def _drain(adapter: LangServeAdapter, messages, tools):
    async def go():
        texts: list[str] = []
        done = None
        async for kind, payload in adapter.stream_with_tools(messages, tools):
            if kind == "text":
                texts.append(payload)
            elif kind == "done":
                done = payload
        return texts, done

    return asyncio.run(go())


def test_events_endpoint_derived_from_base():
    a = LangServeAdapter(base_url="https://gw.corp/chain", client=object())
    assert a.events_endpoint == "https://gw.corp/chain/stream_events"
    a2 = LangServeAdapter(base_url="https://gw.corp/chain/invoke", client=object())
    assert a2.events_endpoint == "https://gw.corp/chain/stream_events"


def test_stream_events_yields_deltas_and_final_response():
    events = [
        json.dumps(
            {"event": "on_chat_model_stream", "data": {"chunk": {"content": "Hello "}}}
        ),
        json.dumps(
            {"event": "on_chat_model_stream", "data": {"chunk": {"content": "world"}}}
        ),
        json.dumps(
            {
                "event": "on_chat_model_end",
                "data": {
                    "output": {
                        "content": "Hello world",
                        "response_metadata": {"stopReason": "max_tokens"},
                        "usage_metadata": {"input_tokens": 11, "output_tokens": 2},
                    }
                },
            }
        ),
    ]
    client = _FakeStreamClient(_sse(events))
    adapter = LangServeAdapter(
        base_url="https://gw.corp/chain", api_key="k", client=client
    )
    texts, done = _drain(adapter, [{"role": "user", "content": "hi"}], [])
    assert texts == ["Hello ", "world"]
    assert done.stop_reason == "max_tokens"  # real stop reason surfaced
    assert done.text == "Hello world"
    # hit /stream_events with SSE Accept, not /invoke
    assert client.stream_calls[0]["url"] == "https://gw.corp/chain/stream_events"
    assert client.stream_calls[0]["headers"]["Accept"] == "text/event-stream"
    assert client.post_calls == []


def test_stream_events_tool_call_from_end_event():
    events = [
        json.dumps(
            {
                "event": "on_chat_model_end",
                "data": {
                    "output": {
                        "content": "",
                        "tool_calls": [
                            {"name": "host__restart", "args": {"name": "x"}, "id": "1"}
                        ],
                    }
                },
            }
        ),
    ]
    client = _FakeStreamClient(_sse(events))
    adapter = LangServeAdapter(base_url="https://gw.corp/chain", client=client)
    _texts, done = _drain(adapter, [{"role": "user", "content": "go"}], TOOLS)
    assert done.stop_reason == "tool_use"
    assert done.tool_calls[0].input == {"name": "x"}


def test_stream_events_unsupported_falls_back_to_invoke():
    client = _FakeStreamClient([], status=404, invoke_body={"output": "via invoke"})
    adapter = LangServeAdapter(base_url="https://gw.corp/chain", client=client)
    texts, done = _drain(adapter, [{"role": "user", "content": "hi"}], [])
    assert texts == ["via invoke"]
    assert done.text == "via invoke"
    # streaming disabled for the session after the 404
    assert adapter.stream_events is False
    assert len(client.post_calls) == 1


def test_stream_events_disabled_uses_invoke_directly():
    client = _FakeStreamClient([], invoke_body={"output": "plain"})
    adapter = LangServeAdapter(
        base_url="https://gw.corp/chain", client=client, stream_events=False
    )
    texts, done = _drain(adapter, [{"role": "user", "content": "hi"}], [])
    assert texts == ["plain"]
    assert client.stream_calls == []  # never tried the SSE route
    assert len(client.post_calls) == 1
