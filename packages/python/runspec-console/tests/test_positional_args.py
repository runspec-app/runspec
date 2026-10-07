"""
test_positional_args.py — positional args are emitted as bare values, not flags.

Core's parser only accepts a positional arg by position; passing it as
`--name value` is rejected as an unknown argument (a hard failure on run). The
executor must therefore emit positionals as bare values, and the bridge resolves
which args are positional from the discovered spec.
"""

from __future__ import annotations

from runspec_console.bridge import Bridge
from runspec_console.executor import args_to_argv


# ── args_to_argv ──────────────────────────────────────────────────────────────


def test_positional_emitted_bare_after_flags():
    assert args_to_argv({"symbol": "AAPL", "price": 1.5}, {"symbol": 1}) == [
        "--price",
        "1.5",
        "AAPL",
    ]


def test_multiple_positionals_in_position_order():
    # dict order is b, a — but they emit in position order (a=1, b=2).
    assert args_to_argv({"b": "B", "a": "A", "flag": True}, {"a": 1, "b": 2}) == [
        "--flag",
        "A",
        "B",
    ]


def test_no_positionals_all_flags():
    assert args_to_argv({"x": "1"}) == ["--x", "1"]


def test_blank_and_none_still_skipped():
    assert args_to_argv({"x": "", "y": None, "z": "v"}) == ["--z", "v"]


def test_list_positional_expands_in_order():
    assert args_to_argv({"items": ["a", "b"]}, {"items": 1}) == ["a", "b"]


def test_positional_blank_skipped():
    assert args_to_argv({"sym": "", "p": 2}, {"sym": 1}) == ["--p", "2"]


# ── Bridge._positionals_for ───────────────────────────────────────────────────


def test_positionals_for_flat_runnable():
    cached = {"args": [{"name": "symbol", "position": 1}, {"name": "price"}]}
    assert Bridge._positionals_for(cached, []) == {"symbol": 1}


def test_positionals_for_resolves_subcommand_leaf():
    cached = {
        "args": [{"name": "region"}],
        "commands": {"show": {"args": [{"name": "symbol", "position": 1}]}},
    }
    assert Bridge._positionals_for(cached, ["show"]) == {"symbol": 1}


def test_positionals_for_uncached_is_empty():
    assert Bridge._positionals_for(None, []) == {}
    assert Bridge._positionals_for({"args": [{"name": "x"}]}, ["missing"]) == {}


def test_object_values_are_sent_as_json():
    # An agent often passes a JSON-object arg as a real object; str() would send
    # Python's repr (single quotes, True), which the runnable's JSON parse rejects.
    import json

    argv = args_to_argv({"values": {"summary": "New laptop", "urgent": True}})
    assert argv[0] == "--values"
    assert json.loads(argv[1]) == {"summary": "New laptop", "urgent": True}
    assert args_to_argv({"x": [{"a": 1}]}) == ["--x", '{"a": 1}']
    assert args_to_argv({"x": ["a", "b"]}) == ["--x", "a", "--x", "b"]
