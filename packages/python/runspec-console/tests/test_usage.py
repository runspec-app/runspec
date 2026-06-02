"""
test_usage.py — _usage_from_response extracts uncached input, output, and the
cache read/write counts that prove prompt caching is working.
"""

from __future__ import annotations

from types import SimpleNamespace

from runspec_console.bridge import Bridge


def _resp(**usage):
    return SimpleNamespace(_raw=SimpleNamespace(usage=SimpleNamespace(**usage)))


def test_anthropic_usage_with_cache():
    out = Bridge._usage_from_response(
        _resp(
            input_tokens=1247,
            output_tokens=342,
            cache_read_input_tokens=8912,
            cache_creation_input_tokens=0,
        )
    )
    assert out == {
        "input": 1247,
        "output": 342,
        "cache_read": 8912,
        "cache_creation": 0,
    }


def test_openai_style_usage_has_zero_cache():
    out = Bridge._usage_from_response(_resp(prompt_tokens=100, completion_tokens=50))
    assert out == {"input": 100, "output": 50, "cache_read": 0, "cache_creation": 0}


def test_no_usage_object():
    assert Bridge._usage_from_response(SimpleNamespace(_raw=None)) == {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
    }


def test_fully_cached_turn_has_zero_new_input():
    # When the whole prefix is a cache hit, input_tokens is 0 but cache_read is large.
    out = Bridge._usage_from_response(
        _resp(input_tokens=0, output_tokens=12, cache_read_input_tokens=9000)
    )
    assert out["input"] == 0
    assert out["cache_read"] == 9000
