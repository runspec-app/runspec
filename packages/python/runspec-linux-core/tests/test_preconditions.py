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


def test_list_packages_requires_a_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.packages.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.list_packages()


def test_list_upgrades_requires_a_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.packages.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.list_upgrades()


def test_upgrade_packages_requires_a_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.packages.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.upgrade_packages()


def test_install_package_requires_a_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.packages.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.install_package("htop")


def test_remove_package_requires_a_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.packages.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.remove_package("htop")


def test_autoremove_packages_requires_a_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.packages.shutil.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.autoremove_packages()


def test_reboot_host_requires_a_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.power.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.reboot_host()


def test_enable_passwordless_sudo_requires_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runspec_linux_core.sudoers.which", lambda _: None)
    with pytest.raises(ToolNotFoundError):
        core.enable_passwordless_sudo("pw", "alice", "scoped")
