"""Perf helpers — pure parsers + the privileged set_sysctl escalation path."""

from __future__ import annotations

import subprocess

import pytest

from runspec_linux_core import perf


def test_parse_sysctl() -> None:
    text = "net.core.somaxconn = 4096\nvm.swappiness = 60\ngarbage line\n"
    entries = perf._parse_sysctl(text)
    assert {"key": "net.core.somaxconn", "value": "4096"} in entries
    assert {"key": "vm.swappiness", "value": "60"} in entries
    assert len(entries) == 2


def test_get_sysctl_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(perf.shutil, "which", lambda _: "/sbin/sysctl")
    monkeypatch.setattr(
        perf.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="a.b = 1\nnet.core.x = 2\n", stderr=""),
    )
    out = perf.get_sysctl("net.core")
    assert out["count"] == 1
    assert out["entries"][0]["key"] == "net.core.x"


def test_parse_pressure() -> None:
    text = "some avg10=0.10 avg60=0.20 avg300=0.30 total=12345\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    parsed = perf._parse_pressure(text)
    assert parsed["some"]["avg10"] == 0.10
    assert parsed["some"]["total"] == 12345.0
    assert parsed["full"]["avg60"] == 0.0


def test_set_sysctl_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, capture_output=None, text=None, input=None):  # noqa: A002
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="net.core.somaxconn = 1024", stderr="")

    monkeypatch.setattr(perf.subprocess, "run", fake_run)
    out = perf.set_sysctl("net.core.somaxconn", "1024", run_as="root")
    assert calls[0] == ["sudo", "-n", "-u", "root", "sysctl", "-w", "net.core.somaxconn=1024"]
    assert out == {"key": "net.core.somaxconn", "value": "1024", "applied": True, "persisted": None, "run_as": "root"}


def test_set_sysctl_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, capture_output=None, text=None, input=None):  # noqa: A002
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(perf.subprocess, "run", fake_run)
    out = perf.set_sysctl("vm.swappiness", "10", persist=True)
    # second call appends to the persist file via tee
    assert calls[1][:2] == ["tee", "-a"]
    assert out["persisted"] == perf._PERSIST_FILE


def test_set_sysctl_rejects_bad_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        perf.set_sysctl("net.core.somaxconn; rm -rf /", "1")
