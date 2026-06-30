"""
test_context_indicator.py — the context-window indicator's pure backend pieces
(docs/design/console-context-indicator.md).

Covers the bucket-splitting estimator (exact total, estimated slices), the
model→window lookup, the parseable truncation marker, the configurable
per-tool-output cap reader, and the message-size helper. Bridge methods are
bound onto a bare stub so we dodge Bridge.__init__ (mirrors test_console_context).
"""

from __future__ import annotations

import types

from runspec_console import bridge_utils as u
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import _AGENT_TOOL_OUTPUT_CAP


# ── estimate_context_buckets ────────────────────────────────────────────────


def test_buckets_sum_to_exact_total():
    parts = {
        "system": 4000,
        "console_context": 1800,
        "memory": 200,
        "tools": 6000,
        "catalog": 0,
        "history": 26000,
    }
    buckets = u.estimate_context_buckets(parts, 48200)
    assert sum(b["tokens"] for b in buckets) == 48200
    # Order is preserved so the UI renders a stable column.
    assert [b["key"] for b in buckets] == list(parts)
    assert all(b["est"] for b in buckets)


def test_buckets_proportional():
    # Two equal buckets split a total evenly.
    buckets = u.estimate_context_buckets({"a": 100, "b": 100}, 1000)
    assert {b["key"]: b["tokens"] for b in buckets} == {"a": 500, "b": 500}


def test_buckets_largest_remainder_keeps_total_exact():
    # 3 equal parts into 100 tokens: 33/33/34, never 33/33/33 (lost token).
    buckets = u.estimate_context_buckets({"a": 1, "b": 1, "c": 1}, 100)
    assert sum(b["tokens"] for b in buckets) == 100
    assert sorted(b["tokens"] for b in buckets) == [33, 33, 34]


def test_buckets_zero_total_or_chars():
    assert all(b["tokens"] == 0 for b in u.estimate_context_buckets({"a": 10}, 0))
    assert all(b["tokens"] == 0 for b in u.estimate_context_buckets({"a": 0}, 100))


def test_buckets_negative_chars_clamped():
    buckets = u.estimate_context_buckets({"a": -5, "b": 100}, 200)
    assert {b["key"]: b["tokens"] for b in buckets} == {"a": 0, "b": 200}


# ── model_context_window ────────────────────────────────────────────────────


def test_window_known_and_unknown():
    assert u.model_context_window("claude-opus-4-8") == 200_000
    assert u.model_context_window("CLAUDE-sonnet-4-6") == 200_000  # case-insensitive
    assert u.model_context_window("gpt-4o") == 128_000
    assert u.model_context_window("") == 200_000  # default
    assert u.model_context_window("something-unheard-of") == 200_000


# ── truncation marker roundtrip ─────────────────────────────────────────────


def test_truncation_marker_roundtrip():
    out = "X" * 1_000_000 + u.make_truncation_suffix(3_200_000, 1_000_000)
    assert u.parse_truncation(out) == {"orig_len": 3_200_000, "cap": 1_000_000}


def test_truncation_absent_returns_none():
    assert u.parse_truncation("a normal tool result with no marker") is None
    # An [exit N] prefix doesn't confuse the parser.
    assert u.parse_truncation("[exit 1]\nclean error text") is None


def test_truncation_marker_survives_exit_prefix():
    body = "Y" * 50 + u.make_truncation_suffix(50, 10)
    assert u.parse_truncation(f"[exit 2]\n{body}") == {"orig_len": 50, "cap": 10}


# ── _tool_output_cap reader ─────────────────────────────────────────────────


def _cap_stub(llm_cfg):
    ns = types.SimpleNamespace()
    ns.get_config = lambda: {"llm": llm_cfg}
    ns._tool_output_cap = types.MethodType(Bridge._tool_output_cap, ns)
    return ns


def test_cap_default_when_unset():
    assert _cap_stub({})._tool_output_cap() == _AGENT_TOOL_OUTPUT_CAP


def test_cap_honours_config():
    assert _cap_stub({"tool_output_cap": 50_000})._tool_output_cap() == 50_000


def test_cap_rejects_nonpositive_and_garbage():
    assert (
        _cap_stub({"tool_output_cap": 0})._tool_output_cap() == _AGENT_TOOL_OUTPUT_CAP
    )
    assert (
        _cap_stub({"tool_output_cap": -5})._tool_output_cap() == _AGENT_TOOL_OUTPUT_CAP
    )
    assert (
        _cap_stub({"tool_output_cap": "nope"})._tool_output_cap()
        == _AGENT_TOOL_OUTPUT_CAP
    )


# ── _messages_chars ─────────────────────────────────────────────────────────


def test_messages_chars_counts_and_survives_unserialisable():
    assert Bridge._messages_chars([{"role": "user", "content": "hi"}]) > 0
    # A non-JSON object falls back to str() rather than raising.
    assert Bridge._messages_chars(object()) > 0
