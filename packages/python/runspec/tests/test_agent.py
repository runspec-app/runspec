"""
Tests for __runspec_agent__ detection on RunSpec.

parse() reads RUNSPEC_AGENT from the environment and exposes it
as args.__runspec_agent__ so runnables can adapt their output without
touching os.environ directly.
"""

from __future__ import annotations

import textwrap

import pytest

import runspec


@pytest.fixture()
def spec_dir(tmp_path, monkeypatch):
    """Minimal runspec.toml in a temp dir, with cwd pointed at it."""
    (tmp_path / "runspec.toml").write_text(
        textwrap.dedent("""\
            [greet]
            description = "Say hello"

            [greet.args.name]
            type = "str"
            default = "world"
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_agent_false_by_default(spec_dir, monkeypatch):
    monkeypatch.delenv("RUNSPEC_AGENT", raising=False)
    args = runspec.parse(script_name="greet", argv=[])
    assert args.__runspec_agent__ is False


def test_agent_true_when_set_to_1(spec_dir, monkeypatch):
    monkeypatch.setenv("RUNSPEC_AGENT", "1")
    args = runspec.parse(script_name="greet", argv=[])
    assert args.__runspec_agent__ is True


def test_agent_true_when_set_to_true(spec_dir, monkeypatch):
    monkeypatch.setenv("RUNSPEC_AGENT", "true")
    args = runspec.parse(script_name="greet", argv=[])
    assert args.__runspec_agent__ is True


def test_agent_true_when_set_to_yes(spec_dir, monkeypatch):
    monkeypatch.setenv("RUNSPEC_AGENT", "yes")
    args = runspec.parse(script_name="greet", argv=[])
    assert args.__runspec_agent__ is True


def test_agent_true_case_insensitive(spec_dir, monkeypatch):
    monkeypatch.setenv("RUNSPEC_AGENT", "TRUE")
    args = runspec.parse(script_name="greet", argv=[])
    assert args.__runspec_agent__ is True


def test_agent_false_when_set_to_0(spec_dir, monkeypatch):
    monkeypatch.setenv("RUNSPEC_AGENT", "0")
    args = runspec.parse(script_name="greet", argv=[])
    assert args.__runspec_agent__ is False


def test_agent_false_when_empty(spec_dir, monkeypatch):
    monkeypatch.setenv("RUNSPEC_AGENT", "")
    args = runspec.parse(script_name="greet", argv=[])
    assert args.__runspec_agent__ is False


def test_agent_does_not_affect_args(spec_dir, monkeypatch):
    monkeypatch.setenv("RUNSPEC_AGENT", "1")
    args = runspec.parse(script_name="greet", argv=["--name", "Jason"])
    assert args.__runspec_agent__ is True
    assert args.name.value == "Jason"


def test_agent_field_on_runspec_model():
    from pathlib import Path

    from runspec.models import RunSpec

    rs = RunSpec(__runspec_runnable__="greet", __runspec_source__=Path("/tmp/runspec.toml"))
    assert rs.__runspec_agent__ is False


def test_agent_field_explicit_true():
    from pathlib import Path

    from runspec.models import RunSpec

    rs = RunSpec(__runspec_runnable__="greet", __runspec_source__=Path("/tmp/runspec.toml"), __runspec_agent__=True)
    assert rs.__runspec_agent__ is True
