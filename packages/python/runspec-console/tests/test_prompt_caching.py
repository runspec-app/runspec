"""
test_prompt_caching.py — apply_prompt_caching adds cache_control breakpoints to
the static prompt prefix (tools + system). Pure function; no anthropic SDK needed.
"""

from __future__ import annotations

from runspec_console.adapters.base import apply_prompt_caching

EPHEMERAL = {"type": "ephemeral"}


def _tools(n=3):
    return [
        {"name": f"t{i}", "description": f"d{i}", "input_schema": {"type": "object"}}
        for i in range(n)
    ]


def test_last_tool_gets_cache_control():
    kwargs = {"tools": _tools(3), "system": "sys"}
    out = apply_prompt_caching(kwargs)
    # only the last tool is annotated — it caches the whole tools prefix
    assert out["tools"][-1]["cache_control"] == EPHEMERAL
    assert "cache_control" not in out["tools"][0]
    assert "cache_control" not in out["tools"][1]


def test_system_str_becomes_cached_block():
    out = apply_prompt_caching({"system": "you are helpful", "tools": _tools(1)})
    assert out["system"] == [
        {"type": "text", "text": "you are helpful", "cache_control": EPHEMERAL}
    ]


def test_does_not_mutate_caller_tool_dicts():
    original = _tools(2)
    apply_prompt_caching({"tools": original, "system": "s"})
    # the caller's shared list/dicts must be untouched (copied before annotating)
    assert "cache_control" not in original[-1]


def test_no_tools_only_system_cached():
    out = apply_prompt_caching({"system": "s", "messages": []})
    assert "tools" not in out
    assert out["system"][0]["cache_control"] == EPHEMERAL


def test_empty_tools_left_alone():
    out = apply_prompt_caching({"tools": [], "system": "s"})
    assert out["tools"] == []  # nothing to annotate, no crash


def test_blank_system_not_wrapped():
    out = apply_prompt_caching({"system": "   ", "tools": _tools(1)})
    assert out["system"] == "   "  # left as-is; only the tools get cached
    assert out["tools"][-1]["cache_control"] == EPHEMERAL


def test_returns_same_dict():
    kwargs = {"system": "s", "tools": _tools(1)}
    assert apply_prompt_caching(kwargs) is kwargs
