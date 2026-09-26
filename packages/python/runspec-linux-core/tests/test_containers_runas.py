"""The run_as / become_method escalation on the Docker container helpers.

The subprocess layer is stubbed so no real docker runs; the tests assert the
escalated argv (the ``sudo -n -u`` / ``su`` prefix) and that no escalation
happens when run_as is unset.
"""

from __future__ import annotations

import subprocess

import pytest

from runspec_linux_core import containers


def _stub(monkeypatch: pytest.MonkeyPatch, stdout: str = "", rc: int = 0, stderr: str = "") -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv, capture_output=None, text=None):  # noqa: A002
        calls.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(containers.subprocess, "run", fake_run)
    monkeypatch.setattr(containers, "_docker_available", lambda: True)
    return calls


def test_list_containers_no_run_as(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(monkeypatch, stdout='abc\timg\t"/bin/sh"\tnow\tUp\tweb\n')
    rows = containers.list_containers()
    assert calls[0][0] == "docker"  # no escalation prefix
    assert rows[0]["name"] == "web"


def test_list_containers_run_as_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(monkeypatch, stdout="")
    containers.list_containers(True, run_as="root")
    assert calls[0][:4] == ["sudo", "-n", "-u", "root"]
    assert calls[0][4:7] == ["docker", "ps", "--format"]
    assert "--all" in calls[0]


def test_restart_container_run_as_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(monkeypatch)
    out = containers.restart_container("web", run_as="deploy")
    assert calls[0] == ["sudo", "-n", "-u", "deploy", "docker", "restart", "web"]
    assert out == {"container": "web", "restarted": True, "run_as": "deploy"}


def test_container_logs_run_as_su(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(monkeypatch, stdout="log line 1\nlog line 2\n")
    out = containers.container_logs("web", 10, run_as="svc", become_method="su")
    assert calls[0] == ["su", "svc", "-c", "docker logs --tail 10 web"]
    assert out["count"] == 2
    assert out["run_as"] == "svc"


def test_container_logs_run_as_passwordless_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, rc=1, stderr="sudo: a password is required")
    with pytest.raises(Exception, match="passwordless sudo is not configured for run-as 'svc'"):
        containers.container_logs("web", run_as="svc")


def test_restart_container_run_as_passwordless_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, rc=1, stderr="sudo: a terminal is required")
    with pytest.raises(Exception, match="passwordless sudo is not configured for run-as 'svc'"):
        containers.restart_container("web", run_as="svc")


_SOCKET_DENIED = "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"


def test_list_containers_socket_denied_suggests_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, rc=1, stderr=_SOCKET_DENIED)
    with pytest.raises(containers.CommandError, match="Re-run with --sudo") as err:
        containers.list_containers(include_all=True)
    assert "docker.sock" in str(err.value)


def test_container_logs_socket_denied_is_an_error_not_log_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, rc=1, stderr=_SOCKET_DENIED)
    with pytest.raises(containers.CommandError, match="--sudo"):
        containers.container_logs("web")


def test_container_logs_other_failure_still_returned_as_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    # unchanged: an unescalated non-permission failure keeps the old behaviour
    _stub(monkeypatch, rc=1, stderr="Error: No such container: web")
    assert containers.container_logs("web")["lines"] == ["Error: No such container: web"]


def test_restart_container_socket_denied_suggests_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, rc=1, stderr=_SOCKET_DENIED)
    with pytest.raises(containers.CommandError, match="docker group"):
        containers.restart_container("web")


def test_escalated_socket_denied_keeps_the_generic_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # escalated already → "re-run with --sudo" would be wrong advice
    _stub(monkeypatch, rc=1, stderr=_SOCKET_DENIED)
    with pytest.raises(containers.CommandError) as err:
        containers.list_containers(run_as="deploy")
    assert "--sudo" not in str(err.value)
