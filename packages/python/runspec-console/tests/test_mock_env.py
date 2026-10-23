"""
test_mock_env.py — mock playback (RUNSPEC_MOCK) set where the console was
started reaches runs that don't inherit the console's environment: ``run_as``
runs (sudo/su strip it) and remote runs. RUNSPEC_MOCK_DIR is a local path, so
it is only carried to local runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from runspec_console import executor
from runspec_console.executor import mock_env


class _Stop(Exception):
    pass


def test_mock_env_empty_when_unset(monkeypatch):
    monkeypatch.delenv("RUNSPEC_MOCK", raising=False)
    monkeypatch.delenv("RUNSPEC_MOCK_DIR", raising=False)
    assert mock_env() == {}
    assert mock_env(remote=True) == {}


def test_mock_env_local_and_remote(monkeypatch):
    monkeypatch.setenv("RUNSPEC_MOCK", "strict")
    monkeypatch.setenv("RUNSPEC_MOCK_DIR", "/tmp/mocks")
    assert mock_env() == {"RUNSPEC_MOCK": "strict", "RUNSPEC_MOCK_DIR": "/tmp/mocks"}
    assert mock_env(remote=True) == {"RUNSPEC_MOCK": "strict"}


def test_remote_run_carries_mock(monkeypatch):
    monkeypatch.setenv("RUNSPEC_MOCK", "1")
    monkeypatch.setenv("RUNSPEC_MOCK_DIR", "/tmp/mocks")
    seen: dict[str, Any] = {}

    def fake(cmd_argv, run_as, become_method, become_flags, env=None):
        seen["env"] = env
        raise _Stop

    monkeypatch.setattr(executor, "remote_exec_command", fake)
    with pytest.raises(_Stop):
        executor.run_remote(
            "web-01",
            "/opt/venv/bin/runspec",
            "jira",
            {},
            ["get-issue"],
            on_line=lambda line, stream: None,
            on_done=lambda code, ms: None,
        )
    assert seen["env"] == {"RUNSPEC_MOCK": "1", "RUNSPEC_LABEL": "console"}


def test_run_as_local_run_carries_mock(monkeypatch):
    monkeypatch.setenv("RUNSPEC_MOCK", "1")
    monkeypatch.setenv("RUNSPEC_MOCK_DIR", "/tmp/mocks")
    seen: dict[str, Any] = {}

    def fake(cmd, run_as, become_method, become_flags, env=None):
        seen["env"] = env
        raise _Stop

    import runspec.become

    monkeypatch.setattr(runspec.become, "build_exec_argv", fake)
    with pytest.raises(_Stop):
        executor.run_local(
            "/opt/venv/bin/runspec",
            "jira",
            {},
            [],
            on_line=lambda line, stream: None,
            on_done=lambda code, ms: None,
            run_as="svc",
        )
    assert seen["env"]["RUNSPEC_MOCK"] == "1"
    assert seen["env"]["RUNSPEC_MOCK_DIR"] == "/tmp/mocks"
