"""Tests for install-service (unit write + systemctl session handling)."""

from __future__ import annotations

from runspec_scheduler import service


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _redirect_unit_dir(monkeypatch, tmp_path):
    d = tmp_path / "systemd" / "user"
    monkeypatch.setattr(service, "unit_dir", lambda: d)
    return d


def test_is_bus_error():
    assert service._is_bus_error("Failed to connect to bus: No medium found")
    assert service._is_bus_error("No medium found")
    assert not service._is_bus_error("some other failure")


def test_install_no_enable_writes_unit(monkeypatch, tmp_path):
    d = _redirect_unit_dir(monkeypatch, tmp_path)
    res = service.install_service(enable=False)
    assert res["ok"] is True and res["enabled"] is False
    assert (d / service.unit_name()).is_file()
    assert "enable --now" in res["hint"]


def test_install_enable_success(monkeypatch, tmp_path):
    _redirect_unit_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: _Proc(0))
    res = service.install_service(enable=True)
    assert res["ok"] is True and res["enabled"] is True


def test_install_bus_error_gives_login_shell_hint(monkeypatch, tmp_path):
    d = _redirect_unit_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *a, **k: _Proc(1, stderr="Failed to connect to bus: No medium found"),
    )
    res = service.install_service(enable=True)
    assert res["ok"] is False and res["enabled"] is False
    # The unit was still written; the hint points at a login shell + lingering.
    assert (d / service.unit_name()).is_file()
    hint = res["hint"]
    assert "-iu" in hint and "enable-linger" in hint


def test_install_other_error_gives_manual_hint(monkeypatch, tmp_path):
    _redirect_unit_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: _Proc(1, stderr="unit is masked"))
    res = service.install_service(enable=True)
    assert res["ok"] is False
    assert "enable --now" in res["hint"] and "-iu" not in res["hint"]


def test_systemctl_env_keeps_existing_runtime_dir(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    env = service._systemctl_env()
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_systemctl_env_recovers_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(service.os, "geteuid", lambda: 4242)
    run_dir = tmp_path / "4242"
    run_dir.mkdir()
    (run_dir / "bus").touch()
    env = service._systemctl_env(run_base=tmp_path)
    assert env["XDG_RUNTIME_DIR"] == str(run_dir)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={run_dir / 'bus'}"


def test_systemctl_env_no_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(service.os, "geteuid", lambda: 4243)  # no dir created
    env = service._systemctl_env(run_base=tmp_path)
    assert "XDG_RUNTIME_DIR" not in env
