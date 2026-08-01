"""The run_as / become_method escalation on the log + service helpers.

The subprocess layer is stubbed so no real command runs; the tests assert the
escalated argv that *would* run (the ``sudo -n -u`` / ``su`` prefix) and that no
escalation happens when run_as is unset.
"""

from __future__ import annotations

import subprocess

import pytest

from runspec_linux_core import logs, services


def _stub(monkeypatch: pytest.MonkeyPatch, stdout: str = "", rc: int = 0, stderr: str = "") -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv, capture_output=None, text=None, check=None):  # noqa: A002
        calls.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ── tail_log ──────────────────────────────────────────────────────────────────


def test_tail_log_no_run_as_is_in_process(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "app.log"
    f.write_text("a\nb\nc\n")
    calls = _stub(monkeypatch)
    out = logs.tail_log(str(f), 2)
    assert out["lines"] == ["b", "c"]
    assert calls == []  # no subprocess without run_as


def test_tail_log_run_as_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(monkeypatch, stdout="x\ny\n")
    out = logs.tail_log("/var/log/secure", 2, run_as="root")
    assert calls[0] == ["sudo", "-n", "-u", "root", "tail", "-n", "2", "/var/log/secure"]
    assert out["lines"] == ["x", "y"]
    assert out["run_as"] == "root"


def test_tail_log_run_as_su(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(monkeypatch, stdout="x\n")
    logs.tail_log("/var/log/secure", 5, run_as="svc", become_method="su")
    assert calls[0] == ["su", "svc", "-c", "tail -n 5 /var/log/secure"]


# ── search_log ────────────────────────────────────────────────────────────────


def test_search_log_run_as_reads_via_cat_and_applies_regex(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub(monkeypatch, stdout="INFO ok\nERROR boom\nerror lower\n")
    out = logs.search_log("/var/log/app.log", "error", run_as="deploy")
    assert calls[0] == ["sudo", "-n", "-u", "deploy", "cat", "/var/log/app.log"]
    # Python re, case-insensitive — matches both ERROR and error
    assert out["matches"] == ["ERROR boom", "error lower"]
    assert out["run_as"] == "deploy"


# ── journalctl ────────────────────────────────────────────────────────────────


def test_journalctl_run_as_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs.shutil, "which", lambda _: "/usr/bin/journalctl")
    calls = _stub(monkeypatch, stdout="line1\nline2\n")
    out = logs.journalctl("nginx", 10, run_as="root")
    assert calls[0][:4] == ["sudo", "-n", "-u", "root"]
    assert "journalctl" in calls[0]
    assert out["count"] == 2
    assert out["run_as"] == "root"


def test_journalctl_passwordless_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs.shutil, "which", lambda _: "/usr/bin/journalctl")
    _stub(monkeypatch, rc=1, stderr="sudo: a password is required")
    with pytest.raises(Exception, match="passwordless sudo is not configured for run-as 'svc'"):
        logs.journalctl("nginx", run_as="svc")


# ── restart_service ───────────────────────────────────────────────────────────


def test_restart_service_run_as_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_systemctl_available", lambda: True)
    calls = _stub(monkeypatch)
    out = services.restart_service("nginx", run_as="root")
    assert calls[0] == ["sudo", "-n", "-u", "root", "systemctl", "restart", "nginx"]
    assert out == {"service": "nginx", "restarted": True, "run_as": "root"}


def test_restart_service_no_run_as_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_systemctl_available", lambda: True)
    calls = _stub(monkeypatch)
    out = services.restart_service("nginx")
    assert calls[0] == ["systemctl", "restart", "nginx"]
    assert out == {"service": "nginx", "restarted": True}
