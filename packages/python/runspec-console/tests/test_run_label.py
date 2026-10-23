"""
test_run_label.py — every run started from here carries RUNSPEC_LABEL=console, so
the runnable's runs index records where it was launched from, alongside the
trigger and runbook provenance.
"""

from __future__ import annotations

from typing import Any

import pytest

from runspec_console import executor


class _Stop(Exception):
    pass


def test_local_run_is_labelled(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_stream(cmd, on_line, on_done, **kwargs):
        seen["extra_env"] = kwargs.get("extra_env")

    monkeypatch.setattr(executor, "_stream", fake_stream)
    executor.run_local(
        "/venv/bin/runspec",
        "deploy",
        {},
        [],
        lambda *_: None,
        lambda *_: None,
        trigger="schedule",
        runbook="restart-web",
    )
    assert seen["extra_env"] == {
        "RUNSPEC_LABEL": "console",
        "RUNSPEC_TRIGGER": "schedule",
        "RUNSPEC_RUNBOOK": "restart-web",
    }


def test_remote_run_is_labelled(monkeypatch):
    monkeypatch.delenv("RUNSPEC_MOCK", raising=False)
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
    assert seen["env"] == {"RUNSPEC_LABEL": "console"}
