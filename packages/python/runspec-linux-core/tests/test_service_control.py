"""control_service: systemctl start/stop/restart/reload/enable/disable as root."""

from __future__ import annotations

import subprocess

import pytest

from runspec_linux_core import services
from runspec_linux_core.errors import CommandError, ToolNotFoundError


def _done(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def systemctl(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    results: dict[str, subprocess.CompletedProcess[str]] = {}

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "show"]:
            return _done(stdout="ActiveState=active\nSubState=running\nLoadState=loaded\nDescription=Alloy\n")
        if cmd[:2] == ["systemctl", "is-enabled"]:
            return _done(stdout="enabled\n")
        return results.get(cmd[0], _done())

    monkeypatch.setattr(services, "_systemctl_available", lambda: True)
    monkeypatch.setattr(services.subprocess, "run", run)
    return calls, results


def test_enable_now(systemctl) -> None:
    calls, _ = systemctl
    out = services.control_service("alloy", "enable", now=True)
    assert calls[0] == ["systemctl", "enable", "--now", "--", "alloy"]
    assert out == {"service": "alloy", "action": "enable", "now": True, "active_state": "active", "sub_state": "running", "enabled": "enabled"}


@pytest.mark.parametrize("action", ["start", "stop", "restart", "reload"])
def test_plain_actions_ignore_now(systemctl, action: str) -> None:
    calls, _ = systemctl
    out = services.control_service("alloy.service", action, now=True)
    assert calls[0] == ["systemctl", action, "--", "alloy.service"]
    assert out["now"] is False


def test_template_unit_names_are_allowed(systemctl) -> None:
    services.control_service("getty@tty1.service", "restart")


@pytest.mark.parametrize("name", ["", "-H host", "--all", "a b", "a;b", "$(x)"])
def test_bad_names(systemctl, name: str) -> None:
    with pytest.raises(ValueError):
        services.control_service(name, "start")


def test_bad_action(systemctl) -> None:
    with pytest.raises(ValueError):
        services.control_service("alloy", "mask")


def test_failure_carries_the_journal(systemctl) -> None:
    _, results = systemctl
    results["systemctl"] = _done(returncode=1, stderr="Job for alloy.service failed.")
    results["journalctl"] = _done(stdout="alloy[12]: error: config.alloy:3: unexpected token\n")
    with pytest.raises(CommandError, match="unexpected token"):
        services.control_service("alloy", "restart")


def test_no_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_systemctl_available", lambda: False)
    with pytest.raises(ToolNotFoundError):
        services.control_service("alloy", "start")
