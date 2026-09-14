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


# ── multi-turn breakpoint: last content block of the last message ─────────────


def test_last_message_string_content_gets_cached_block():
    kwargs = {
        "system": "s",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "latest question"},
        ],
    }
    out = apply_prompt_caching(kwargs)
    # only the LAST message is wrapped + marked
    assert out["messages"][-1]["content"] == [
        {"type": "text", "text": "latest question", "cache_control": EPHEMERAL}
    ]
    # earlier message left untouched
    assert out["messages"][0] == {"role": "user", "content": "first"}


def test_last_message_block_list_marks_last_block():
    kwargs = {
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "1"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "2"},
                ],
            },
        ],
    }
    out = apply_prompt_caching(kwargs)
    blocks = out["messages"][-1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == EPHEMERAL


def test_message_breakpoint_does_not_mutate_caller_history():
    history = [{"role": "user", "content": "only"}]
    apply_prompt_caching({"messages": history})
    # the caller's stored history must be untouched (copied before annotating)
    assert history == [{"role": "user", "content": "only"}]


def test_search_mode_index_rides_the_marked_system_block():
    """In search mode the catalogue index composes into the SYSTEM prompt (not the
    first user message, as before 0.262.0). apply_prompt_caching marks the system
    block, so the index sits inside the cached prefix — and never in messages[0].
    This is the whole point of the move: a gateway that caches only the system
    block then caches the index."""
    from runspec_console.bridge import Bridge

    index_section = Bridge._catalog_system_section("- ping — Ping a host.")
    system = "BASE INSTRUCTIONS\n\n" + index_section
    out = apply_prompt_caching(
        {
            "system": system,
            "tools": _tools(1),
            "messages": [{"role": "user", "content": "ping web01"}],
        }
    )
    block = out["system"][0]
    assert block["cache_control"] == EPHEMERAL  # the system block is the cached one
    assert "- ping — Ping a host." in block["text"]  # …and the index is inside it
    # The index is absent from the (also-marked) first/last user message.
    assert "- ping — Ping a host." not in str(out["messages"][0]["content"])


def test_last_message_sdk_object_blocks_left_untouched():
    # An assistant turn whose content is raw SDK block objects (not dicts) can't
    # carry a cache_control dict key — it's left as-is rather than crashing.
    class _Block:
        pass

    blk = _Block()
    kwargs = {"messages": [{"role": "assistant", "content": [blk]}]}
    out = apply_prompt_caching(kwargs)
    assert out["messages"][-1]["content"] == [blk]
