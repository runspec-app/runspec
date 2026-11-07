"""Tests for the systemd units and systemctl session handling."""

from __future__ import annotations

from runspec_scheduler import service


def test_is_bus_error():
    assert service._is_bus_error("Failed to connect to bus: No medium found")
    assert service._is_bus_error("No medium found")
    assert not service._is_bus_error("some other failure")


def test_user_unit_leaves_running_fires_on_restart():
    text = service.render_unit(["/v/bin/runspec-scheduler", "run"])
    assert "KillMode=process" in text and "Restart=on-failure" in text
    assert "ExecStart=/v/bin/runspec-scheduler run" in text
    assert "WantedBy=default.target" in text and "User=" not in text


def test_system_unit_runs_as_the_owner():
    text = service.render_system_unit("svc-ops", ["/opt/venvs/ops/bin/runspec-scheduler", "run"])
    assert "User=svc-ops" in text and "KillMode=process" in text
    assert "WantedBy=multi-user.target" in text


def test_unit_quotes_a_path_with_spaces():
    text = service.render_unit(["/v w/bin/runspec-scheduler", "run", "--config", "/v w/c.toml"])
    assert "ExecStart='/v w/bin/runspec-scheduler' run --config '/v w/c.toml'" in text


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
