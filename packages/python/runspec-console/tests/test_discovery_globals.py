"""
test_discovery_globals.py — discovery merges inherited (global / parent-command)
args down the subcommand tree, tagging them so the Forms view can group them.

Mirrors core parser._resolve_subcommand: the effective arg set at a node is the
ancestor args merged with the node's own, child overriding parent on a clash.
"""

from __future__ import annotations

from typing import Any

from runspec_console.discovery import _spec_to_runnables

# region/env have no default → required by inference; source has a default.
# multi adds a required `symbols` and nests `create`, which *overrides* region.
SPEC: dict[str, Any] = {
    "description": "tool",
    "args": {
        "region": {"type": "choice", "options": ["us", "eu"]},
        "env": {"type": "choice", "options": ["dev", "prod"]},
        "source": {"default": "examplecorp_symbol"},
    },
    "commands": {
        "show": {
            "description": "show",
            "args": {"symbol": {"type": "str"}},
        },
        "multi": {
            "description": "multi",
            "require-command": True,
            "args": {"symbols": {"type": "str", "multiple": True, "required": True}},
            "commands": {
                "create": {
                    "args": {
                        "price": {"type": "float", "required": False},
                        # override the inherited global `region`
                        "region": {"type": "str"},
                    },
                },
            },
        },
    },
}


def _root() -> dict[str, Any]:
    return _spec_to_runnables("tool", SPEC, "localhost", "grp")[0]


def _argmap(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a["name"]: a for a in node["args"]}


def test_root_args_are_not_inherited():
    root = _root()
    m = _argmap(root)
    assert set(m) == {"region", "env", "source"}
    assert all(not a.get("inherited") for a in root["args"])
    assert m["region"]["required"] is True
    assert m["env"]["required"] is True
    assert m["source"]["required"] is False  # has a default


def test_leaf_inherits_globals_tagged():
    show = _root()["commands"]["show"]
    m = _argmap(show)
    assert set(m) == {"region", "env", "source", "symbol"}
    assert m["region"]["inherited"] is True
    assert m["env"]["inherited"] is True
    assert m["source"]["inherited"] is True
    assert not m["symbol"].get("inherited")  # the command's own arg
    assert m["symbol"]["required"] is True


def test_intermediate_node_owns_its_arg():
    multi = _root()["commands"]["multi"]
    m = _argmap(multi)
    assert not m["symbols"].get("inherited")  # declared on multi
    assert m["region"]["inherited"] is True  # from the root


def test_two_tier_inheritance_and_child_override():
    create = _root()["commands"]["multi"]["commands"]["create"]
    m = _argmap(create)
    # root globals + multi's symbols are all inherited here
    assert m["env"]["inherited"] is True
    assert m["source"]["inherited"] is True
    assert m["symbols"]["inherited"] is True
    # price is create's own
    assert not m["price"].get("inherited")
    # region was overridden at create → own, the str type wins, no duplicate
    assert not m["region"].get("inherited")
    assert m["region"]["type"] == "str"
    assert [a["name"] for a in create["args"]].count("region") == 1


def test_no_commands_runnable_unchanged():
    flat = _spec_to_runnables(
        "flat", {"args": {"x": {"type": "int", "default": 1}}}, "h", "g"
    )[0]
    assert "commands" not in flat
    assert all(not a.get("inherited") for a in flat["args"])
