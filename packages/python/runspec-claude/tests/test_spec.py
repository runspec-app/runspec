"""The bundled runspec.toml must load and expose the four subcommands."""

from __future__ import annotations

from pathlib import Path

import runspec as rs

_SPEC = Path(__file__).resolve().parent.parent / "runspec_claude" / "runspec.toml"


def test_spec_loads():
    spec = rs.load_spec("claude-run", config_path=_SPEC)
    assert spec.runspec_runnable == "claude-run"


def test_subcommands_present():
    spec = rs.load_spec("claude-run", config_path=_SPEC)
    commands = set(spec.runspec_spec.get("commands", {}))
    assert commands == {"ask", "continue", "resume", "sessions"}


def test_global_args_inherited():
    spec = rs.load_spec("claude-run", config_path=_SPEC)
    args = spec.runspec_spec.get("args", {})
    assert "permission-mode" in args
    assert args["permission-mode"]["default"] == "default"
