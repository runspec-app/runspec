"""Missing-tool preconditions raise ToolNotFoundError (no mocks of the work itself).

These deterministically exercise the raise-on-failure contract by making the
relevant ``shutil.which`` lookup report the tool as absent.
"""

import pytest

import runspec_linux_core as core
from runspec_linux_core.errors import ToolNotFoundError


def test_check_service_requires_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.services.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.check_service("nginx")


def test_list_services_requires_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.services.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.list_services("all")


def test_restart_service_requires_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.services.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.restart_service("nginx")


def test_journalctl_requires_journalctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.logs.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.journalctl("nginx")


def test_show_connections_requires_ss_or_netstat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.network.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.show_connections("all")


def test_last_logins_requires_last(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.security.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.last_logins(10)


def test_who_requires_who(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.security.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.who()


def test_list_containers_requires_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.containers.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.list_containers(False)


def test_container_logs_requires_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.containers.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.container_logs("web", 10)


def test_restart_container_requires_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.containers.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.restart_container("web")
