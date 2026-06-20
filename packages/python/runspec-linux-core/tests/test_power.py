"""reboot_host builds the right command and surfaces failures as CommandError."""

import subprocess

import pytest

from runspec_linux_core import power
from runspec_linux_core.errors import CommandError


def _completed(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_reboot_now_uses_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("runspec_linux_core.power.which", lambda name: name if name == "shutdown" else None)
    monkeypatch.setattr(
        "runspec_linux_core.power.subprocess.run",
        lambda cmd, **kw: seen.append(cmd) or _completed(),
    )
    result = power.reboot_host(0)
    assert seen == [["shutdown", "-r", "now"]]
    assert result == {"scheduled": True, "delay_minutes": 0, "command": "shutdown -r now"}


def test_reboot_with_delay_schedules(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("runspec_linux_core.power.which", lambda name: name if name == "shutdown" else None)
    monkeypatch.setattr(
        "runspec_linux_core.power.subprocess.run",
        lambda cmd, **kw: seen.append(cmd) or _completed(),
    )
    result = power.reboot_host(5)
    assert seen == [["shutdown", "-r", "+5"]]
    assert result["delay_minutes"] == 5


def test_reboot_falls_back_to_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("runspec_linux_core.power.which", lambda name: name if name == "systemctl" else None)
    monkeypatch.setattr(
        "runspec_linux_core.power.subprocess.run",
        lambda cmd, **kw: seen.append(cmd) or _completed(),
    )
    result = power.reboot_host(0)
    assert seen == [["systemctl", "reboot"]]
    assert result["command"] == "systemctl reboot"


def test_reboot_non_zero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.power.which", lambda name: name if name == "shutdown" else None)
    monkeypatch.setattr(
        "runspec_linux_core.power.subprocess.run",
        lambda cmd, **kw: _completed(returncode=1, stderr="Operation not permitted"),
    )
    with pytest.raises(CommandError):
        power.reboot_host(0)
