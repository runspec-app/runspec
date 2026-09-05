"""
test_discovery_argfields.py — discovery carries the string-validation and
metadata arg fields the Specs inspector surfaces (pattern / min-length /
max-length / delimiter / meta), accepting both the hyphenated TOML spelling and
the underscore-normalised CLI-JSON spelling.
"""

from __future__ import annotations

from typing import Any

from runspec_console.discovery import _spec_to_runnables


def _args_by_name(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    runnables = _spec_to_runnables("tool", spec, "local", "grp")
    top = runnables[0]
    return {a["name"]: a for a in top["args"]}


def test_validation_fields_survive_hyphen_and_underscore() -> None:
    spec: dict[str, Any] = {
        "description": "tool",
        "args": {
            # hyphenated (raw-TOML discovery path)
            "code": {
                "type": "str",
                "pattern": "^[A-Z]{3}$",
                "min-length": 3,
                "max-length": 3,
                "meta": {"team": "ops"},
            },
            # underscore (CLI-JSON path) + delimiter
            "tags": {
                "type": "str",
                "delimiter": ",",
                "min_length": 1,
                "max_length": 50,
            },
        },
    }
    args = _args_by_name(spec)

    code = args["code"]
    assert code["pattern"] == "^[A-Z]{3}$"
    assert code["min_length"] == 3
    assert code["max_length"] == 3
    assert code["meta"] == {"team": "ops"}

    tags = args["tags"]
    assert tags["delimiter"] == ","
    assert tags["min_length"] == 1
    assert tags["max_length"] == 50


def test_absent_validation_fields_are_omitted() -> None:
    spec: dict[str, Any] = {
        "description": "tool",
        "args": {"plain": {"type": "str"}},
    }
    plain = _args_by_name(spec)["plain"]
    for key in ("pattern", "min_length", "max_length", "delimiter", "meta"):
        assert key not in plain
