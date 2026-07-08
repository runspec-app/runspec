"""Daemon + discovery tests."""

from __future__ import annotations

from pathlib import Path

from runspec_scheduler import daemon, discovery
from runspec_scheduler.config import ScheduleEntry, upsert_schedule
from runspec_scheduler.discovery import Target
from runspec_scheduler.timing import When


def test_discover_excludes_self_and_runspec(monkeypatch):
    fake = [
        {"runnable": "runspec-scheduler", "source": "/a", "spec": {}},
        {"runnable": "runspec", "source": "/b", "spec": {}},
        {"runnable": "backup", "source": "/c", "spec": {"run_as": "svc"}},
    ]
    monkeypatch.setattr(discovery, "_discover_installed", lambda: fake)
    monkeypatch.setattr(discovery, "_deduplicate", lambda x: x)
    got = discovery.discover()
    assert set(got) == {"backup"}


def test_fire_entry_invokes_runnable(monkeypatch, tmp_path):
    calls = {}
    target = Target(
        runnable="backup",
        source="/pkg/runspec.toml",
        binary=Path("/venv/bin/backup"),
        run_as="svc",
        become_method="sudo",
        become_flags=None,
    )
    monkeypatch.setattr(daemon, "resolve_target", lambda name: target)

    class R:
        returncode = 0
        stdout = "done"
        stderr = ""

    def fake_run(binary, command_path, args, **kw):
        calls["binary"] = binary
        calls["args"] = args
        calls["kw"] = kw
        return R()

    monkeypatch.setattr(daemon, "run_runnable", fake_run)

    entry = ScheduleEntry(
        id="s",
        when=When(every="15m"),
        runnable="backup",
        args={"level": "warn"},
        run_as="svc",
        env_file="/home/svc/s.env",
    )
    d = daemon.Daemon(tmp_path / "c.toml")
    code = d.fire_entry(entry)
    assert code == 0
    assert calls["binary"] == target.binary
    assert calls["args"] == {"level": "warn"}
    assert calls["kw"]["run_as"] == "svc"
    assert calls["kw"]["env_file"] == "/home/svc/s.env"
    assert calls["kw"]["source_toml"] == "/pkg/runspec.toml"


def test_fire_entry_missing_runnable(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "resolve_target", lambda name: None)
    d = daemon.Daemon(tmp_path / "c.toml")
    entry = ScheduleEntry(id="s", when=When(every="15m"), runnable="gone")
    assert d.fire_entry(entry) is None


def test_rebuild_loads_enabled_only(monkeypatch, tmp_path):
    path = tmp_path / "c.toml"
    upsert_schedule(path, ScheduleEntry(id="on", when=When(every="15m"), runnable="a", enabled=True))
    upsert_schedule(path, ScheduleEntry(id="off", when=When(every="15m"), runnable="b", enabled=False))
    d = daemon.Daemon(path)
    try:
        d._rebuild()
        jobs = d._scheduler.get_jobs()
        assert {j.id for j in jobs} == {"on"}
    finally:
        d._shutdown()
