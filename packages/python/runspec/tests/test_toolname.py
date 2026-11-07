"""MCP tool name length: fit_tool_name, leaf_tool_names, serve + runspec test wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from runspec.inference import infer_script
from runspec.serve import _expand_tools, _fit_name
from runspec.toolname import MCP_TOOL_NAME_MAX, fit_tool_name, leaf_tool_names


def test_short_name_unchanged():
    assert fit_tool_name("gitlab_get-issue") == "gitlab_get-issue"
    assert fit_tool_name("x" * MCP_TOOL_NAME_MAX) == "x" * MCP_TOOL_NAME_MAX


def test_long_name_fits_and_is_stable():
    name = "a" * 200
    fitted = fit_tool_name(name)
    assert len(fitted) <= MCP_TOOL_NAME_MAX
    assert fitted == fit_tool_name(name)
    assert fitted.startswith("a" * 100)


def test_long_names_sharing_a_start_stay_distinct():
    base = "acme-department-tools_" + "raise-" * 30
    one, two = fit_tool_name(base + "laptop"), fit_tool_name(base + "desktop")
    assert one != two
    assert len(one) <= MCP_TOOL_NAME_MAX and len(two) <= MCP_TOOL_NAME_MAX


def test_fixed_vector():
    # Pinned so the Node pack (src/toolname.ts) can assert the same output.
    assert fit_tool_name("abcdefghij_klmnop", 12) == "abcde_10b41d"


def test_trailing_separator_dropped_before_hash():
    fitted = fit_tool_name("abc_" + "x" * 20, 11)
    assert fitted.startswith("abc_") and not fitted.startswith("abc__")


def test_custom_limit():
    assert len(fit_tool_name("y" * 80, 64)) <= 64


def _spec() -> dict:
    return {
        "description": "d",
        "args": {"g": {"default": "x"}},
        "commands": {
            "a": {"description": "a", "args": {}},
            "b": {"description": "b", "commands": {"c": {"description": "c", "args": {}}}},
        },
    }


def test_leaf_tool_names_match_expand_tools():
    inferred = infer_script(_spec(), "confirm")
    served = [t[0] for t in _expand_tools("tool", inferred, [], None, "sudo", None)]
    assert leaf_tool_names("tool", _spec()) == served == ["tool_a", "tool_b_c"]


def test_serve_fit_name_warns_and_renames(capsys: pytest.CaptureFixture[str]):
    long_name = "r" * 140
    name, schema = _fit_name(long_name, {"name": long_name, "inputSchema": {}})
    assert len(name) <= MCP_TOOL_NAME_MAX
    assert schema["name"] == name
    err = capsys.readouterr().err
    assert "140 characters" in err and name in err


def test_serve_fit_name_leaves_short_names_quiet(capsys: pytest.CaptureFixture[str]):
    assert _fit_name("short", {"name": "short"}) == ("short", {"name": "short"})
    assert capsys.readouterr().err == ""


def test_runspec_test_warns_on_long_leaf(tmp_path: Path):
    from runspec.cli import _tool_name_warnings

    long_cmd = "c" * 130
    (tmp_path / "runspec.toml").write_text(f'[tool]\ndescription = "d"\n\n[tool.commands.{long_cmd}]\ndescription = "x"\n\n[tool.commands.ok]\ndescription = "y"\n')
    warnings = _tool_name_warnings("tool", str(tmp_path / "runspec.toml"))
    assert len(warnings) == 1
    assert f"tool_{long_cmd}" in warnings[0]
