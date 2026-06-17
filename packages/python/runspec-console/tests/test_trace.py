"""
Tests for the agent diagnostic trace (trace.py): path resolution, JSON-safe
rendering of provider message content, and the best-effort line writer.
"""

from __future__ import annotations

import json

from runspec_console.trace import (
    jsonsafe_messages,
    trace_path,
    write_trace,
)


# ── path resolution ───────────────────────────────────────────────────────────


def test_trace_off_by_default(monkeypatch):
    monkeypatch.delenv("RUNSPEC_AGENT_TRACE", raising=False)
    assert trace_path(debug=False) is None


def test_trace_on_with_debug(monkeypatch):
    monkeypatch.delenv("RUNSPEC_AGENT_TRACE", raising=False)
    p = trace_path(debug=True)
    assert p and p.endswith("runspec_agent_trace.jsonl")


def test_explicit_env_path_wins(monkeypatch):
    monkeypatch.setenv("RUNSPEC_AGENT_TRACE", "/tmp/custom.jsonl")
    assert trace_path(debug=False) == "/tmp/custom.jsonl"


# ── JSON-safe message rendering ───────────────────────────────────────────────


class _Block:
    """An SDK content-block object (not JSON-serialisable) — must be stringified."""

    def __init__(self, text):
        self.text = text

    def __str__(self):
        return f"<block {self.text}>"


def test_string_content_passes_through():
    msgs = [{"role": "user", "content": "hello"}]
    assert jsonsafe_messages(msgs) == [{"role": "user", "content": "hello"}]


def test_tool_result_content_preserved_verbatim():
    # The Anthropic tool_result turn: a list of plain dicts. The output string is
    # exactly what the model sees — it must survive untouched.
    payload = '{"hostname": "vm", "kernel": "#1 SMP @0"}'
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": payload}
            ],
        }
    ]
    out = jsonsafe_messages(msgs)
    assert out[0]["content"][0]["content"] == payload
    # And the whole thing is now JSON-serialisable.
    json.dumps(out)


def test_sdk_objects_are_stringified():
    msgs = [{"role": "assistant", "content": [_Block("a"), _Block("b")]}]
    out = jsonsafe_messages(msgs)
    assert out[0]["content"] == ["<block a>", "<block b>"]
    json.dumps(out)  # no raise


def test_openai_tool_turn_fields_kept():
    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "result-text"}]
    out = jsonsafe_messages(msgs)
    assert out[0]["tool_call_id"] == "c1"
    assert out[0]["content"] == "result-text"


# ── writer ────────────────────────────────────────────────────────────────────


def test_write_trace_appends_jsonl(tmp_path):
    p = tmp_path / "trace.jsonl"
    write_trace(str(p), "tool_result", {"tool": "h__system-info", "output": "{}"})
    write_trace(str(p), "model_request", {"messages": []})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["kind"] == "tool_result" and rec["tool"] == "h__system-info"
    assert "ts" in rec


def test_write_trace_swallows_bad_path():
    # A bad path must not raise into the agent loop.
    write_trace("/nonexistent-dir/x/y/z.jsonl", "x", {"a": 1})
