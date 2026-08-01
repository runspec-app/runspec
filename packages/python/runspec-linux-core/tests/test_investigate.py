"""App-discovery & investigation helpers — pure parsers + escalation argv."""

from __future__ import annotations

import subprocess

import pytest

from runspec_linux_core import investigate


def test_parse_show_and_exec_path() -> None:
    text = "Id=nginx.service\nExecStart={ path=/usr/sbin/nginx ; argv[]=/usr/sbin/nginx -g daemon on; ; ignore_errors=no }\nWorkingDirectory=/etc/nginx\nUser=www-data\n"
    props = investigate._parse_show(text)
    assert props["Id"] == "nginx.service"
    assert investigate._exec_start_path(props["ExecStart"]) == "/usr/sbin/nginx"


def test_service_detail_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(investigate.shutil, "which", lambda _: "/bin/systemctl")
    calls: list[list[str]] = []

    def fake_run(argv, capture_output=None, text=None):  # noqa: A002
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="Id=app.service\nExecStart={ path=/opt/app/bin ; }\n", stderr="")

    monkeypatch.setattr(investigate.subprocess, "run", fake_run)
    out = investigate.service_detail("app", run_as="root")
    assert calls[0][:4] == ["sudo", "-n", "-u", "root"]
    assert out["unit"] == "app.service"
    assert out["exec_start_path"] == "/opt/app/bin"


def test_parse_ss_listening_tcp_only() -> None:
    # `ss -tlnp` (no Netid column)
    text = (
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'LISTEN 0      511    0.0.0.0:8080       0.0.0.0:*         users:(("gunicorn",pid=1234,fd=7))\n'
        'LISTEN 0      128    127.0.0.1:5432     0.0.0.0:*         users:(("postgres",pid=99,fd=5))\n'
    )
    rows = investigate._parse_ss_listening(text)
    assert rows[0]["port"] == "8080"
    assert rows[0]["processes"] == [{"process": "gunicorn", "pid": 1234}]
    assert rows[1]["processes"][0]["pid"] == 99


def test_parse_ss_listening_with_netid() -> None:
    # `ss -tulnp` (leading Netid column) — the local address is still anchored
    # on the wildcard peer, so the port resolves correctly despite the shift.
    text = 'Netid State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\nudp   UNCONN 0      0      0.0.0.0:53         0.0.0.0:*         users:(("systemd-resolve",pid=7,fd=12))\n'
    rows = investigate._parse_ss_listening(text)
    assert rows[0]["proto"] == "udp"
    assert rows[0]["port"] == "53"
    assert rows[0]["processes"][0]["pid"] == 7


def test_open_ports_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(investigate.shutil, "which", lambda _: "/usr/bin/ss")
    calls: list[list[str]] = []

    def fake_run(argv, capture_output=None, text=None):  # noqa: A002
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="Netid State\n", stderr="")

    monkeypatch.setattr(investigate.subprocess, "run", fake_run)
    investigate.open_ports("tcp", run_as="root")
    assert calls[0] == ["sudo", "-n", "-u", "root", "ss", "-tlnp"]


def test_parse_failed_units() -> None:
    text = "app.service loaded failed failed My App\n● bad.service loaded failed failed Bad\n"
    rows = investigate._parse_failed_units(text)
    assert rows[0]["unit"] == "app.service"
    assert rows[1]["unit"] == "bad.service"
    assert rows[0]["active"] == "failed"


def test_owning_package_dpkg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(investigate.shutil, "which", lambda name: "/usr/bin/dpkg" if name == "dpkg" else None)
    monkeypatch.setattr(
        investigate.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="coreutils: /usr/bin/ls\n", stderr=""),
    )
    out = investigate.owning_package("/usr/bin/ls")
    assert out == {"path": "/usr/bin/ls", "owned": True, "package": "coreutils", "manager": "dpkg"}
